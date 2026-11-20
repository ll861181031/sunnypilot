#include <cassert>
#include <mutex>
#include <map>
#ifdef __COMMA_HARDWARE__
#include <exception>
#include <stdexcept>
#endif

#ifdef __COMMA_HARDWARE__
#include "system/loggerd/clip_encoder.h"
#endif
#include "system/loggerd/loggerd.h"
#include "system/loggerd/encoder/jpeg_encoder.h"

#ifdef __COMMA_HARDWARE__
#include "system/loggerd/encoder/v4l_encoder.h"
#define Encoder V4LEncoder
#else
#include "system/loggerd/encoder/ffmpeg_encoder.h"
#define Encoder FfmpegEncoder
#endif

ExitHandler do_exit;
std::atomic<bool> camera_replaced = false;

struct EncoderdState {
  int max_waiting = 0;

  // 启动时对齐采集时间，帧号仅为没有时间戳的旧回放保留。
  std::mutex sync_mutex;
  int encoders_ready = 0;
  std::atomic<uint32_t> start_frame_id = 0;
  std::atomic<uint64_t> start_timestamp_ns = 0;
  std::map<VisionStreamType, bool> camera_ready;
  std::map<VisionStreamType, bool> camera_synced;
  bool start_ready = false;
};

// 先等待所有已配置摄像头就绪，再让各路跨过同一个采集时间起点。
bool sync_encoders(EncoderdState *s, VisionStreamType cam_type, uint32_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(s->sync_mutex);
  if (s->camera_synced[cam_type]) return true;

  if (s->max_waiting > 1 && !s->start_ready) {
    // add a small margin to the start frame id in case one of the encoders already dropped the next frame
    update_max_atomic(s->start_frame_id, frame_id + 2);
    if (timestamp_ns) {
      // 固定到下一整秒，而不是“首帧 + 100 ms”。独立进程的启停和调度延迟可能
      // 让两路首帧落在不同帧周期，后者会让首段起点随启动抖动而错开。
      const uint64_t start_ns = (timestamp_ns / 1'000'000'000ULL + 1) * 1'000'000'000ULL;
      if (start_ns > s->start_timestamp_ns) s->start_timestamp_ns = start_ns;
    }
    if (!std::exchange(s->camera_ready[cam_type], true)) {
      ++s->encoders_ready;
      LOGD("camera %d encoder ready", cam_type);
      if (s->encoders_ready == s->max_waiting) {
        // 所有路都登记后一次性启用起点。下面从 s->start_ready 读取，不能在
        // 首路登记时绕过；这正是首段偶发错开 333 ms 的来源。
        s->start_ready = true;
        LOGD("all encoders ready, start timestamp %" PRIu64, (uint64_t)s->start_timestamp_ns);
      }
    }
    return false;
  } else {
    if (s->max_waiting == 1) {
      update_max_atomic(s->start_frame_id, frame_id);
      if (timestamp_ns) s->start_timestamp_ns = timestamp_ns;
      s->start_ready = true;
    }
    const bool synced = timestamp_ns && s->start_timestamp_ns
        ? timestamp_ns >= s->start_timestamp_ns
        : frame_id >= s->start_frame_id;
    s->camera_synced[cam_type] = synced;
    if (!synced) LOGD("camera %d waiting for frame %d, cur %d", cam_type, (int)s->start_frame_id, frame_id);
    return synced;
  }
}

void encoder_set_bitrate(std::unique_ptr<Encoder> &e) {
  static Params params;
  std::string val = params.get("LivestreamEncoderBitrate");
  if (val.empty()) return;
  int bitrate = std::stoi(val);
  e->set_bitrate(bitrate);
}

void encoder_request_keyframe(std::unique_ptr<Encoder> &e) {
  static Params params;
  if (!params.getBool("LivestreamRequestKeyframe")) return;
  e->request_keyframe();
}

void encoder_thread(EncoderdState *s, const LogCameraInfo &cam_info) {
  util::set_thread_name(cam_info.thread_name);

  std::vector<std::unique_ptr<Encoder>> encoders;

  VisionIpcClient vipc_client = VisionIpcClient("camerad", cam_info.stream_type, false);

  std::unique_ptr<JpegEncoder> jpeg_encoder;

  int cur_seg = 0;
  while (!do_exit) {
    if (!vipc_client.connect(false)) {
      util::sleep_for(5);
      continue;
    }

    // init encoders
    if (encoders.empty()) {
      const VisionBuf &buf_info = vipc_client.buffers[0];
      LOGW("encoder %s init %zux%zu", cam_info.thread_name, buf_info.width, buf_info.height);
      assert(buf_info.width > 0 && buf_info.height > 0);

      for (const auto &encoder_info : cam_info.encoder_infos) {
        auto &e = encoders.emplace_back(new Encoder(encoder_info, buf_info.width, buf_info.height));
        e->encoder_open();
      }

      // Only one thumbnail can be generated per camera stream
      if (auto thumbnail_name = cam_info.encoder_infos[0].thumbnail_name) {
        jpeg_encoder = std::make_unique<JpegEncoder>(thumbnail_name, buf_info.width / 4, buf_info.height / 4);
      }
    }

    bool lagging = false;
    while (!do_exit) {
      VisionIpcBufExtra extra;
      VisionBuf* buf = vipc_client.recv(&extra);
      if (buf == nullptr) {
        if (!vipc_client.is_connected()) {
          // A replacement publisher owns different buffers and can reset frame IDs
          // or stream dimensions. Restart all encoders together via the manager.
          LOGE("encoder %s camera publisher replaced; restarting encoderd", cam_info.thread_name);
          camera_replaced = true;
          do_exit = true;
        }
        continue;
      }

      // detect loop around and drop the frames
      if (buf->get_frame_id() != extra.frame_id) {
        if (!lagging) {
          LOGE("encoder %s lag  buffer id: %" PRIu64 " extra id: %d", cam_info.thread_name, buf->get_frame_id(), extra.frame_id);
          lagging = true;
        }
        continue;
      }
      lagging = false;

      const uint64_t timestamp_ns = extra.timestamp_sof ? extra.timestamp_sof : extra.timestamp_eof;
      if (!sync_encoders(s, cam_info.stream_type, extra.frame_id, timestamp_ns)) {
        continue;
      }
      if (do_exit) break;

      // 各摄像头帧率和编号独立，正常录像使用同一个采集时间基准切段。
      // 没有时间戳的旧回放仍按帧号兼容，不能用解码或调度时间替代采集时间。
      const int frames_per_seg = SEGMENT_LENGTH * MAIN_FPS;
      const uint64_t start_ns = s->start_timestamp_ns;
      if ((timestamp_ns && start_ns && timestamp_ns < start_ns) || (!timestamp_ns && extra.frame_id < s->start_frame_id)) continue;
      const int target_seg = timestamp_ns && start_ns
          ? (timestamp_ns - start_ns) / (uint64_t(SEGMENT_LENGTH) * 1'000'000'000ULL)
          : (extra.frame_id - s->start_frame_id) / frames_per_seg;
      if (cur_seg < target_seg) {
        for (auto &e : encoders) {
          e->encoder_close();
          e->encoder_open();
        }
        // 设备断流后时间戳可能一下跨过多个段。编码器不需要为已经错过的空段
        // 每段开关一次，直接把录制段号推进到当前时间所在的段即可。
        cur_seg = target_seg;
      }

      // encode a frame
      for (int i = 0; i < encoders.size(); ++i) {
        if (cam_info.encoder_infos[i].is_live) {
          encoder_set_bitrate(encoders[i]);
          encoder_request_keyframe(encoders[i]);
        }

        int out_id = encoders[i]->encode_frame(buf, &extra);

        if (out_id == -1) {
          LOGE("Failed to encode frame. frame_id: %d", extra.frame_id);
        }
      }

      if (jpeg_encoder && (extra.frame_id % 1200 == 100)) {
        jpeg_encoder->pushThumbnail(buf, extra);
      }
    }
  }
}

template <size_t N>
void encoderd_thread(const LogCameraInfo (&cameras)[N]) {
  EncoderdState s;

  std::set<VisionStreamType> streams;
  while (!do_exit) {
    streams = VisionIpcClient::getAvailableStreams("camerad", false);
    if (!streams.empty()) {
      break;
    }
    util::sleep_for(100);
  }

  if (!streams.empty()) {
    std::vector<std::thread> encoder_threads;
    // 在线程开始读帧前固定路数，避免首个线程把双摄误判为单摄。
    s.max_waiting = streams.size();
    for (auto stream : streams) {
      auto it = std::find_if(std::begin(cameras), std::end(cameras),
                             [stream](auto &cam) { return cam.stream_type == stream; });
      assert(it != std::end(cameras));
      encoder_threads.push_back(std::thread(encoder_thread, &s, *it));
    }

    for (auto &t : encoder_threads) t.join();
  }
}

int main(int argc, char* argv[]) {
#ifdef __COMMA_HARDWARE__
  if (argc > 1 && std::string(argv[1]) == "--clip") {
    if (argc < 6) {
      fprintf(stderr, "usage: encoderd --clip OUTPUT START DURATION [--bitrate BPS] [--speedup N] "
                      "[--metadata JSON] SEGMENT [SEGMENT ...]\n");
      return 2;
    }
    try {
      int bitrate = 5'000'000;
      int speedup = 1;
      std::string metadata;
      int input_arg = 5;
      while (input_arg < argc && std::string(argv[input_arg]).rfind("--", 0) == 0) {
        const std::string option = argv[input_arg++];
        if (option == "--") break;
        if (input_arg == argc) throw std::invalid_argument("missing clip option value");
        if (option == "--bitrate") bitrate = std::stoi(argv[input_arg++]);
        else if (option == "--speedup") speedup = std::stoi(argv[input_arg++]);
        else if (option == "--metadata") metadata = argv[input_arg++];
        else throw std::invalid_argument("unknown clip option: " + option);
      }
      if (input_arg == argc) throw std::invalid_argument("missing clip input");
      std::vector<std::string> inputs(argv + input_arg, argv + argc);
      return encode_clip(inputs, argv[2], std::stod(argv[3]), std::stod(argv[4]),
                         bitrate, speedup, metadata);
    } catch (const std::exception &e) {
      fprintf(stderr, "clip encoding failed: %s\n", e.what());
      return 1;
    }
  }
#endif
  if (!Hardware::PC()) {
    int ret;
    ret = util::set_realtime_priority(52);
    assert(ret == 0);
    ret = util::set_core_affinity({3});
    assert(ret == 0);
  }
  if (argc > 1) {
    std::string arg1(argv[1]);
    if (arg1 == "--stream") {
      encoderd_thread(stream_cameras_logged);
    } else {
      LOGE("Argument '%s' is not supported", arg1.c_str());
    }
  } else {
    encoderd_thread(cameras_logged);
  }
  return camera_replaced ? 1 : 0;
}

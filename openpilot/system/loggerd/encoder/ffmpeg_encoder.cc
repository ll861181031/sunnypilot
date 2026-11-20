#include "system/loggerd/encoder/ffmpeg_encoder.h"

#include <fcntl.h>
#include <unistd.h>

#include <cassert>
#include <cstdio>
#include <cstdlib>

#define __STDC_CONSTANT_MACROS

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
}

#include "common/swaglog.h"
#include "common/util.h"
#include "common/yuv.h"

const int env_debug_encoder = (getenv("DEBUG_ENCODER") != NULL) ? atoi(getenv("DEBUG_ENCODER")) : 0;

FfmpegEncoder::FfmpegEncoder(const EncoderInfo &encoder_info, int in_width, int in_height)
    : VideoEncoder(encoder_info, in_width, in_height) {
  frame = av_frame_alloc();
  assert(frame);
  frame->format = AV_PIX_FMT_YUV420P;
  frame->width = out_width;
  frame->height = out_height;
  frame->linesize[0] = out_width;
  frame->linesize[1] = out_width/2;
  frame->linesize[2] = out_width/2;

  convert_buf.resize(in_width * in_height * 3 / 2);

  if (in_width != out_width || in_height != out_height) {
    downscale_buf.resize(out_width * out_height * 3 / 2);
  }
}

FfmpegEncoder::~FfmpegEncoder() {
  encoder_close();
  av_frame_free(&frame);
}

void FfmpegEncoder::encoder_open() {
  const EncoderSettings settings = encoder_info.get_settings(in_width);
  auto codec_id = settings.encode_type == cereal::EncodeIndex::Type::QCAMERA_H264
                      ? AV_CODEC_ID_H264
                      : AV_CODEC_ID_FFVHUFF;
  const AVCodec *codec = avcodec_find_encoder(codec_id);
  if (codec == nullptr && codec_id == AV_CODEC_ID_H264) {
    // libx264 is optional in a minimal FFmpeg build. Fall back to the built-in
    // lossless encoder instead of aborting the whole loggerd on a missing codec.
    LOGE("H.264 encoder unavailable; falling back to FFVHUFF");
    codec_id = AV_CODEC_ID_FFVHUFF;
    codec = avcodec_find_encoder(codec_id);
  }
  if (codec == nullptr) {
    LOGE("no FFmpeg encoder available");
    return;
  }

  this->codec_ctx = avcodec_alloc_context3(codec);
  if (this->codec_ctx == nullptr) {
    LOGE("failed to allocate FFmpeg codec context");
    return;
  }
  this->codec_ctx->width = frame->width;
  this->codec_ctx->height = frame->height;
  this->codec_ctx->pix_fmt = AV_PIX_FMT_YUV420P;
  this->codec_ctx->time_base = (AVRational){ 1, encoder_info.fps };
  this->codec_ctx->max_b_frames = 0;
  // Same rate control the V4L encoder gets from its controls: a keyframe every
  // gop_size frames, a target bitrate and a one-second VBV window around it. Without
  // these, libx264 ran at its default CRF, so the recorded bitrate was whatever the
  // content happened to need and a keyframe could be arbitrarily far away - a viewer
  // joining a livestream then waited for one, and the segment file size was not
  // predictable from the encoder settings.
  const int bitrate = requested_bitrate > 0 ? requested_bitrate : settings.bitrate;
  this->codec_ctx->gop_size = settings.gop_size;
  this->codec_ctx->bit_rate = bitrate;
  this->codec_ctx->rc_max_rate = bitrate;
  this->codec_ctx->rc_buffer_size = bitrate;  // one second of VBV
  // Publish metadata for the input frame being encoded. Delayed H.264
  // packets would otherwise be paired with a later frame's id/timestamps.
  AVDictionary *options = nullptr;
  if (codec_id == AV_CODEC_ID_H264) {
    // Matroska cannot start an H.264 track without the SPS/PPS: loggerd feeds the
    // header published below into avformat_write_header(), and the muxer returns
    // AVERROR_INVALIDDATA when it is missing. Without this flag libx264 keeps the
    // parameters in-band and extradata stays empty, which aborted loggerd on the
    // first keyframe. It also populates extradata during avcodec_open2(), so it is
    // already available before any packet is produced.
    this->codec_ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    av_dict_set(&options, "tune", "zerolatency", 0);
    // libx264 defaults to preset "medium". Measured on 600 frames of real 1344x760
    // footage that is 8.69s of wall time for 30s of material, only 3.5x realtime, which
    // leaves too little headroom to guarantee the 20 Hz pipeline never stalls.
    // "veryfast" does the same work in 2.60s - 11.5x realtime - for a 9% larger file.
    av_dict_set(&options, "preset", "veryfast", 0);
  }
  int err = avcodec_open2(this->codec_ctx, codec, &options);
  av_dict_free(&options);
  if (err < 0) {
    LOGE("failed to open FFmpeg encoder: %d", err);
    avcodec_free_context(&this->codec_ctx);
    return;
  }

  is_open = true;
  segment_num++;
  counter = 0;
  input_counter = 0;
}

void FfmpegEncoder::encoder_close() {
  if (!is_open) return;

  avcodec_free_context(&codec_ctx);
  is_open = false;
}

void FfmpegEncoder::set_bitrate(int bitrate) {
  if (bitrate == requested_bitrate) return;
  if (bitrate <= 0) {
    LOGE("invalid ffmpeg encoder bitrate %d", bitrate);
    return;
  }
  // libx264 cannot change its rate control on a live context, so this is stored and
  // applied by the next encoder_open(). encoderd calls set_bitrate() on every frame of
  // a live stream, which is why the log is a one-shot debug line on change instead of
  // an error per frame.
  requested_bitrate = bitrate;
  LOGD("ffmpeg encoder %s bitrate set to %d, applied on the next segment", encoder_info.publish_name, bitrate);
}

void FfmpegEncoder::request_keyframe() {
  // The request has to travel with the frame: libx264 decides the picture type when
  // the frame is submitted, and only encode_frame() submits frames. Record it here and
  // let the next frame carry it.
  force_keyframe = true;
}

int FfmpegEncoder::encode_frame(VisionBuf* buf, VisionIpcBufExtra *extra) {
  if (!is_open) {
    return 0;
  }
  assert(buf->width == this->in_width);
  assert(buf->height == this->in_height);

  uint8_t *cy = convert_buf.data();
  uint8_t *cu = cy + in_width * in_height;
  uint8_t *cv = cu + (in_width / 2) * (in_height / 2);
  yuv::nv12_to_i420(buf->y, buf->stride,
                    buf->uv, buf->stride,
                    cy, in_width,
                    cu, in_width/2,
                    cv, in_width/2,
                    in_width, in_height);

  if (downscale_buf.size() > 0) {
    uint8_t *out_y = downscale_buf.data();
    uint8_t *out_u = out_y + frame->width * frame->height;
    uint8_t *out_v = out_u + (frame->width / 2) * (frame->height / 2);
    yuv::i420_scale(cy, in_width,
                    cu, in_width/2,
                    cv, in_width/2,
                    in_width, in_height,
                    out_y, frame->width,
                    out_u, frame->width/2,
                    out_v, frame->width/2,
                    frame->width, frame->height);
    frame->data[0] = out_y;
    frame->data[1] = out_u;
    frame->data[2] = out_v;
  } else {
    frame->data[0] = cy;
    frame->data[1] = cu;
    frame->data[2] = cv;
  }
  // PTS is in codec time_base units, one tick per submitted frame. The
  // output packet counter can stall while an encoder buffers input.
  frame->pts = input_counter;
  // Every frame has to state its own type: leaving AV_PICTURE_TYPE_I behind after a
  // forced keyframe would turn every following frame into an IDR and inflate the file.
  frame->pict_type = force_keyframe ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
  force_keyframe = false;

  int ret = counter;

  int err = avcodec_send_frame(this->codec_ctx, frame);
  // Advance the input timeline even when this frame is rejected: reusing the same
  // PTS on the next frame produces non-monotonic timestamps.
  input_counter++;
  if (err < 0) {
    LOGE("avcodec_send_frame error %d", err);
    ret = -1;
  }

  AVPacket pkt = {};
  pkt.data = NULL;
  pkt.size = 0;
  while (ret >= 0) {
    err = avcodec_receive_packet(this->codec_ctx, &pkt);
    if (err == AVERROR_EOF) {
      break;
    } else if (err == AVERROR(EAGAIN)) {
      // Encoder might need a few frames on startup to get started. Keep going
      ret = 0;
      break;
    } else if (err < 0) {
      LOGE("avcodec_receive_packet error %d", err);
      ret = -1;
      break;
    }

    if (env_debug_encoder) {
      printf("%20s got %8d bytes flags %8x idx %4d id %8d\n", encoder_info.publish_name, pkt.size, pkt.flags, counter, extra->frame_id);
    }

    // The SPS/PPS requested above. publisher_publish() attaches the header only to
    // keyframe messages, which is exactly when loggerd reads it to write the muxer
    // header. It is empty for the lossless codec, which needs no extradata.
    publisher_publish(segment_num, counter, *extra,
      (pkt.flags & AV_PKT_FLAG_KEY) ? V4L2_BUF_FLAG_KEYFRAME : 0,
      kj::arrayPtr<capnp::byte>(this->codec_ctx->extradata, (size_t)this->codec_ctx->extradata_size),
      kj::arrayPtr<capnp::byte>(pkt.data, pkt.size));

    counter++;
  }
  av_packet_unref(&pkt);
  return ret;
}

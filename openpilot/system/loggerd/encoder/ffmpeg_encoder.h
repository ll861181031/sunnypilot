#pragma once

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
}

#include "system/loggerd/encoder/encoder.h"
#include "system/loggerd/loggerd.h"

class FfmpegEncoder : public VideoEncoder {
public:
  FfmpegEncoder(const EncoderInfo &encoder_info, int in_width, int in_height);
  ~FfmpegEncoder();
  int encode_frame(VisionBuf* buf, VisionIpcBufExtra *extra);
  void encoder_open();
  void encoder_close();
  void set_bitrate(int bitrate);
  void request_keyframe();

private:
  int segment_num = -1;
  int counter = 0;
  int64_t input_counter = 0;
  bool is_open = false;

  // Applied by the next encoder_open(): libx264 cannot change its rate control on a
  // running context, and encoderd asks for a new bitrate on every livestream frame.
  int requested_bitrate = -1;
  // Set by request_keyframe(), consumed by the next encode_frame().
  bool force_keyframe = false;

  AVCodecContext *codec_ctx;
  AVFrame *frame = NULL;
  std::vector<uint8_t> convert_buf;
  std::vector<uint8_t> downscale_buf;
};

import io
import subprocess
from pathlib import Path

import pytest
import zstandard as zstd

from openpilot.cereal import log
from openpilot.common.basedir import BASEDIR
from openpilot.system.loggerd.drive_index import read_log
from openpilot.tools.lib.logreader import LogReader, decompress_stream


@pytest.fixture(scope="module")
def writer(tmp_path_factory):
  directory = tmp_path_factory.mktemp("zstd-checkpoint")
  source, binary = directory / "writer.cc", directory / "writer"
  source.write_text(r'''
#include <unistd.h>
#include <string>
#include "system/loggerd/zstd_writer.h"
int main(int argc, char **argv) {
  ZstdFileWriter writer(argv[1], 1);
  for (int i = 0; i < 65; ++i) {
    std::string message(4096, char(i + 32));
    writer.write(message.data(), message.size());
    usleep(20000);
  }
  if (argc > 2) _exit(0);
  return 0;
}
''')
  root = Path(BASEDIR)
  command = ["c++", "-std=c++17", "-I", str(root / "openpilot"), str(source),
             str(root / "openpilot/system/loggerd/zstd_writer.cc"),
             str(root / "openpilot/common/util.cc"), "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
             "-lzstd", "-lcrypto", "-lpthread", "-o", str(binary)]
  subprocess.run(command, check=True, capture_output=True, text=True)
  return binary


def completed_frames(raw):
  frames = []
  while raw:
    decoder = zstd.ZstdDecompressor().decompressobj()
    data = decoder.decompress(raw)
    if not decoder.eof:
      break
    frames.append(data)
    raw = decoder.unused_data
  return frames


def test_abrupt_exit_preserves_completed_message_checkpoints(writer, tmp_path):
  path = tmp_path / "abrupt.zst"
  subprocess.run([str(writer), str(path), "abrupt"], check=True, timeout=5)
  frames = completed_frames(path.read_bytes())
  assert frames, "abrupt exit left no completed compression frame"
  recovered = b"".join(frames)
  expected = b"".join(bytes([i + 32]) * 4096 for i in range(65))
  assert len(recovered) >= 40 * 4096
  assert len(recovered) % 4096 == 0
  assert recovered == expected[:len(recovered)]


def test_clean_close_preserves_all_messages_across_checkpoints(writer, tmp_path):
  path = tmp_path / "clean.zst"
  subprocess.run([str(writer), str(path)], check=True, timeout=5)
  raw = path.read_bytes()
  assert len(completed_frames(raw)) >= 2
  with zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
    actual = reader.read()
  assert actual == b"".join(bytes([i + 32]) * 4096 for i in range(65))
  assert decompress_stream(raw) == actual


@pytest.mark.parametrize("tail", ["complete", "truncated", "invalid"])
def test_log_index_reads_concatenated_checkpoints_and_retains_prefix(tmp_path, tail):
  messages = []
  for index in range(3):
    message = log.Event.new_message()
    message.logMonoTime = index + 1
    message.init("carState").vEgo = index
    messages.append(message.to_bytes())
  frames = [zstd.compress(message) for message in messages]
  if tail == "truncated":
    frames[-1] = frames[-1][:-1]
  elif tail == "invalid":
    frames[-1] = b"invalid frame"
  path = tmp_path / "rlog.zst"
  path.write_bytes(b"".join(frames))
  data, _, issues = read_log(path)
  assert data.startswith(b"".join(messages[:2]))
  if tail == "complete":
    assert data == b"".join(messages) and not issues
    assert [message.logMonoTime for message in LogReader(str(path))] == [1, 2, 3]
  else:
    assert issues

import os
from pathlib import Path
import signal
import subprocess

import pytest

from openpilot.cereal import log, messaging
from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware.hw import Paths
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.system.loggerd.drive_index import read_log


def test_native_logger_records_navigation_with_and_without_stopline(tmp_path, monkeypatch):
  binary = Path(BASEDIR) / 'openpilot/system/loggerd/loggerd'
  if not binary.exists():
    pytest.skip('build loggerd before running native logging integration')
  monkeypatch.setenv('LOGGERD_TEST', '1')
  monkeypatch.setenv('LOGGERD_SEGMENT_LENGTH', '60')
  with OpenpilotPrefix():
    pm = messaging.PubMaster(['yoloObjects'])
    output = tmp_path / 'logger.txt'
    with output.open('w') as stdout:
      process = subprocess.Popen([str(binary)], cwd=binary.parent, stdout=stdout,
                                 stderr=subprocess.STDOUT, start_new_session=True)
      try:
        assert pm.wait_for_readers_to_update('yoloObjects', timeout=5), output.read_text()
        for index, distance in enumerate((-1.0, 15.0)):
          event = messaging.new_message('yoloObjects')
          light = event.yoloObjects
          light.frameId = index
          light.egoLightState = 'red'
          light.egoLightSource = 'navigation'
          light.egoLightConfirmations = 2
          light.egoLightDistance = distance
          light.egoLightIsStopLine = distance > 0
          light.stopLineConfirmations = 3 if distance > 0 else 0
          light.stopLineTargetId = 'test-line' if distance > 0 else ''
          pm.send('yoloObjects', event)
        assert pm.wait_for_readers_to_update('yoloObjects', timeout=5), output.read_text()
      finally:
        if process.poll() is None:
          assert os.getpgid(process.pid) == process.pid
          os.killpg(process.pid, signal.SIGTERM)
          try:
            process.wait(timeout=5)
          except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
      assert process.returncode == 0, output.read_text()
    records = []
    for path in Path(Paths.log_root()).glob('*/rlog.zst'):
      raw, _, issues = read_log(path)
      assert not issues
      records.extend(event.yoloObjects.to_dict() for event in log.Event.read_multiple_bytes(raw)
                     if event.which() == 'yoloObjects')
    assert len(records) == 2
    assert [record['egoLightDistance'] for record in records] == [-1.0, 15.0]
    assert [record['stopLineConfirmations'] for record in records] == [0, 3]
    assert records[1]['stopLineTargetId'] == 'test-line'

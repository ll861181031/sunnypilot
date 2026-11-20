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


def test_existing_native_logger_preserves_navigation_evidence(tmp_path, monkeypatch):
  binary = Path(BASEDIR) / 'openpilot/system/loggerd/loggerd'
  if not binary.exists():
    pytest.skip('build loggerd before running native logging integration')
  monkeypatch.setenv('LOGGERD_TEST', '1')
  monkeypatch.setenv('LOGGERD_SEGMENT_LENGTH', '60')
  with OpenpilotPrefix():
    pm = messaging.PubMaster(['modelDataV2SP'])
    output = tmp_path / 'logger.txt'
    with output.open('w') as stdout:
      process = subprocess.Popen([str(binary)], cwd=binary.parent, stdout=stdout,
                                 stderr=subprocess.STDOUT, start_new_session=True)
      try:
        assert pm.wait_for_readers_to_update('modelDataV2SP', timeout=5), output.read_text()
        for index in range(2):
          event = messaging.new_message('modelDataV2SP')
          event.valid = True
          vector = [0.0] * 47
          vector[15], vector[-1] = 1.0, 0.2 * index
          event.modelDataV2SP.navInstructionValid = True
          event.modelDataV2SP.navEvidence = {
            'version': 1, 'frameId': 42 + index, 'frameIdExtra': 41 + index,
            'timestampEof': 9_000_000_000 + index * 50_000_000,
            'source': 'phone', 'instructionReason': 'accepted', 'navigation': vector,
            'receiptAvailable': True, 'receiptVerified': True, 'receiptReason': 'matched',
            'workerSession': 123456, 'workerSequence': index + 1,
            'feedNavCrc32': 0xDEADBEEF, 'stagingNavCrc32': 0xDEADBEEF,
            'gpuSampled': index == 0, 'gpuMatch': index == 0,
          }
          pm.send('modelDataV2SP', event)
        assert pm.wait_for_readers_to_update('modelDataV2SP', timeout=5), output.read_text()
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
      records.extend(event.modelDataV2SP.navEvidence.to_dict() for event in log.Event.read_multiple_bytes(raw)
                     if event.which() == 'modelDataV2SP')
    assert len(records) == 2
    assert [record['frameId'] for record in records] == [42, 43]
    assert [record['workerSequence'] for record in records] == [1, 2]
    assert [record['gpuSampled'] for record in records] == [True, False]
    assert records[0]['feedNavCrc32'] == 0xDEADBEEF
    assert records[1]['navigation'][15] == 1.0
    assert records[1]['navigation'][-1] == pytest.approx(0.2)

"""NavRuntime: the real map/instruction inputs, with the staleness rules the model expects."""
import time

import numpy as np
import pytest

from openpilot.selfdrive.modeld.nav_inputs import INSTRUCTION_TIMEOUT, MAP_TIMEOUT, NavRuntime

MAP_SHAPE = (1, 1, 256, 256)
VEC_SHAPE = (1, 47)


@pytest.fixture(autouse=True)
def isolate_phone_source(monkeypatch):
  monkeypatch.setattr("openpilot.selfdrive.modeld.nav_inputs.cplink_navi_active", lambda: False)


class _Maneuver:
  def __init__(self, type_, modifier, distance):
    self.type, self.modifier, self.distance = type_, modifier, distance


class _Instruction:
  def __init__(self, maneuvers):
    self.allManeuvers = maneuvers


class _FakeSM:
  def __init__(self, instruction=None, age=0.0):
    now = time.monotonic()
    self.valid = {"navInstruction": instruction is not None}
    self.recv_time = {"navInstruction": now - age}
    self._instruction = instruction or _Instruction([])

  def __getitem__(self, key):
    assert key == "navInstruction"
    return self._instruction


class TestNavRuntime:
  def test_no_sources_returns_none(self):
    runtime = NavRuntime()
    runtime._client_failed = True  # no mapsd on this host
    image, vector = runtime.update(_FakeSM(), 10.0, MAP_SHAPE, VEC_SHAPE)
    assert image is None and vector is None

  def test_fresh_map_is_returned(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    runtime.map_image = np.full(MAP_SHAPE, 7, dtype=np.uint8)
    runtime.map_at = time.monotonic()
    image, _ = runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert image is not None and int(image.max()) == 7

  def test_stale_map_is_dropped(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    runtime.map_image = np.full(MAP_SHAPE, 7, dtype=np.uint8)
    runtime.map_at = time.monotonic() - (MAP_TIMEOUT + 1.0)
    image, _ = runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert image is None

  def test_instruction_becomes_the_47_vector(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    instruction = _Instruction([_Maneuver("turn", "right", 300.0)])
    _, vector = runtime.update(_FakeSM(instruction), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert vector is not None and vector.shape == VEC_SHAPE
    assert vector[0, 10] == 1.0 and vector[0, -1] == 3.0

  def test_travel_shortens_the_instruction_distance(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    instruction = _Instruction([_Maneuver("turn", "right", 300.0)])
    _, fresh = runtime.update(_FakeSM(instruction, age=0.0), 0.0, MAP_SHAPE, VEC_SHAPE)
    _, aged = runtime.update(_FakeSM(instruction, age=0.2), 10.0, MAP_SHAPE, VEC_SHAPE)
    assert fresh[0, -1] == 3.0
    assert aged[0, -1] == pytest.approx(2.98)  # 300 m - 10 m/s * 0.2 s, /100

  def test_stale_instruction_is_dropped(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    instruction = _Instruction([_Maneuver("turn", "right", 300.0)])
    _, vector = runtime.update(_FakeSM(instruction, age=INSTRUCTION_TIMEOUT + 1.0), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert vector is None

  def test_invalid_instruction_is_dropped(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    sm = _FakeSM()
    sm.valid["navInstruction"] = False
    _, vector = runtime.update(sm, 0.0, MAP_SHAPE, VEC_SHAPE)
    assert vector is None


class TestNavStatus:
  """modelDataV2SP carries these fields; a drive is judged from the log with them."""

  def test_nothing_seen_reports_invalid_with_zero_age(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    status = runtime.status()
    assert status["map_valid"] is False and status["instruction_valid"] is False
    assert status["map_age"] == 0.0
    assert status["instruction_age"] < 0.1  # FakeSM stamps recv_time even with no instruction

  def test_fresh_map_reports_valid(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    runtime.map_image = np.zeros(MAP_SHAPE, dtype=np.uint8)
    runtime.map_at = time.monotonic()
    runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    status = runtime.status()
    assert status["map_valid"] is True and status["map_age"] < 0.1

  def test_stale_map_reports_invalid_with_growing_age(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    runtime.map_image = np.zeros(MAP_SHAPE, dtype=np.uint8)
    runtime.map_at = time.monotonic() - (MAP_TIMEOUT + 2.0)
    runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    status = runtime.status()
    assert status["map_valid"] is False
    assert status["map_age"] > MAP_TIMEOUT

  def test_instruction_age_survives_becoming_stale(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    instruction = _Instruction([_Maneuver("turn", "right", 100.0)])
    runtime.update(_FakeSM(instruction, age=0.0), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert runtime.status()["instruction_valid"] is True
    runtime.update(_FakeSM(instruction, age=INSTRUCTION_TIMEOUT + 1.0), 0.0, MAP_SHAPE, VEC_SHAPE)
    status = runtime.status()
    assert status["instruction_valid"] is False
    assert status["instruction_age"] >= INSTRUCTION_TIMEOUT


class TestModelDataV2SPStatus:
  """Schema contract: these four fields are how a drive is audited from the log alone."""

  def test_nav_status_round_trips(self):
    import openpilot.cereal.messaging as messaging
    from openpilot.cereal import log
    msg = messaging.new_message("modelDataV2SP")
    msg.modelDataV2SP.navMapValid = True
    msg.modelDataV2SP.navInstructionValid = False
    msg.modelDataV2SP.navMapAge = 0.05
    msg.modelDataV2SP.navInstructionAge = 3.0
    with log.Event.from_bytes(msg.to_bytes()) as parsed:
      assert parsed.modelDataV2SP.navMapValid is True
      assert parsed.modelDataV2SP.navInstructionValid is False
      assert parsed.modelDataV2SP.navMapAge == pytest.approx(0.05, abs=1e-6)
      assert parsed.modelDataV2SP.navInstructionAge == pytest.approx(3.0, abs=1e-6)

  def test_defaults_are_false_so_other_models_stay_unlabelled(self):
    import openpilot.cereal.messaging as messaging
    msg = messaging.new_message("modelDataV2SP")
    with __import__("openpilot.cereal", fromlist=["log"]).log.Event.from_bytes(msg.to_bytes()) as parsed:
      assert parsed.modelDataV2SP.navMapValid is False
      assert parsed.modelDataV2SP.navInstructionValid is False


class TestFillNavStatus:
  """The publish helper, including the no-nav case that must clear the fields."""

  class _Target:
    navMapValid = True
    navInstructionValid = True
    navMapAge = 9.0
    navInstructionAge = 9.0

  def test_writes_the_status(self):
    from openpilot.selfdrive.modeld.modeld import fill_nav_status
    target = self._Target()
    fill_nav_status(target, {"map_valid": True, "instruction_valid": False,
                             "map_age": 0.2, "instruction_age": 4.0})
    assert target.navMapValid is True and target.navInstructionValid is False
    assert target.navMapAge == pytest.approx(0.2) and target.navInstructionAge == pytest.approx(4.0)

  def test_no_nav_model_clears_previous_values(self):
    from openpilot.selfdrive.modeld.modeld import fill_nav_status
    target = self._Target()
    fill_nav_status(target, None)
    assert target.navMapValid is False and target.navInstructionValid is False
    assert target.navMapAge == 0.0 and target.navInstructionAge == 0.0


class _FakeBuf:
  def __init__(self, image):
    self.width, self.height = image.shape[-1], image.shape[-2]
    self.data = image.tobytes()


class _FakeClient:
  def __init__(self, image):
    self.image = image

  def is_connected(self):
    return True

  def connect(self, blocking):
    return True

  def recv(self, timeout):
    return _FakeBuf(self.image)


class TestMapBlackFrame:
  """mapsd's "no map" frame is all zeros and carries no timestamp the binding can read."""

  def _runtime(self, image):
    runtime = NavRuntime()
    runtime.client = _FakeClient(image)
    return runtime

  def test_black_frame_is_not_a_valid_map(self):
    runtime = self._runtime(np.zeros(MAP_SHAPE, dtype=np.uint8))
    image, _ = runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert image is None
    assert runtime.status()["map_valid"] is False

  def test_ego_arrow_makes_it_valid(self):
    frame = np.zeros(MAP_SHAPE, dtype=np.uint8)
    frame[0, 0, 180, 128] = 255  # the white ego arrow
    runtime = self._runtime(frame)
    image, _ = runtime.update(_FakeSM(), 0.0, MAP_SHAPE, VEC_SHAPE)
    assert image is not None and image.dtype == np.uint8
    assert runtime.status()["map_valid"] is True


class TestReportedAges:
  def test_ages_are_never_negative(self):
    runtime = NavRuntime()
    runtime._client_failed = True
    runtime.map_image = np.full(MAP_SHAPE, 3, dtype=np.uint8)
    runtime.map_at = time.monotonic()
    runtime.update(_FakeSM(_Instruction([_Maneuver("turn", "right", 100.0)]), age=0.0),
                   0.0, MAP_SHAPE, VEC_SHAPE)
    status = runtime.status()
    assert status["map_age"] >= 0.0
    assert status["instruction_age"] >= 0.0

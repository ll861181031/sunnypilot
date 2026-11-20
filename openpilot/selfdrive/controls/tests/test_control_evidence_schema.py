import base64

import zstandard as zstd

from openpilot.cereal import log, messaging
from openpilot.system.loggerd.drive_index import read_log


def test_navigation_and_stopline_evidence_survives_log_roundtrip(tmp_path):
  event = messaging.new_message('yoloObjects')
  light = event.yoloObjects
  light.egoLightState = 'red'
  light.egoLightSource = 'navigation'
  light.egoLightSession = 'session-a'
  light.egoLightSequence = 12
  light.egoLightReceivedMonoTime = 10_000_000_000
  light.egoLightIntersectionId = 'junction-a'
  light.egoLightDirection = 'straight'
  light.egoLightRelevant = True
  light.egoLightConfirmations = 2
  light.stopLineConfirmations = 3
  light.stopLineTargetId = 'line-a'
  light.stopLineFrameId = 101
  light.stopLineCaptureMonoTime = 9_990_000_000
  light.stopLineDetectedMonoTime = 10_010_000_000
  light.stopLineReason = 'confirmed'
  path = tmp_path / 'rlog.zst'
  path.write_bytes(zstd.compress(event.to_bytes()))
  raw, _, issues = read_log(path)
  assert not issues
  with log.Event.from_bytes(raw) as decoded:
    assert decoded.yoloObjects.egoLightConfirmations == 2
    assert decoded.yoloObjects.stopLineConfirmations == 3
    assert decoded.yoloObjects.stopLineCaptureMonoTime < decoded.yoloObjects.stopLineDetectedMonoTime
    assert decoded.yoloObjects.egoLightIntersectionId == 'junction-a'


def test_lead_and_plan_evidence_has_unknown_defaults_and_roundtrips():
  event = messaging.new_message('radarState')
  assert str(event.radarState.leadOne.observationState) == 'unknown'
  assert event.radarState.leadOne.visionTrackId == 0
  event.radarState.leadOne.observationState = 'uncertain'
  event.radarState.leadOne.observationAge = 0.6
  event.radarState.leadOne.visionTrackId = 2
  event.radarState.leadOne.releaseReason = 'lost'
  with log.Event.from_bytes(event.to_bytes()) as decoded:
    assert str(decoded.radarState.leadOne.observationState) == 'uncertain'
    assert decoded.radarState.leadOne.releaseReason == 'lost'
  plan = messaging.new_message('longitudinalPlanSP').longitudinalPlanSP
  assert plan.trafficLightState == ''
  assert not plan.turnGuardActive and not plan.turnGuardEnforced
  assert not plan.leadGuardActive and not plan.leadGuardEnforced


def test_pre_extension_messages_decode_evidence_as_unknown():
  # Serialized with the pre-change schema in an isolated Python process.
  legacy = {
    'yoloObjects': ''.join([
      'AAAAAAwAAAAAAAAAAgABABXNWwcAAAAAlwAAAAAAAAAAAAAAAwAEAAAAAAAAAAAAAAAAAAYAAAAAAHBBAAAAAAAAAAAAAAAA',
      'AAAAAAAAAAAAAAAAAAAAAAEAAAAiAAAAcmVkAAAAAAA=',
    ]),
    'radarState': ''.join([
      'AAAAABYAAAAAAAAAAgABABXNWwcAAAAADAAAAAAAAAAAAAAABQAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
      'AAAAAAAAAAAAAAAAAAAAABAAAAAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPBBAAAAAAAAAAAAAAAA',
      'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAAA==',
    ]),
    'longitudinalPlanSP': 'AAAAAAsAAAAAAAAAAgABABXNWwcAAAAAawAAAAAAAAAAAAAAAgAFAAAAAAAAAAAAAACAvwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
  }
  with log.Event.from_bytes(base64.b64decode(legacy['yoloObjects'])) as event:
    assert event.yoloObjects.egoLightState == 'red'
    assert event.yoloObjects.egoLightFrames == 6
    assert event.yoloObjects.egoLightSource == ''
    assert not event.yoloObjects.egoLightRelevant
    assert event.yoloObjects.egoLightConfirmations == 0
    assert event.yoloObjects.stopLineConfirmations == 0
  with log.Event.from_bytes(base64.b64decode(legacy['radarState'])) as event:
    assert event.radarState.leadOne.present
    assert str(event.radarState.leadOne.observationState) == 'unknown'
    assert event.radarState.leadOne.visionTrackId == 0
  with log.Event.from_bytes(base64.b64decode(legacy['longitudinalPlanSP'])) as event:
    assert event.longitudinalPlanSP.aTarget == -1
    assert event.longitudinalPlanSP.trafficLightState == ''
    assert not event.longitudinalPlanSP.turnGuardEnforced
    assert not event.longitudinalPlanSP.leadGuardEnforced

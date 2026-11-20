"""Real navigation inputs for the nav driving model.

Two sources, both optional and independently stale-checked:

* the map image from `mapsd` (VisionIPC, 256x256 uint8 grayscale), and
* the 47-float instruction vector built from `navInstruction.allManeuvers`.

Upstream comma keeps the last map image until it is 0.5 s old and drops the vector when
the instruction is stale; the model was trained with explicit zeros for missing inputs,
so this returns None rather than a made-up frame.
"""
import math
import time
import uuid

import numpy as np

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.selfdrive.navigation.cplink import amap_root_nav_enabled, cplink_navi_active
from openpilot.selfdrive.navigation.helpers import _field, navigation_input, valhalla_type
from openpilot.selfdrive.navigation.nav_map_contract import (LocalNavdInstructionContext,
  LocalNavdPhonePoseContext, LocalNavdRouteContext, MapFrameMatcher, MapFrameMetadata, MapPixels, RouteRejected,
  WireRouteBinding, current_local_binding, local_phone_observation)

MAP_TIMEOUT = 0.5  # s, same as the upstream map-validity gate
INSTRUCTION_TIMEOUT = 0.5  # s


class NavRuntime:
  def __init__(self):
    self.client = None
    self._client_failed = False
    self.map_image: np.ndarray | None = None
    self.map_at = 0.0
    self.map_valid = False
    self.instruction_valid = False
    self.instruction_at = 0.0
    self.map_age = 0.0
    self.instruction_age = 0.0
    self._map_poll = 1
    self._cplink_active = False
    self._amap_root_active = False
    self._source_changed_at = 0.0
    self._accept_after = 0.0
    self._map_received_at = 0.0
    self._map_frame_id = 0
    self._map_poll_reason = "notPolled"
    self._evidence = {}
    self._phone_route_context = None
    self._phone_matcher = None
    self._phone_frames = {}
    self._phone_map_reason = "phoneMapSuppressed"
    self._phone_revoke_after_ns = 0
    self._accepted_phone_metadata = None
    self._last_update_at = 0.0
    self._phone_route_after = 0.0
    self._history_model = None
    self._history_root_active = None
    self._navigation_mode_reset_pending = False
    self._instruction_diagnostic = {}
    self._history_keys = {"map": None, "navigation": None}
    self._history_epochs = {"map": 0, "navigation": 0}
    self._native_route_key = None
    self._zero_map = self._zero_vector = None

  def _ensure_client(self):
    if self.client is None and not self._client_failed:
      try:
        from msgq.visionipc import VisionIpcClient
        self.client = VisionIpcClient("mapsd", VisionStreamType.VISION_STREAM_MAP, True)
      except Exception:
        # No mapsd (or no VisionIPC on this host): keep running on zeros.
        self._client_failed = True
        self.client = None
    return self.client

  def _poll_map(self, map_shape) -> None:
    client = self._ensure_client()
    if client is None:
      self._map_poll_reason = "clientUnavailable"
      return
    try:
      # VisionIPC needs an explicit non-blocking connect; mapsd may start after modeld,
      # so keep retrying instead of latching a failure.
      if not client.is_connected():
        if not client.connect(False):
          self._map_poll_reason = "notConnected"
          return
        self._phone_frames.clear()
        self._phone_matcher = MapFrameMatcher.for_link(uuid.uuid4().hex, connected_mono_time_ns=time.monotonic_ns())
      elif self._cplink_active and self._phone_matcher is None:
        self._phone_matcher = MapFrameMatcher.for_link(uuid.uuid4().hex, connected_mono_time_ns=time.monotonic_ns())
      buf = client.recv(0)
    except Exception:
      self._client_failed = True
      self._map_poll_reason = "receiveError"
      return
    if buf is None:
      self._map_poll_reason = "noFrame"
      return
    # The timestamps belong to the client metadata, not to the image buffer. Reject
    # frames queued before a source transition even if they were only received now.
    produced = getattr(client, "timestamp_eof", 0) / 1e9
    now = time.monotonic()
    if produced and not 0.0 <= now - produced < MAP_TIMEOUT:
      self._map_poll_reason = "futureProduced" if produced > now else "staleProduced"
      return
    if self._source_changed_at and produced < self._accept_after:
      self._map_poll_reason = "sourceSwitch"
      return
    if buf.width != map_shape[-1] or buf.height != map_shape[-2]:
      self._map_poll_reason = "shapeMismatch"
      return
    image = np.frombuffer(buf.data, dtype=np.uint8, count=buf.width * buf.height).reshape(map_shape).copy()
    if not image.any():
      # mapsd sends an all-zero frame when it has no usable map. A real render always
      # contains the white ego arrow, so black means "no map" and must not be counted
      # as a valid input - otherwise modelV2SP claims navigation on frames the model
      # actually drove with zeros.
      self.map_image = None
      self._phone_frames.clear()
      if self._cplink_active:
        self._phone_revoke_after_ns = max(self._phone_revoke_after_ns, round((produced or now) * 1e9))
      self._map_poll_reason = "emptyMap"
      return
    if self._cplink_active:
      try:
        frame = MapPixels(int(getattr(client, "frame_id", 0)), int(getattr(client, "timestamp_sof", 0)),
                          int(getattr(client, "timestamp_eof", 0)), buf.width, buf.height, image.tobytes())
      except RouteRejected:
        self._map_poll_reason = "invalidFrameMetadata"
        return
      if frame.timestamp_eof_ns <= self._phone_revoke_after_ns:
        self._map_poll_reason = "frameBeforeRevocation"
        return
      self._phone_frames[(frame.frame_id, frame.timestamp_eof_ns)] = frame
      while len(self._phone_frames) > 8:
        self._phone_frames.pop(next(iter(self._phone_frames)))
      self._map_poll_reason = "awaitingBinding"
      return
    self.map_image = image
    self.map_at = produced or now
    self._map_received_at = now
    self._map_frame_id = int(getattr(client, "frame_id", 0))
    self._map_poll_reason = "accepted"

  def _update_phone_route(self, sm, now: float) -> None:
    if getattr(sm, "updated", {}).get("navRoute", False):
      previous = self._phone_route_context
      self._phone_route_context = None
      try:
        context = LocalNavdRouteContext.from_submaster(sm, now_ns=round(now * 1e9))
        if context.produced_mono_time_ns < round(self._phone_route_after * 1e9):
          raise RouteRejected("route_before_source_switch")
        self._phone_route_context = context
      except (RouteRejected, AttributeError, KeyError, TypeError, ValueError):
        self.map_image = None
        self._phone_frames.clear()
        self._phone_map_reason = "localRouteRejected"
        return
      if previous is not None and previous.binding.route_key() != context.binding.route_key():
        self.map_image = None
        self._phone_frames.clear()

  def _phone_map(self, sm, now: float, map_shape):
    now_ns = round(now * 1e9)
    self._accepted_phone_metadata = None
    matcher = self._phone_matcher
    if getattr(sm, "updated", {}).get("navMapFrameSP", False):
      try:
        if not sm.valid.get("navMapFrameSP", False):
          raise RouteRejected("map_sidecar_invalid")
        received = round(sm.recv_time.get("navMapFrameSP", 0.0) * 1e9)
        produced = sm.logMonoTime.get("navMapFrameSP", 0)
        if not (0 < produced <= now_ns and 0 <= now_ns - produced < int(MAP_TIMEOUT * 1e9)):
          raise RouteRejected("map_sidecar_stale_or_future")
        metadata = MapFrameMetadata.from_dict(sm["navMapFrameSP"].to_dict())
        if not metadata.valid:
          self._phone_revoke_after_ns = max(self._phone_revoke_after_ns, min(now_ns, max(produced, metadata.timestamp_eof_ns)))
          raise RouteRejected("map_sidecar_invalid")
        if matcher is not None:
          matcher.add_metadata(metadata, received_mono_time_ns=received)
      except (RouteRejected, AttributeError, KeyError, TypeError, ValueError):
        self._phone_revoke_after_ns = max(self._phone_revoke_after_ns, min(now_ns, sm.logMonoTime.get("navMapFrameSP", now_ns)))
        self._phone_frames.clear()
        self.map_image = None
        self._phone_map_reason = "mapSidecarRejected"
        return None
    try:
      instruction = local_phone_observation(sm, self._phone_route_context, now_ns=now_ns)
    except (RouteRejected, AttributeError, KeyError, TypeError, ValueError):
      self._phone_map_reason = "phoneMapSuppressed" if self._phone_route_context is None else "instructionBindingRejected"
      return None
    if matcher is None:
      self._phone_map_reason = "phoneMapSuppressed" if self._phone_route_context is None else "mapLinkUnavailable"
      return None
    self._phone_map_reason = "mapFrameMissing"
    for key, frame in sorted(self._phone_frames.items(), key=lambda item: item[1].timestamp_eof_ns, reverse=True):
      if frame.timestamp_eof_ns <= self._phone_revoke_after_ns or not 0 <= now_ns - frame.timestamp_eof_ns < int(MAP_TIMEOUT * 1e9):
        self._phone_frames.pop(key, None)
        continue
      result = matcher.match_local(frame, route_context=self._phone_route_context,
        instruction_context=instruction, now_ns=now_ns)
      self._phone_map_reason = result.reason
      if matcher.requires_reconnect:
        self.client = None
        self._client_failed = False
        self._phone_matcher = None
        self._phone_frames.clear()
        self.map_image = None
        return None
      if result.accepted:
        image = np.frombuffer(frame.pixels, dtype=np.uint8).reshape(map_shape).copy()
        self.map_image = image
        self.map_at = frame.timestamp_eof_ns / 1e9
        self._map_received_at = now
        self._map_frame_id = frame.frame_id
        self._accepted_phone_metadata = result.metadata
        return image
    return None

  def _phone_instruction(self, sm, now: float, v_ego: float, vec_shape) -> np.ndarray | None:
    received = sm.recv_time.get("navInstruction", 0.0)
    produced_ns = getattr(sm, "logMonoTime", {}).get("navInstruction", int(received * 1e9))
    produced = produced_ns / 1e9
    diagnostic = {
      "instructionReason": "missing", "maneuverCount": 0, "selectedManeuverIndex": -1,
      "selectedCommand": -1, "rawDistance": 0.0, "adjustedDistance": 0.0,
      "traveledDistance": 0.0, "maneuverType": "", "maneuverModifier": "",
    }
    self._instruction_diagnostic = diagnostic

    # Read only while the message is valid. Invalid Cap'n Proto payloads can contain
    # arbitrary values, so diagnosing them must not turn into an input decision.
    if not sm.valid.get("navInstruction", False):
      diagnostic["instructionReason"] = "invalidMessage"
      return None

    if not received and not produced:
      diagnostic["instructionReason"] = "missing"
      return None
    if now < received:
      diagnostic["instructionReason"] = "futureReceived"
      return None
    if now < produced:
      diagnostic["instructionReason"] = "futureProduced"
      return None
    if now - received >= INSTRUCTION_TIMEOUT:
      diagnostic["instructionReason"] = "staleReceived"
      return None
    if now - produced >= INSTRUCTION_TIMEOUT:
      diagnostic["instructionReason"] = "staleProduced"
      return None

    try:
      binding_version = sm["navInstruction"].routeBinding.version
    except (AttributeError, KeyError):
      binding_version = 0
    if binding_version == 0:
      diagnostic["instructionReason"] = "legacyBinding"
      return None
    try:
      instruction = LocalNavdInstructionContext.from_submaster(sm, now_ns=round(now * 1e9))
      current_local_binding(self._phone_route_context, instruction, now_ns=round(now * 1e9))
    except RouteRejected as error:
      if error.reason == "instruction_route_mismatch":
        reason = "instructionRouteMismatch"
      elif error.reason.endswith("_stale_or_future"):
        reason = "staleProduced"
      elif error.reason == "legacy_binding":
        reason = "legacyBinding"
      else:
        reason = error.reason
      diagnostic["instructionReason"] = reason
      return None
    except (AttributeError, KeyError, TypeError, ValueError):
      diagnostic["instructionReason"] = "instructionBindingInvalid"
      return None

    # Validate time and route identity before decoding maneuver fields. Invalid or
    # legacy Cap'n Proto payloads may otherwise make diagnostic reads an input path.
    try:
      maneuvers = sm["navInstruction"].allManeuvers
      diagnostic["maneuverCount"] = len(maneuvers)
      if len(maneuvers):
        first = maneuvers[0]
        raw_distance = float(_field(first, "distance", 0.0))
        diagnostic.update(maneuverType=str(_field(first, "type")),
                          maneuverModifier=str(_field(first, "modifier")),
                          rawDistance=raw_distance,
                          adjustedDistance=raw_distance)
    except (AttributeError, KeyError, TypeError, ValueError):
      diagnostic["instructionReason"] = "diagnosticError"
      return None

    if len(maneuvers) != 1:
      diagnostic["instructionReason"] = "invalidManeuverCount"
      return None
    maneuver = maneuvers[0]
    maneuver_type = str(_field(maneuver, "type"))
    modifier = str(_field(maneuver, "modifier"))
    command = valhalla_type(maneuver_type, modifier)
    # navigation_input() treats an unknown modifier as straight. Root input cannot use
    # that fallback: only actions verified from native AMap data may pass.
    verified_commands = {
      ("turn", "left", 15), ("turn", "right", 10),
      ("turn", "slight right", 9), ("turn", "sharp right", 11),
      ("turn", "uturn", 13),
      ("roundabout", "", 26), ("exit roundabout", "", 27),
    }
    if (maneuver_type, modifier, command) not in verified_commands:
      diagnostic["instructionReason"] = "unsupportedManeuver"
      return None
    try:
      distance = float(_field(maneuver, "distance", 0.0))
    except (TypeError, ValueError, OverflowError):
      diagnostic["instructionReason"] = "nonfiniteDistance"
      return None
    if not math.isfinite(distance):
      diagnostic["instructionReason"] = "nonfiniteDistance"
      return None
    traveled = max(v_ego, 0.0) * max(now - produced, 0.0)
    adjusted = distance - traveled
    diagnostic.update(adjustedDistance=adjusted, traveledDistance=traveled,
                      selectedManeuverIndex=0, selectedCommand=command)
    if adjusted < 0:
      diagnostic["instructionReason"] = "passedManeuvers"
      return None
    vector = np.zeros(vec_shape, dtype=np.float32)
    vector[0, command] = 1.0
    vector[0, -1] = adjusted / 100.0
    diagnostic["instructionReason"] = "accepted"
    return vector

  def update(self, sm, v_ego: float, map_shape, vec_shape) -> tuple[np.ndarray | None, np.ndarray | None]:
    now = time.monotonic()
    self._amap_root_active = amap_root_nav_enabled()
    phone_active = cplink_navi_active()
    if phone_active != self._cplink_active:
      self._cplink_active = phone_active
      self._source_changed_at = now
      self._phone_route_after = self._last_update_at or now - INSTRUCTION_TIMEOUT
      self._accept_after = now + max(MAP_TIMEOUT, INSTRUCTION_TIMEOUT)
      self.map_image = None
      self._phone_route_context = None
      self._phone_frames.clear()
      self._phone_matcher = None
      self._accepted_phone_metadata = None
      self._phone_revoke_after_ns = 0
    self._map_poll = (self._map_poll + 1) % 2
    if phone_active:
      self._update_phone_route(sm, now)
    if (phone_active or self._map_poll == 0) and now >= self._accept_after:
      self._poll_map(map_shape)
    # Read the clock after polling: `_poll_map` stamps `map_at` itself, so measuring
    # before it produced a negative map age on the very frame a map arrived.
    now = time.monotonic()
    image = self.map_image if (not phone_active and self.map_image is not None
                               and now - self.map_at < MAP_TIMEOUT) else None
    if phone_active and now >= self._accept_after:
      image = self._phone_map(sm, now, map_shape)

    vector = None
    if self._amap_root_active:
      vector = self._phone_instruction(sm, now, v_ego, vec_shape)
    elif sm.valid.get("navInstruction", False):
      received = sm.recv_time.get("navInstruction", 0.0)
      produced = getattr(sm, "logMonoTime", {}).get("navInstruction", int(received * 1e9)) / 1e9
      if (received >= self._accept_after and produced >= self._accept_after
          and 0.0 <= now - received < INSTRUCTION_TIMEOUT and 0.0 <= now - produced < INSTRUCTION_TIMEOUT):
        traveled = max(v_ego, 0.0) * (now - produced)
        candidate = navigation_input(sm["navInstruction"].allManeuvers, traveled)
        if candidate[:-1].any():
          vector = candidate.reshape(vec_shape)

    # Status for `modelDataV2SP`: the drive can then be checked from the log alone. The ages
    # are measured from the last *accepted* input, so a stale source shows up as an
    # increasing age next to a false flag instead of resetting to zero.
    self.map_valid = image is not None
    self.instruction_valid = vector is not None
    if self.instruction_valid:
      self.instruction_at = now
    # Age keeps growing while a map is stale (that is how the log shows a dead source);
    # only a never-seen map reports zero.
    self.map_age = max(now - self.map_at, 0.0) if self.map_image is not None else 0.0
    received = sm.recv_time.get("navInstruction")
    produced = getattr(sm, "logMonoTime", {}).get("navInstruction", 0) / 1e9
    self.instruction_age = max(now - (produced or received), 0.0) if produced or received else 0.0
    try:
      self._record_evidence(sm, v_ego, now, image, vector)
    except Exception:
      self._evidence = {"instructionReason": "diagnosticError", "mapReason": "diagnosticError"}
    self._last_update_at = now
    return image, vector

  def _instruction_history_key(self, sm):
    try:
      return WireRouteBinding.from_dict(sm["navInstruction"].routeBinding.to_dict()).route_key()
    except (RouteRejected, AttributeError, KeyError, TypeError, ValueError):
      return None

  def inputs_for_model(self, sm, v_ego: float, model) -> dict[str, np.ndarray]:
    map_shape, vec_shape = model.input_shapes["nav_map"], model.input_shapes["navigation"]
    image, vector = self.update(sm, v_ego, map_shape, vec_shape)
    supported = (getattr(model, "nav_reset_supported", False) is True
                 and callable(getattr(model, "request_nav_reset", None)))
    if self._cplink_active and image is not None and not supported:
      image = None
      self.map_valid = False
      self._evidence.update(mapReason="navigationResetUnavailable", mapCorrelationReason="navigationResetUnavailable",
                            mapBinding={}, mapRendererInstanceId="", mapPixelCrc32=0)

    if self._history_model is not model:
      self._history_model = model
      self._history_keys = {"map": None, "navigation": None}
      self._history_root_active = None
      self._navigation_mode_reset_pending = False
    if self._history_root_active != self._amap_root_active and (self._history_root_active is not None or self._amap_root_active):
      # Clear any previously unbound maneuver history once, including a new Root
      # model. Keep the request pending until the worker advertises reset support.
      self._navigation_mode_reset_pending = True
    self._history_root_active = self._amap_root_active
    if not self._cplink_active and getattr(sm, "updated", {}).get("navRoute", False):
      try:
        coordinates = sm["navRoute"].coordinates
        if len(coordinates) > 100_000:
          raise ValueError("route_point_limit")
        self._native_route_key = tuple((point.longitude, point.latitude) for point in coordinates)
      except (AttributeError, KeyError, TypeError, ValueError):
        self._native_route_key = None
    source = (self._cplink_active, self._source_changed_at)
    context = self._phone_route_context
    route = context.binding.route_key() if self._cplink_active and context is not None else self._native_route_key
    keys = {"map": (source, route) if image is not None else None,
            "navigation": (source, self._instruction_history_key(sm), self._native_route_key if not self._cplink_active else None)
            if vector is not None else None}
    for channel, key in keys.items():
      previous = self._history_keys[channel]
      changed = previous != key and (previous is not None or key is not None)
      changed |= channel == "navigation" and self._navigation_mode_reset_pending
      if changed and supported:
        self._history_epochs[channel] += 1
        model.request_nav_reset(**{channel + "_epoch": self._history_epochs[channel]})
        if channel == "navigation":
          self._navigation_mode_reset_pending = False
      self._history_keys[channel] = key

    if self._zero_map is None or self._zero_map.shape != tuple(map_shape):
      self._zero_map = np.zeros(map_shape, np.uint8)
    if self._zero_vector is None or self._zero_vector.shape != tuple(vec_shape):
      self._zero_vector = np.zeros(vec_shape, np.float32)
    return {"nav_map": image if image is not None else self._zero_map,
            "navigation": vector if vector is not None else self._zero_vector}

  def _record_evidence(self, sm, v_ego: float, now: float, image, vector) -> None:
    # Diagnostics observe the result after the existing gates. They never choose an
    # input, retain a rejected maneuver or change the navigation encoding.
    received = sm.recv_time.get("navInstruction", 0.0)
    produced_ns = getattr(sm, "logMonoTime", {}).get("navInstruction", int(received * 1e9))
    produced = produced_ns / 1e9
    evidence = {
      "source": "phone" if self._cplink_active else "native",
      "sourceChangedMonoTime": int(self._source_changed_at * 1e9),
      "acceptAfterMonoTime": int(self._accept_after * 1e9),
      "sourceTransition": now < self._accept_after,
      "instructionMonoTime": int(produced_ns),
      "instructionReceivedMonoTime": int(received * 1e9),
      "instructionReason": "missing",
      "mapReason": "missing",
      "mapPollReason": self._map_poll_reason,
      "mapMonoTime": int(self.map_at * 1e9),
      "mapFrameId": self._map_frame_id,
      "mapReceivedMonoTime": int(self._map_received_at * 1e9),
      "mapBinding": {},
      "mapRendererInstanceId": "",
      "mapPixelCrc32": 0,
      "mapCorrelationReason": self._phone_map_reason if self._cplink_active else "notApplicable",
      "maneuverCount": 0,
      "selectedManeuverIndex": -1,
      "selectedCommand": -1,
      "rawDistance": 0.0,
      "adjustedDistance": 0.0,
      "traveledDistance": 0.0,
      "maneuverType": "",
      "maneuverModifier": "",
      "nextManeuverType": "",
      "nextManeuverModifier": "",
      "nextManeuverDistance": 0.0,
      "phonePositionQuality": "unknown",
      "phoneGpsAccuracyM": 0.0,
      "phoneNativePoseErrorM": 0.0,
      "controlInputAllowed": False,
    }
    if self._amap_root_active:
      evidence.update(self._instruction_diagnostic)
    if self._cplink_active:
      evidence["mapReason"] = ("phoneMapSuppressed" if self._phone_route_context is None
                               else "sourceSwitch" if now < self._accept_after else self._phone_map_reason)
      if isinstance(self._phone_route_context, LocalNavdPhonePoseContext):
        evidence.update(phonePositionQuality=self._phone_route_context.quality_reason,
                        phoneGpsAccuracyM=self._phone_route_context.horizontal_accuracy_m,
                        phoneNativePoseErrorM=self._phone_route_context.native_pose_error_m,
                        controlInputAllowed=False)
      metadata = self._accepted_phone_metadata
      if image is not None and metadata is not None:
        evidence.update(mapBinding=metadata.route_binding.to_dict(), mapRendererInstanceId=metadata.renderer_instance_id,
                        mapPixelCrc32=metadata.image_crc32, mapCorrelationReason="accepted")
    elif now < self._accept_after:
      evidence["mapReason"] = "sourceSwitch"
    elif image is not None:
      evidence["mapReason"] = "accepted"
    elif self.map_image is not None:
      evidence["mapReason"] = "staleProduced"
    else:
      evidence["mapReason"] = "missing" if self._map_poll_reason == "accepted" else self._map_poll_reason

    if self._amap_root_active:
      self._evidence = evidence
      return

    # Invalid messages may contain arbitrary fields. Logging them must not turn a
    # normally rejected message into an exception in the inference loop.
    try:
      maneuvers = sm["navInstruction"].allManeuvers
      evidence["maneuverCount"] = len(maneuvers)
      if len(maneuvers):
        evidence.update(self._maneuver_evidence(maneuvers[0], 0.0))
      if len(maneuvers) > 1:
        evidence.update({
          "nextManeuverType": str(_field(maneuvers[1], "type")),
          "nextManeuverModifier": str(_field(maneuvers[1], "modifier")),
          "nextManeuverDistance": float(_field(maneuvers[1], "distance", 0.0)),
        })
      if not received and not produced:
        reason = "missing"
      elif not sm.valid.get("navInstruction", False):
        reason = "invalidMessage"
      elif received < self._accept_after or produced < self._accept_after:
        reason = "sourceSwitch"
      elif now < received:
        reason = "futureReceived"
      elif now < produced:
        reason = "futureProduced"
      elif now - received >= INSTRUCTION_TIMEOUT:
        reason = "staleReceived"
      elif now - produced >= INSTRUCTION_TIMEOUT:
        reason = "staleProduced"
      else:
        traveled = max(v_ego, 0.0) * (now - produced)
        evidence["traveledDistance"] = traveled
        reason = "emptyManeuvers"
        for index, maneuver in enumerate(maneuvers):
          fields = self._maneuver_evidence(maneuver, traveled)
          command = valhalla_type(_field(maneuver, "type"), _field(maneuver, "modifier"))
          if index == 0:
            evidence.update(fields)
          if command is None or command <= 6:
            rejected = "unsupportedManeuver"
          elif not math.isfinite(fields["adjustedDistance"]):
            rejected = "nonfiniteDistance"
          elif fields["adjustedDistance"] < 0:
            rejected = "passedManeuvers"
          else:
            if vector is not None:
              evidence.update(fields)
              evidence["selectedManeuverIndex"] = index
              evidence["selectedCommand"] = command
              reason = "accepted"
            else:
              reason = "noUsableManeuver"
            break
          if index == 0:
            reason = rejected
          elif reason != rejected:
            reason = "noUsableManeuver"
      evidence["instructionReason"] = reason
    except Exception:
      evidence["instructionReason"] = "diagnosticError"
    self._evidence = evidence

  @staticmethod
  def _maneuver_evidence(maneuver, traveled: float) -> dict:
    distance = float(_field(maneuver, "distance", 0.0))
    return {"maneuverType": str(_field(maneuver, "type")),
            "maneuverModifier": str(_field(maneuver, "modifier")),
            "rawDistance": distance, "adjustedDistance": distance - traveled}

  def evidence(self) -> dict:
    return self._evidence.copy()

  def status(self) -> dict[str, float | bool]:
    return {
      "map_valid": self.map_valid,
      "instruction_valid": self.instruction_valid,
      "map_age": float(self.map_age),
      "instruction_age": float(self.instruction_age),
    }

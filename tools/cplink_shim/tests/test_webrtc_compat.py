import time
from dataclasses import fields

import pytest

from openpilot.system.webrtc.helpers import StreamRequestBody
from openpilot.system.webrtc.webrtcd import _SESSION_STALE_GRACE_S, _session_stale


def test_cplink_request_body_is_accepted_and_enabled_defaults_true():
    payload = {
        "sdp": "v=0\r\n",
        "cameras": ["road"],
        "bridge_services_in": [],
        "bridge_services_out": [],
        "client_id": "3f0c0d2e-0000-4000-8000-000000000000",
        "takeover": False,
        "carrot_state": False,
    }
    allowed = {field.name for field in fields(StreamRequestBody)}
    body = StreamRequestBody(**{key: value for key, value in payload.items() if key in allowed})
    assert body.enabled is True
    assert body.cameras == ["road"]
    assert body.client_id.startswith("3f0c")
    assert body.takeover is False
    assert body.carrot_state is False


def test_session_stale_reclaims_only_after_the_grace_period():
    """The threshold is 60 s, not 10 s.

    It was 10 s in this test only because the docstring in webrtcd said 10 s while
    the code had already grown a 60 s container/NAT grace on top of a now-dead 10 s
    early return. Both sides now read `_SESSION_STALE_GRACE_S`, so this test pins
    the boundary from that constant instead of hard-coding a second number.
    """
    class Stream:
        def get_receiver_report_stats(self):
            return {}

    session = type("Session", (), {"created": time.monotonic() - (_SESSION_STALE_GRACE_S + 1), "stream": Stream()})()
    assert _session_stale(session) is True
    # Inside the grace window a session with no stats is NOT reclaimed.
    session.created = time.monotonic() - (_SESSION_STALE_GRACE_S - 1)
    assert _session_stale(session) is False
    session.created = time.monotonic()
    assert _session_stale(session) is False


def test_session_with_receiver_reports_is_never_stale():
    class Stream:
        def get_receiver_report_stats(self):
            return {"packetsReceived": 10}

    session = type("Session", (), {"created": time.monotonic() - (_SESSION_STALE_GRACE_S + 1), "stream": Stream()})()
    assert _session_stale(session) is False


def test_second_client_gets_the_exact_409_code():
    import asyncio
    import json
    from types import SimpleNamespace

    from openpilot.system.webrtc.webrtcd import ServerState, handle_get_stream

    class Task:
        def done(self):
            return False

    state = ServerState()
    state.streams["active"] = SimpleNamespace(
        run_task=Task(), enabled=True, client_id="client-a",
        created=time.monotonic(), stream=SimpleNamespace(get_receiver_report_stats=lambda: {"x": 1}),
    )
    body = json.dumps({"sdp": "v=0\r\n", "cameras": ["road"], "client_id": "client-b"})
    status, payload, _ = asyncio.run(handle_get_stream(state, body.encode(), "application/json"))
    assert status == 409
    assert json.loads(payload)["code"] == "carrot_vision_busy"

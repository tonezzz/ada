import json
import asyncio
import sys
import types
import importlib
import pytest


pytestmark = pytest.mark.unit


def _import_server_with_stubs():
    if "server" in sys.modules:
        del sys.modules["server"]

    stub_ada = types.ModuleType("ada")

    class StubAudioLoop:
        def __init__(self, *args, **kwargs):
            self.init_kwargs = kwargs
            self.printer_agent = None

        async def run(self):
            return None

        def stop(self):
            return None

        def update_permissions(self, permissions):
            self.permissions = permissions

        def set_paused(self, paused: bool):
            self.paused = paused

    stub_ada.AudioLoop = StubAudioLoop

    stub_authenticator = types.ModuleType("authenticator")

    class StubFaceAuthenticator:
        def __init__(self, *args, **kwargs):
            self.authenticated = False

        async def start_authentication_loop(self):
            return None

    stub_authenticator.FaceAuthenticator = StubFaceAuthenticator

    stub_kasa_agent = types.ModuleType("kasa_agent")

    class StubKasaAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def initialize(self):
            return None

    stub_kasa_agent.KasaAgent = StubKasaAgent

    sys.modules["ada"] = stub_ada
    sys.modules["authenticator"] = stub_authenticator
    sys.modules["kasa_agent"] = stub_kasa_agent

    return importlib.import_module("server")


def test_status_endpoint(monkeypatch):
    server = _import_server_with_stubs()

    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        resp = client.get("/status")

    assert resp.status_code == 200
    assert resp.json().get("status") == "running"


@pytest.mark.asyncio
async def test_socket_connect_emits_status_and_auth_bypass(monkeypatch):
    server = _import_server_with_stubs()

    events = []

    async def fake_emit(event, data=None, room=None, to=None):
        events.append((event, data, room, to))

    monkeypatch.setattr(server.sio, "emit", fake_emit)

    server.SETTINGS["face_auth_enabled"] = False
    server.authenticator = None

    sid = "test-sid"
    await server.connect(sid, {})

    assert any(e[0] == "status" for e in events)
    assert any(e[0] == "auth_status" and e[1] == {"authenticated": True} for e in events)


@pytest.mark.asyncio
async def test_start_audio_blocked_when_auth_required(monkeypatch):
    server = _import_server_with_stubs()

    events = []

    async def fake_emit(event, data=None, room=None, to=None):
        events.append((event, data, room, to))

    monkeypatch.setattr(server.sio, "emit", fake_emit)

    server.SETTINGS["face_auth_enabled"] = True
    server.authenticator = types.SimpleNamespace(authenticated=False)

    await server.start_audio("test-sid", {})

    assert any(e[0] == "error" and e[1] == {"msg": "Authentication Required"} for e in events)


@pytest.mark.asyncio
async def test_start_audio_when_already_running_emits_status(monkeypatch):
    server = _import_server_with_stubs()

    events = []

    async def fake_emit(event, data=None, room=None, to=None):
        events.append((event, data, room, to))

    monkeypatch.setattr(server.sio, "emit", fake_emit)

    class DummyTask:
        def done(self):
            return False

        def cancelled(self):
            return False

    server.audio_loop = object()
    server.loop_task = DummyTask()

    await server.start_audio("test-sid", {"device_index": 1, "device_name": "Mic"})

    assert any(e[0] == "status" and e[1] == {"msg": "A.D.A Already Running"} for e in events)


@pytest.mark.asyncio
async def test_start_audio_initializes_audioloop_with_device_payload(monkeypatch):
    server = _import_server_with_stubs()

    created = {}

    class CapturingAudioLoop(server.ada.AudioLoop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created["loop"] = self

    monkeypatch.setattr(server.ada, "AudioLoop", CapturingAudioLoop)

    async def fake_emit(event, data=None, room=None, to=None):
        return None

    monkeypatch.setattr(server.sio, "emit", fake_emit)
    async def _noop_monitor():
        await asyncio.sleep(0)

    monkeypatch.setattr(server, "monitor_printers_loop", _noop_monitor)

    server.audio_loop = None
    server.loop_task = None

    await server.start_audio(
        "test-sid",
        {"device_index": 7, "device_name": "USB Mic", "muted": True},
    )

    loop = created["loop"]
    assert loop.init_kwargs.get("input_device_index") == 7
    assert loop.init_kwargs.get("input_device_name") == "USB Mic"
    assert getattr(loop, "paused", False) is True


@pytest.mark.asyncio
async def test_audioloop_callbacks_forward_to_socketio(monkeypatch):
    server = _import_server_with_stubs()

    created = {}
    events = []

    class CapturingAudioLoop(server.ada.AudioLoop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created["loop"] = self

    monkeypatch.setattr(server.ada, "AudioLoop", CapturingAudioLoop)

    async def fake_emit(event, data=None, room=None, to=None):
        events.append((event, data, room, to))

    monkeypatch.setattr(server.sio, "emit", fake_emit)
    async def _noop_monitor():
        await asyncio.sleep(0)

    monkeypatch.setattr(server, "monitor_printers_loop", _noop_monitor)

    server.audio_loop = None
    server.loop_task = None

    await server.start_audio("test-sid", {"muted": False})

    loop = created["loop"]
    on_cad_data = loop.init_kwargs["on_cad_data"]
    on_transcription = loop.init_kwargs["on_transcription"]

    on_cad_data({"vertices": [1, 2, 3]})
    on_transcription({"sender": "User", "text": "hello"})

    await asyncio.sleep(0)

    assert any(e[0] == "cad_data" and e[1] == {"vertices": [1, 2, 3]} for e in events)
    assert any(e[0] == "transcription" and e[1] == {"sender": "User", "text": "hello"} for e in events)


@pytest.mark.asyncio
async def test_audioloop_more_callbacks_forward_to_socketio(monkeypatch):
    server = _import_server_with_stubs()

    created = {}
    events = []

    class CapturingAudioLoop(server.ada.AudioLoop):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created["loop"] = self

    monkeypatch.setattr(server.ada, "AudioLoop", CapturingAudioLoop)

    async def fake_emit(event, data=None, room=None, to=None):
        events.append((event, data, room, to))

    monkeypatch.setattr(server.sio, "emit", fake_emit)

    async def _noop_monitor():
        await asyncio.sleep(0)

    monkeypatch.setattr(server, "monitor_printers_loop", _noop_monitor)

    server.audio_loop = None
    server.loop_task = None

    await server.start_audio("test-sid", {"muted": False})

    loop = created["loop"]
    on_web_data = loop.init_kwargs["on_web_data"]
    on_tool_confirmation = loop.init_kwargs["on_tool_confirmation"]
    on_cad_status = loop.init_kwargs["on_cad_status"]
    on_cad_thought = loop.init_kwargs["on_cad_thought"]
    on_project_update = loop.init_kwargs["on_project_update"]
    on_device_update = loop.init_kwargs["on_device_update"]
    on_error = loop.init_kwargs["on_error"]

    on_web_data({"log": "did a thing", "image": "..."})
    on_tool_confirmation({"id": "1", "tool": "write_file", "args": {"path": "x"}})

    on_cad_status("generating")
    on_cad_status({"status": "retrying", "attempt": 2, "max_attempts": 3, "error": "boom"})

    on_cad_thought("thinking...")
    on_project_update("proj-a")
    on_device_update([{"ip": "1.2.3.4"}])
    on_error("bad")

    await asyncio.sleep(0)

    assert any(e[0] == "browser_frame" and e[1] == {"log": "did a thing", "image": "..."} for e in events)
    assert any(
        e[0] == "tool_confirmation_request"
        and e[1] == {"id": "1", "tool": "write_file", "args": {"path": "x"}}
        for e in events
    )

    assert any(e[0] == "cad_status" and e[1] == {"status": "generating"} for e in events)
    assert any(
        e[0] == "cad_status"
        and e[1] == {"status": "retrying", "attempt": 2, "max_attempts": 3, "error": "boom"}
        for e in events
    )

    assert any(e[0] == "cad_thought" and e[1] == {"text": "thinking..."} for e in events)
    assert any(e[0] == "project_update" and e[1] == {"project": "proj-a"} for e in events)
    assert any(e[0] == "kasa_devices" and e[1] == [{"ip": "1.2.3.4"}] for e in events)
    assert any(e[0] == "error" and e[1] == {"msg": "bad"} for e in events)


def test_load_settings_merges_tool_permissions(tmp_path, monkeypatch):
    server = _import_server_with_stubs()

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "face_auth_enabled": True,
                "tool_permissions": {"generate_cad": False},
            }
        )
    )

    monkeypatch.setattr(server, "SETTINGS_FILE", str(settings_path))
    server.SETTINGS = server.DEFAULT_SETTINGS.copy()

    server.load_settings()

    assert server.SETTINGS["face_auth_enabled"] is True
    assert server.SETTINGS["tool_permissions"]["generate_cad"] is False
    assert server.SETTINGS["tool_permissions"]["run_web_agent"] is True


def test_save_settings_writes_json(tmp_path, monkeypatch):
    server = _import_server_with_stubs()

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(server, "SETTINGS_FILE", str(settings_path))

    server.SETTINGS["face_auth_enabled"] = True
    server.save_settings()

    data = json.loads(settings_path.read_text())
    assert data["face_auth_enabled"] is True

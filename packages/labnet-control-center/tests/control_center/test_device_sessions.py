"""Device control sessions: Play/Pause, settings order, lease release, viewers."""

from __future__ import annotations

import asyncio
import json
import math
import socket
import threading
import time

import grpc
import httpx
import pytest
import uvicorn

from fastapi.testclient import TestClient

from labnet.client import LabPCInfo, ResourceConflictError
from labnet.client.core.models import DeviceInfo, EndpointDescriptor, Measurement, MeasurementQuality
from labnet.client.core.exceptions import InvalidDeviceArgumentError
from labnet_control_center import panels, sessions
from labnet_control_center.app import _sse, create_app
from labnet_control_center.config import ControlCenterSettings
from labnet_control_center.sessions import DeviceSession, SessionError
from labnet_sim._generated.contracts import SIMULATED_WAVEMETER_CONTRACT


ENDPOINT = EndpointDescriptor(
    transport="grpc", target="127.0.0.1:50052", api_version="labnet.v3", secure=False
)
PLAY = {
    "settings": [{"method": "set_exposure", "value": 50.0}],
    "stream": {"method": "stream_wavelength", "sample_rate_hz": 50.0, "channel": None},
}


def _settings(**overrides) -> ControlCenterSettings:
    values = dict(
        automation_enabled=True, photo_agent_enabled=True,  # the optional parts, on
        server_url="http://127.0.0.1:8008", api_key="key", ca_file=None,
        bind_host="127.0.0.1", port=8080, refresh_seconds=60.0,
        idle_release_seconds=0.3, max_hold_minutes=30.0, script_timeout_seconds=30.0,
    )
    values.update(overrides)
    return ControlCenterSettings(**values)


class FakeStream:
    def __init__(self, log: list[str], rate: float, bad_readings: bool = False) -> None:
        self._log = log
        self._interval = 1 / rate
        self._sequence = 0
        self._bad_readings = bad_readings  # every other reading NaN, as a Lab PC sends a BAD one

    def __aiter__(self):
        return self

    async def __anext__(self) -> Measurement:
        await asyncio.sleep(self._interval)
        self._sequence += 1
        if self._bad_readings and self._sequence % 2 == 0:
            return Measurement(
                operation_id="op", sequence=self._sequence, timestamp_ns=time.time_ns(),
                value=math.nan, unit="nm", quality=MeasurementQuality.BAD, status="NON_FINITE",
            )
        return Measurement(
            operation_id="op", sequence=self._sequence, timestamp_ns=time.time_ns(),
            value=780.241 + self._sequence * 1e-6, unit="nm",
            quality=MeasurementQuality.GOOD, status="OK",
        )

    async def aclose(self) -> None:
        self._log.append("stream closed")


class FakeWavemeter:
    contract = SIMULATED_WAVEMETER_CONTRACT
    bad_readings = False
    read_error: Exception | None = None

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def set_exposure(self, value: float, *, timeout_seconds: float | None = None) -> Measurement:
        self._log.append(f"set_exposure {value:g}" + (f" within {timeout_seconds:g} s" if timeout_seconds else ""))
        return value

    async def read_wavelength(self, *, timeout_seconds: float | None = None) -> Measurement:
        if self.read_error is not None:
            raise self.read_error
        return Measurement(
            operation_id="op", sequence=1, timestamp_ns=time.time_ns(), value=780.241, unit="nm",
            quality=MeasurementQuality.GOOD, status="OK",
        )

    def stream_wavelength(self, *, sample_rate_hz: float, **_ignored) -> FakeStream:
        self._log.append("stream opened")
        return FakeStream(self._log, sample_rate_hz, self.bad_readings)

    async def aclose(self) -> None:
        self._log.append("lease released")


class FakeCentral:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.busy = False

    async def get_device(self, device_id: str) -> DeviceInfo:
        return DeviceInfo(device_id=device_id, manager_id="lab-pc-dev-02", endpoint=ENDPOINT,
                          device_type="simulated_wavemeter")

    async def acquire_device(self, contract, device_id: str) -> FakeWavemeter:
        if self.busy:
            raise ResourceConflictError("Device already has an active lease.")
        self.log.append("lease acquired")
        return FakeWavemeter(self.log)

    async def list_lab_pcs(self):
        def device(device_id: str, leased: bool = False) -> DeviceInfo:
            return DeviceInfo(
                device_id=device_id, manager_id="lab-pc-dev-02", endpoint=ENDPOINT,
                device_type="simulated_wavemeter", runtime_state="ready", availability="available",
                effective_available=not leased, allocation_state="leased" if leased else "free",
            )

        return [
            LabPCInfo(
                manager_id="lab-pc-dev-02", host="127.0.0.1", endpoint=ENDPOINT, online=True,
                last_seen=None,
                devices=(device("dev02-wavemeter"), device("dev02-busy", leased=True)),
            )
        ]

    async def aclose(self) -> None:
        pass


def _session(central: FakeCentral, **settings) -> DeviceSession:
    return DeviceSession("dev02-wavemeter", central, _settings(**settings))


@pytest.mark.asyncio
async def test_play_applies_settings_before_streaming_and_pause_releases() -> None:
    central = FakeCentral()
    session = _session(central)
    viewer = session.subscribe()

    state = await session.play(PLAY)
    await asyncio.sleep(0.2)
    paused = await session.pause()

    assert state["state"] == "streaming" and state["leased"] is True
    assert central.log[:3] == ["lease acquired", "set_exposure 50", "stream opened"]
    assert central.log[-2:] == ["stream closed", "lease released"]
    assert paused["state"] == "idle" and paused["leased"] is False
    events = [viewer.queue.get_nowait() for _ in range(viewer.queue.qsize())]
    assert [event["type"] for event in events][:2] == ["state", "reset"]
    assert any(event["type"] == "samples" for event in events)


@pytest.mark.asyncio
async def test_play_again_starts_a_fresh_graph() -> None:
    session = _session(FakeCentral())
    await session.play(PLAY)
    await asyncio.sleep(0.15)
    await session.pause()
    assert session.snapshot_event()["samples"]

    await session.play(PLAY)
    assert session.snapshot_event()["samples"] == []
    await session.pause()


@pytest.mark.asyncio
async def test_a_device_in_use_is_reported_and_nothing_is_held() -> None:
    central = FakeCentral()
    central.busy = True
    session = _session(central)

    with pytest.raises(SessionError, match="in use") as failure:
        await session.play(PLAY)

    assert failure.value.status_code == 409
    assert session.state_event()["leased"] is False


@pytest.mark.asyncio
async def test_bad_requests_are_refused_before_leasing() -> None:
    central = FakeCentral()
    session = _session(central)
    for request, message in [
        ({**PLAY, "settings": [{"method": "read_wavelength", "value": 1}]}, "not a setting"),
        ({**PLAY, "stream": {**PLAY["stream"], "method": "set_exposure"}}, "not a stream"),
        ({**PLAY, "stream": {**PLAY["stream"], "sample_rate_hz": 5000}}, "Sample rate"),
    ]:
        with pytest.raises(SessionError, match=message):
            await session.play(request)
    assert "lease acquired" not in central.log


@pytest.mark.asyncio
async def test_the_lease_is_released_when_the_last_viewer_leaves() -> None:
    central = FakeCentral()
    session = _session(central, idle_release_seconds=0.3)
    first, second = session.subscribe(), session.subscribe()
    await session.play(PLAY)

    session.unsubscribe(first)
    await asyncio.sleep(0.5)
    assert session.state_event()["leased"] is True  # someone is still watching

    session.unsubscribe(second)
    await asyncio.sleep(0.5)
    state = session.state_event()
    assert state["leased"] is False and "nobody had the page open" in state["message"]


@pytest.mark.asyncio
async def test_play_with_no_viewer_still_releases() -> None:
    session = _session(FakeCentral(), idle_release_seconds=0.3)
    await session.play(PLAY)  # e.g. pressed through the API
    await asyncio.sleep(0.6)
    assert session.state_event()["leased"] is False


@pytest.mark.asyncio
async def test_script_and_stream_share_one_lease() -> None:
    central = FakeCentral()
    session = _session(central)
    session.subscribe()
    await session.play(PLAY)
    await session.run_script("device.set_exposure(20)")
    for _ in range(100):
        if not session.state_event()["script_running"]:
            break
        await asyncio.sleep(0.1)
    await session.pause()

    assert central.log.count("lease acquired") == 1
    assert "set_exposure 20" in central.log
    assert central.log[-1] == "lease released"


class _RunningApp:
    """The Control Center on a real port, so browser disconnects are real."""

    def __init__(self, central: FakeCentral) -> None:
        self.app = create_app(_settings(idle_release_seconds=0.3), client_factory=lambda _: central)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_RunningApp":
        self.thread.start()
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/health/live", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        return self

    def __exit__(self, *_exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def test_leaving_an_idle_page_unregisters_the_viewer_at_once() -> None:
    """Nothing is being sent, so only the disconnect itself can end the stream."""

    with _RunningApp(FakeCentral()) as running:
        base = f"http://127.0.0.1:{running.port}/api/devices/dev02-wavemeter"
        with httpx.stream("GET", f"{base}/events", timeout=5) as events:
            next(line for line in events.iter_lines() if line.startswith("data:"))
        session = running.app.state.sessions.get("dev02-wavemeter")
        for _ in range(30):
            if session.state_event()["viewers"] == 0:
                break
            time.sleep(0.1)
        assert session.state_event()["viewers"] == 0


def test_closing_the_browser_unregisters_the_viewer() -> None:
    """A real server: disconnecting from the event stream must free the device."""

    central = FakeCentral()
    with _RunningApp(central) as running:
        base = f"http://127.0.0.1:{running.port}/api/devices/dev02-wavemeter"
        with httpx.stream("GET", f"{base}/events", timeout=5) as events:
            lines = events.iter_lines()
            next(line for line in lines if line.startswith("data:"))  # the snapshot
            state = httpx.post(f"{base}/play", json=PLAY, timeout=5).json()
            assert state["state"] == "streaming" and state["viewers"] == 1
        # The stream is closed; after the grace period the lease must be gone.
        for _ in range(40):
            if "lease released" in central.log:
                break
            time.sleep(0.1)
        assert "lease released" in central.log


async def _until(condition, seconds: float = 10.0) -> None:
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_release_stops_the_stream_and_the_script_and_frees_every_lease() -> None:
    central = FakeCentral()
    session = _session(central, idle_release_seconds=60)
    viewer = session.subscribe()
    await session.play(PLAY)
    await session.run_script("other = lease('dev01-dummy-a')\nsleep(30)")
    await _until(lambda: "dev01-dummy-a" in session.activity())

    held = session.activity()
    assert held["dev02-wavemeter"]["streaming"] and held["dev02-wavemeter"]["script_running"]
    assert held["dev02-wavemeter"]["since"] is not None
    assert held["dev01-dummy-a"]["session"] == "dev02-wavemeter"

    state = await session.release("Paused from the device list.")

    assert session.activity() == {}
    assert state["leased"] is False and state["script_running"] is False
    assert state["message"] == "Paused from the device list."
    assert central.log.count("lease acquired") == central.log.count("lease released") == 2
    events = [viewer.queue.get_nowait() for _ in range(viewer.queue.qsize())]
    assert any(event.get("text") == "Paused from the device list." for event in events)


def test_the_device_list_shows_and_pauses_what_the_control_center_holds() -> None:
    """Any viewer, on any computer, can pause a page someone left running."""

    app = create_app(_settings(idle_release_seconds=60), client_factory=lambda _: FakeCentral())
    with TestClient(app) as browser:
        def devices() -> dict[str, dict]:
            snapshot = browser.get("/api/status").json()
            return {device["id"]: device for pc in snapshot["lab_pcs"] for device in pc["devices"]}

        assert devices()["dev02-wavemeter"]["control_center"] is None
        assert browser.post("/api/devices/dev02-wavemeter/play", json=PLAY).status_code == 200

        held = devices()
        meter = held["dev02-wavemeter"]
        assert meter["state"] == "leased"
        assert meter["control_center"]["streaming"] is True
        assert meter["control_center"]["page"] == "/dev02-wavemeter"
        # Leased by some other LabNet client: shown, but not ours to pause.
        assert held["dev02-busy"]["state"] == "leased" and held["dev02-busy"]["control_center"] is None

        released = browser.post("/api/devices/dev02-wavemeter/release")
        assert released.status_code == 200 and released.json()["leased"] is False
        meter = devices()["dev02-wavemeter"]
        assert meter["state"] == "available" and meter["control_center"] is None


# ---- calls from a page's panel ---------------------------------------------------


@pytest.mark.asyncio
async def test_calls_share_one_lease_until_pause() -> None:
    central = FakeCentral()
    session = _session(central)
    viewer = session.subscribe()

    assert await session.call("set_exposure", {"value": 20}, timeout_seconds=45) == 20
    assert await session.call("read_wavelength", {}) == 780.241
    assert session.state_event()["calls_held"] is True
    state = await session.pause()

    assert central.log == ["lease acquired", "set_exposure 20 within 45 s", "lease released"]
    assert state["leased"] is False and state["calls_held"] is False
    events = [viewer.queue.get_nowait() for _ in range(viewer.queue.qsize())]
    assert [event["calls_held"] for event in events if event["type"] == "state"] == [True, False]


@pytest.mark.asyncio
async def test_calls_and_the_stream_share_one_lease() -> None:
    central = FakeCentral()
    session = _session(central)
    session.subscribe()
    await session.play(PLAY)
    await session.call("set_exposure", {"value": 30})
    await session.pause()

    assert central.log.count("lease acquired") == central.log.count("lease released") == 1


@pytest.mark.asyncio
async def test_bad_calls_are_refused_before_leasing() -> None:
    central = FakeCentral()
    session = _session(central)
    for method, timeout, message in [
        ("no_such_method", 30, "not a method"),
        ("stream_wavelength", 30, "use Play"),
        ("read_wavelength", 0, "timeout_seconds"),
        ("read_wavelength", 10_000, "timeout_seconds"),
    ]:
        with pytest.raises(SessionError, match=message) as failure:
            await session.call(method, {}, timeout_seconds=timeout)
        assert failure.value.status_code == 400
    assert central.log == []


@pytest.mark.asyncio
async def test_calls_are_refused_on_a_device_automation_holds() -> None:
    central = FakeCentral()
    session = DeviceSession("dev02-wavemeter", central, _settings(), blocked=lambda _device_id: True)

    with pytest.raises(SessionError, match="Automation") as failure:
        await session.call("read_wavelength", {})

    assert failure.value.status_code == 409 and central.log == []


@pytest.mark.asyncio
async def test_a_refused_argument_and_a_failed_call_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(FakeCentral())

    with pytest.raises(SessionError, match="unexpected keyword") as wrong_name:
        await session.call("set_exposure", {"exposure": 20})
    monkeypatch.setattr(
        FakeWavemeter, "read_error",
        InvalidDeviceArgumentError(grpc.StatusCode.INVALID_ARGUMENT, "value must be within [757, 782]."),
    )
    with pytest.raises(SessionError, match=r"within \[757, 782\]") as out_of_range:
        await session.call("read_wavelength", {})
    monkeypatch.setattr(FakeWavemeter, "read_error", RuntimeError("the Lab PC went away"))
    with pytest.raises(SessionError, match="went away") as failed:
        await session.call("read_wavelength", {})
    await session.pause()

    assert wrong_name.value.status_code == 400
    assert out_of_range.value.status_code == 400
    assert failed.value.status_code == 502


@pytest.mark.asyncio
async def test_calls_give_the_lease_back_when_they_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "CALL_IDLE_RELEASE_SECONDS", 0.2)
    central = FakeCentral()
    session = _session(central, idle_release_seconds=60)
    session.subscribe()

    await session.call("read_wavelength", {})
    await _until(lambda: "lease released" in central.log, seconds=3)

    state = session.state_event()
    assert state["calls_held"] is False and "no commands" in state["message"]


@pytest.mark.asyncio
async def test_calls_give_the_lease_back_at_the_hold_limit() -> None:
    central = FakeCentral()
    session = _session(central, max_hold_minutes=0.005, idle_release_seconds=60)  # 0.3 s
    session.subscribe()

    for _ in range(3):  # busy, so only the hold limit can end it
        await session.call("read_wavelength", {})
        await asyncio.sleep(0.05)
    await _until(lambda: "lease released" in central.log, seconds=3)

    assert "automatically" in session.state_event()["message"]


def test_the_call_endpoint_returns_the_value_and_checks_the_request() -> None:
    central = FakeCentral()
    app = create_app(_settings(idle_release_seconds=60), client_factory=lambda _: central)
    with TestClient(app) as browser:
        base = "/api/devices/dev02-wavemeter"
        reply = browser.post(f"{base}/call", json={"method": "read_wavelength"})
        assert reply.status_code == 200 and reply.json() == {"value": 780.241}

        refused = [
            {"method": "set_exposure", "args": {"value": [1, 2]}},
            {"method": "read_wavelength", "timeout_seconds": 5000},
        ]
        for body in refused:
            assert browser.post(f"{base}/call", json=body).status_code == 422
        stream = browser.post(f"{base}/call", json={"method": "stream_wavelength"})
        assert stream.status_code == 400 and "use Play" in stream.json()["detail"]

        assert browser.post(f"{base}/pause").json()["leased"] is False
    assert central.log == ["lease acquired", "lease released"]


@pytest.mark.asyncio
async def test_bad_stream_readings_reach_the_page_as_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeWavemeter, "bad_readings", True)
    session = _session(FakeCentral())
    viewer = session.subscribe()
    await session.play(PLAY)
    await asyncio.sleep(0.2)
    await session.pause()

    values = [point[1] for point in session.snapshot_event()["samples"]]
    assert None in values and any(isinstance(value, float) for value in values)

    def refuse(constant: str) -> None:
        raise AssertionError(f"{constant} is not JSON")

    events = [viewer.queue.get_nowait() for _ in range(viewer.queue.qsize())]
    for event in [session.snapshot_event(), *events]:
        json.loads(_sse(event).removeprefix("data: "), parse_constant=refuse)


# ---- panels that driver packages ship --------------------------------------------


def test_a_driver_package_panel_is_served_and_chosen(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "acme_panels"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "simulated_wavemeter.js").write_text("// the driver's panel\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(panels, "_registered", lambda: {"simulated_wavemeter": "acme_panels"})

    app = create_app(_settings(), client_factory=lambda _: FakeCentral())
    with TestClient(app) as browser:
        device = browser.get("/api/status").json()["lab_pcs"][0]["devices"][0]
        script = browser.get(device["panel_url"])
        missing = browser.get("/api/panels/dummy_aom.js")

    assert device["panel"] == "simulated_wavemeter"
    assert device["panel_url"] == "/api/panels/simulated_wavemeter.js"
    assert script.status_code == 200 and script.text == "// the driver's panel\n"
    assert script.headers["content-type"].startswith("text/javascript")
    assert missing.status_code == 404


def test_without_a_driver_panel_the_built_in_one_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panels, "_registered", lambda: {})

    assert panels.panel_for("simulated_wavemeter") == (
        "simulated_wavemeter", "/static/js/panels/simulated_wavemeter.js"
    )
    assert panels.panel_for("acme_laser") == ("generic", "/static/js/panels/generic.js")
    assert panels.driver_panel("simulated_wavemeter") is None

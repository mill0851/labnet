"""Device sessions: the Control Center's lease on one device for its control page.

A session leases the device only while something needs it: the live stream
(Play), a running script, or the page's own calls (a panel's Move or Apply
button, see :meth:`DeviceSession.call`). Pause and a finished script each give
up their claim; calls give theirs up at Pause, after
:data:`CALL_IDLE_RELEASE_SECONDS` without a call, or at the hold limit. The
lease is released once no claim remains, so the device is free for others
again. Every browser viewing the page subscribes to the session's events
(state changes, samples, script output); when the last viewer leaves, the
session stops after a short grace period.

Stream samples that are not finite (a reading the device marked bad) reach the
page as ``None``, a gap in the graph.

Anyone using the Control Center may stop a session, from any computer: the
Network and Devices pages list what the Control Center holds (see
:meth:`SessionManager.activity`) with a Pause button that calls
:meth:`DeviceSession.release`.

Device errors can quote a lease id, which is enough to drive the device while
the lease lasts. Everything a browser receives from a session passes through
:func:`~labnet.redaction.redact`: :class:`SessionError`
messages, state messages and every published event. The log keeps full text.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from labnet.client import AsyncLabClient, ResourceConflictError, ResourceNotFoundError
from labnet.client.core.exceptions import DeviceOutOfRangeError, InvalidDeviceArgumentError
from labnet.contracts import DeviceContract, RpcSpec, registry

from .config import ControlCenterSettings
from .device_calls import SETTING_PATTERNS as _SETTING_PATTERNS
from .device_calls import STREAM_PATTERN as _STREAM_PATTERN
from .device_calls import call_device, device_error, json_safe
from .device_calls import find_method as _method
from labnet.redaction import redact
from .script_runner import ScriptProcess
from .scripting import ScriptError, validate_script


LOGGER = logging.getLogger(__name__)
MAX_BUFFERED_SAMPLES = 20_000
MAX_SAMPLE_RATE_HZ = 200.0
MAX_SCRIPT_STREAM_SAMPLES = 10_000
# A page's calls keep the lease this long after the last one (Central's own idle
# timeout is longer), so a panel left open frees the device for others.
CALL_IDLE_RELEASE_SECONDS = 300.0
MAX_CALL_SECONDS = 600.0
_CALLS = "calls"  # the holder name of the page's calls


class SessionError(Exception):
    """A request the session cannot carry out; ``status_code`` maps to HTTP."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(redact(message))
        self.status_code = status_code


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=5000))
    # Set when the viewer fell too far behind; its event stream then ends and
    # the browser reconnects, receiving a fresh snapshot.
    dropped: bool = False


class DeviceSession:
    def __init__(
        self,
        device_id: str,
        client: AsyncLabClient,
        settings: ControlCenterSettings,
        blocked: Callable[[str], bool] | None = None,
    ) -> None:
        self.device_id = device_id
        self._client = client
        self._settings = settings
        # True for a device an Automation grant holds (see automation_grants.py).
        self._blocked = blocked or (lambda _device_id: False)
        self._lock = asyncio.Lock()
        self._contract: DeviceContract | None = None
        self._device: Any = None  # AsyncContractDeviceClient while leased
        self._holders: set[str] = set()
        self._subscribers: set[Subscriber] = set()
        self._buffer: deque[list[float]] = deque(maxlen=MAX_BUFFERED_SAMPLES)
        self._stream_task: asyncio.Task[None] | None = None
        self._hold_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._script: ScriptProcess | None = None
        self._script_devices: dict[str, Any] = {}
        self._script_leased_at: dict[str, float] = {}
        self._calls_task: asyncio.Task[None] | None = None
        self._calls_since = 0.0
        self._last_call_at = 0.0
        self._calls_running = 0
        self.leased_at: float | None = None
        self.state = "idle"
        self.message: str | None = None
        self.unit = ""
        self.play_request: dict[str, Any] | None = None
        self.started_at: float | None = None

    # ---- events ---------------------------------------------------------------

    def state_event(self) -> dict[str, Any]:
        return {
            "type": "state",
            "state": self.state,
            "message": self.message,
            "leased": self._device is not None,
            "script_running": self._script is not None,
            # The page's calls hold the lease (released at Pause, or when idle).
            "calls_held": _CALLS in self._holders,
            "unit": self.unit,
            "play": self.play_request,
            "started_at": self.started_at,
            "max_hold_seconds": self._settings.max_hold_minutes * 60,
            "viewers": len(self._subscribers),
        }

    def snapshot_event(self) -> dict[str, Any]:
        return {**self.state_event(), "type": "snapshot", "samples": list(self._buffer)}

    def _publish(self, event: dict[str, Any]) -> None:
        if event.get("type") != "samples":  # samples are numbers; everything else may quote an error
            event = {key: redact(value) if isinstance(value, str) else value for key, value in event.items()}
        for subscriber in list(self._subscribers):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscriber.dropped = True
                self._subscribers.discard(subscriber)

    def _set_state(self, state: str, message: str | None = None) -> None:
        self.state = state
        self.message = redact(message) if message else message
        self._publish(self.state_event())

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber()
        self._subscribers.add(subscriber)
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)
        if not self._subscribers and (self._holders or self._script) and self._idle_task is None:
            self._idle_task = asyncio.create_task(self._release_when_unwatched())

    async def _release_when_unwatched(self) -> None:
        await asyncio.sleep(self._settings.idle_release_seconds)
        self._idle_task = None
        if not self._subscribers:
            self.stop_script("Stopped: nobody had the page open.")
            await self.pause("Stopped: nobody had the page open, so the device was released.")

    # ---- lease ----------------------------------------------------------------

    async def _ensure_contract(self) -> DeviceContract:
        if self._contract is None:
            try:
                info = await self._client.get_device(self.device_id)
            except ResourceNotFoundError as exc:
                raise SessionError(404, f"No device {self.device_id!r} is registered.") from exc
            contract = registry.find(info.device_type)
            if contract is None:
                raise SessionError(409, f"This Control Center has no contract for {info.device_type!r}.")
            self._contract = contract
        return self._contract

    async def _acquire(self, holder: str) -> None:
        contract = await self._ensure_contract()
        if self._device is None:
            try:
                self._device = await self._client.acquire_device(contract, self.device_id)
                self.leased_at = time.time()
            except ResourceConflictError as exc:
                raise SessionError(409, f"{self.device_id} is in use by another client or unavailable.") from exc
        self._holders.add(holder)

    async def _release(self, holder: str) -> None:
        self._holders.discard(holder)
        if not self._holders and self._device is not None:
            device, self._device = self._device, None
            self.leased_at = None
            try:
                await device.aclose()
            except Exception:
                LOGGER.exception("Releasing the lease on %s failed.", self.device_id)

    # ---- Play / Pause -----------------------------------------------------------

    def _refuse_if_granted(self, device_id: str) -> None:
        if self._blocked(device_id):
            raise SessionError(
                409,
                f"{device_id} is held by an Automation experiment. Stop it on the Network or "
                "Devices page (or wait for it to end) before using the device here.",
            )

    async def play(self, request: dict[str, Any]) -> dict[str, Any]:
        self._refuse_if_granted(self.device_id)
        contract = await self._ensure_contract()
        settings, stream_spec, rate, channel = self._check_play_request(contract, request)
        async with self._lock:
            if self._stream_task is not None:
                raise SessionError(409, "Already playing. Pause first to change settings.")
            await self._acquire("stream")
            self._set_state("starting", "Applying settings…")
            try:
                for spec, value in settings:
                    await getattr(self._device, spec.python_name)(value)
            except Exception as exc:
                await self._release("stream")
                self._set_state("idle", f"Could not apply {spec.python_name}: {exc}")
                raise SessionError(502, f"Could not apply {spec.python_name}: {exc}") from exc

            self.play_request = request
            self.unit = stream_spec.default_unit
            self.started_at = time.time()
            self._buffer.clear()
            self._publish({"type": "reset"})
            self._stream_task = asyncio.create_task(self._stream(stream_spec, rate, channel))
            self._hold_task = asyncio.create_task(self._hold_limit())
            self._set_state("streaming")
            if not self._subscribers and self._idle_task is None:
                # Started without the page open (e.g. via the API): same rule.
                self._idle_task = asyncio.create_task(self._release_when_unwatched())
            return self.state_event()

    def _check_play_request(self, contract: DeviceContract, request: dict[str, Any]):
        settings = []
        for item in request.get("settings", []):
            spec = _method(contract, str(item.get("method")))
            if spec is None or spec.pattern not in _SETTING_PATTERNS:
                raise SessionError(400, f"{item.get('method')!r} is not a setting of {contract.device_type}.")
            value = item.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise SessionError(400, f"{spec.python_name} needs a number.")
            settings.append((spec, float(value)))
        stream = request.get("stream") or {}
        stream_spec = _method(contract, str(stream.get("method")))
        if stream_spec is None or stream_spec.pattern != _STREAM_PATTERN:
            raise SessionError(400, f"{stream.get('method')!r} is not a stream of {contract.device_type}.")
        rate = stream.get("sample_rate_hz")
        if not isinstance(rate, (int, float)) or not 0.1 <= rate <= MAX_SAMPLE_RATE_HZ:
            raise SessionError(400, f"Sample rate must be between 0.1 and {MAX_SAMPLE_RATE_HZ:g} Hz.")
        channel = stream.get("channel") or None
        return settings, stream_spec, float(rate), channel

    async def _stream(self, spec: RpcSpec, rate: float, channel: str | None) -> None:
        options: dict[str, Any] = {"sample_rate_hz": rate}
        if channel:
            options["channel"] = channel
        try:
            while True:  # a stream ends at the Lab PC's maximum duration; reopen it
                samples = getattr(self._device, spec.python_name)(**options)
                try:
                    async for sample in samples:
                        # A bad reading is NaN, which JSON cannot carry: it becomes None, a gap.
                        point = [sample.timestamp_ns / 1e9, json_safe(sample.value)]
                        self._buffer.append(point)
                        self._publish({"type": "samples", "points": [point]})
                finally:
                    await samples.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # lease lost, Lab PC gone, device error...
            LOGGER.warning("Stream from %s stopped: %s", self.device_id, exc)
            asyncio.create_task(self.pause(f"Stopped: {type(exc).__name__}: {exc}", failed=True))

    async def _hold_limit(self) -> None:
        minutes = self._settings.max_hold_minutes
        await asyncio.sleep(minutes * 60)
        asyncio.create_task(self.pause(f"Stopped automatically after {minutes:g} minutes."))

    async def pause(self, reason: str | None = None, *, failed: bool = False) -> dict[str, Any]:
        async with self._lock:
            current = asyncio.current_task()
            for task in (self._stream_task, self._hold_task):
                if task is not None and task is not current:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            had_stream = self._stream_task is not None
            self._stream_task = None
            self._hold_task = None
            await self._release("stream")
            had_calls = await self._end_calls()
            if had_stream or had_calls or reason:
                self._set_state("error" if failed else "idle", reason)
            return self.state_event()

    async def release(self, reason: str) -> dict[str, Any]:
        """Stop the stream and any script, so every lease this page holds is freed."""

        self.stop_script(reason)
        await self.pause(reason)
        for _ in range(50):  # the script process reports back once it has exited
            if self._script is None:
                break
            await asyncio.sleep(0.1)
        return self.state_event()

    # ---- calls from the page's panel ---------------------------------------------

    async def call(self, method: str, args: dict[str, Any], timeout_seconds: float = 30.0) -> Any:
        """Run one method for the page's panel and return its result as JSON can carry it.

        The first call leases the device; later ones reuse the lease, which
        Pause releases (as does :data:`CALL_IDLE_RELEASE_SECONDS` without a call,
        or the hold limit). Streams are Play's. The Lab PC checks the arguments
        against the driver's and the site's limits, as for any client.
        """

        self._refuse_if_granted(self.device_id)
        contract = await self._ensure_contract()
        spec = _method(contract, method)
        if spec is None:
            raise SessionError(400, f"{method!r} is not a method of {contract.device_type}.")
        if spec.pattern == _STREAM_PATTERN:
            raise SessionError(400, f"{method} is a stream: use Play.")
        if not 0 < timeout_seconds <= MAX_CALL_SECONDS:
            raise SessionError(400, f"timeout_seconds must be above 0 and at most {MAX_CALL_SECONDS:g}.")
        async with self._lock:
            first = _CALLS not in self._holders
            await self._acquire(_CALLS)
            device = self._device
            self._calls_running += 1
            self._last_call_at = time.time()
            if first:
                self._calls_since = self._last_call_at
                self._calls_task = asyncio.create_task(self._calls_limit())
                if not self._subscribers and self._idle_task is None:
                    self._idle_task = asyncio.create_task(self._release_when_unwatched())
                self._publish(self.state_event())
        try:
            result = await call_device(device, spec, {**args, "timeout_seconds": timeout_seconds})
        except (TypeError, ValueError, InvalidDeviceArgumentError, DeviceOutOfRangeError) as exc:
            raise SessionError(400, f"{method}: {device_error(exc)}") from exc
        except Exception as exc:  # the device failed, the Lab PC is gone, the lease was lost...
            LOGGER.warning("%s.%s failed: %s", self.device_id, method, exc)
            raise SessionError(502, f"{method} failed: {device_error(exc)}") from exc
        finally:
            self._calls_running -= 1
            self._last_call_at = time.time()
        return json_safe(result)

    async def _calls_limit(self) -> None:
        """Release the calls' lease when they go idle or reach the hold limit."""

        hold_seconds = self._settings.max_hold_minutes * 60
        while True:
            now = time.time()
            hold_end = self._calls_since + hold_seconds
            idle_end = self._last_call_at + CALL_IDLE_RELEASE_SECONDS
            if now >= hold_end:
                reason = f"Released automatically after {self._settings.max_hold_minutes:g} minutes."
                break
            if now >= idle_end and not self._calls_running:
                reason = f"Released: no commands for {CALL_IDLE_RELEASE_SECONDS / 60:g} minutes."
                break
            await asyncio.sleep(max(0.05, min(hold_end, idle_end) - now))
        asyncio.create_task(self._release_calls(reason))

    async def _release_calls(self, reason: str) -> None:
        async with self._lock:
            if await self._end_calls():
                self._set_state(self.state, reason)

    async def _end_calls(self) -> bool:
        """Drop the calls' claim on the lease (the caller holds the lock); False if there was none."""

        task, self._calls_task = self._calls_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if _CALLS not in self._holders:
            return False
        await self._release(_CALLS)
        return True

    def activity(self) -> dict[str, dict[str, Any]]:
        """What this session holds right now, by device id (empty when idle)."""

        held: dict[str, dict[str, Any]] = {}
        if self._device is not None:
            held[self.device_id] = {
                "session": self.device_id,
                "since": self.leased_at,
                "streaming": self._stream_task is not None,
                "script_running": self._script is not None,
                "viewers": len(self._subscribers),
            }
        for device_id in self._script_devices:
            held[device_id] = {
                "session": self.device_id,  # leased by the script on this page
                "since": self._script_leased_at.get(device_id),
                "streaming": False,
                "script_running": True,
                "viewers": len(self._subscribers),
            }
        return held

    # ---- scripts ------------------------------------------------------------------

    async def run_script(self, code: str) -> dict[str, Any]:
        self._refuse_if_granted(self.device_id)
        contract = await self._ensure_contract()
        try:
            validate_script(code)
        except ScriptError as exc:
            raise SessionError(400, str(exc)) from exc
        async with self._lock:
            if self._script is not None:
                raise SessionError(409, "A script is already running. Stop it first.")
            await self._acquire("script")
            self._script = ScriptProcess(
                code=code,
                devices={
                    "device": {
                        "label": self.device_id,
                        "methods": [spec.python_name for spec in contract.methods],
                    }
                },
                on_call=self._script_call,
                on_lease=self._script_lease,
                on_event=lambda event: self._publish({"type": "script", **event}),
                on_finished=self._script_finished,
                loop=asyncio.get_running_loop(),
                timeout_seconds=self._settings.script_timeout_seconds,
            )
            self._script.start()
            self._publish({"type": "script", "kind": "started", "text": ""})
            self._publish(self.state_event())
            return self.state_event()

    def stop_script(self, reason: str = "Stopped by user.") -> None:
        if self._script is not None:
            self._script.stop(reason)

    async def _script_call(self, handle: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        device = self._device if handle == "device" else self._script_devices.get(handle)
        if device is None:
            raise RuntimeError(f"Device {handle!r} is not leased.")
        spec = _method(device.contract, method)
        if spec is None:
            raise RuntimeError(f"{method!r} is not a method of {device.contract.device_type}.")
        if spec.pattern == _STREAM_PATTERN:
            count = kwargs.get("sample_count")
            if not isinstance(count, int) or not 1 <= count <= MAX_SCRIPT_STREAM_SAMPLES:
                raise ValueError(
                    f"In scripts, {method}() needs sample_count between 1 and {MAX_SCRIPT_STREAM_SAMPLES}."
                )
        return await call_device(device, spec, kwargs, args=args)

    async def _script_lease(self, device_id: str) -> dict[str, Any]:
        if device_id == self.device_id:
            raise ValueError("Use `device` for this page's device.")
        if self._blocked(device_id):
            raise ValueError(f"{device_id} is held by an Automation experiment.")
        if device_id not in self._script_devices:
            info = await self._client.get_device(device_id)
            contract = registry.find(info.device_type)
            if contract is None:
                raise ValueError(f"No contract for {info.device_type!r}.")
            self._script_devices[device_id] = await self._client.acquire_device(contract, device_id)
            self._script_leased_at[device_id] = time.time()
        device = self._script_devices[device_id]
        return {"handle": device_id, "methods": [spec.python_name for spec in device.contract.methods]}

    async def _script_finished(self, error: str | None) -> None:
        async with self._lock:
            for device in self._script_devices.values():
                try:
                    await device.aclose()
                except Exception:
                    LOGGER.exception("Releasing a script lease failed.")
            self._script_devices.clear()
            self._script_leased_at.clear()
            self._script = None
            await self._release("script")
        self._publish({"type": "script", "kind": "error" if error else "done", "text": error or "Script finished."})
        self._publish(self.state_event())

    async def close(self) -> None:
        self.stop_script("Stopped: the Control Center is shutting down.")
        await self.pause()


class SessionManager:
    """One :class:`DeviceSession` per device id, created on first use."""

    def __init__(
        self,
        client: AsyncLabClient,
        settings: ControlCenterSettings,
        blocked: Callable[[str], bool] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sessions: dict[str, DeviceSession] = {}
        # Devices an Automation grant holds: Play and scripts are refused (409) on them.
        self._blocked = blocked

    def blocked(self, device_id: str) -> bool:
        return bool(self._blocked and self._blocked(device_id))

    def get(self, device_id: str) -> DeviceSession:
        if device_id not in self._sessions:
            self._sessions[device_id] = DeviceSession(device_id, self._client, self._settings, self.blocked)
        return self._sessions[device_id]

    def activity(self) -> dict[str, dict[str, Any]]:
        """Every device the Control Center holds, and which page holds it."""

        held: dict[str, dict[str, Any]] = {}
        for session in self._sessions.values():
            held.update(session.activity())
        return held

    async def close(self) -> None:
        for session in self._sessions.values():
            await session.close()


__all__ = ["DeviceSession", "SessionError", "SessionManager", "Subscriber"]

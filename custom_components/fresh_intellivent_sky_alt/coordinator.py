"""Data coordinator for Fresh Intellivent Sky."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, TypeVar

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyfreshintellivent import FreshIntelliVent

from .const import (
    CONF_DEVICE_INFO_FETCH_EVERY,
    CONF_ERROR_COOLDOWN_SECONDS,
    CONF_MODE_FETCH_EVERY,
    CONF_SCAN_INTERVAL,
    CONF_WRITE_DEBOUNCE_MS,
    DEFAULT_DEVICE_INFO_FETCH_EVERY,
    DEFAULT_ERROR_COOLDOWN_SECONDS,
    DEFAULT_MODE_FETCH_EVERY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WRITE_DEBOUNCE_MS,
    DELAY_KEY,
    DETECTION_KEY,
    DOMAIN,
    ENABLED_KEY,
    MINUTES_KEY,
    RPM_KEY,
    TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_WRITE_IDLE_TIMEOUT = 30
_OPERATION_RETRY_ATTEMPTS = 2
_OPERATION_RETRY_BACKOFF_SECONDS = 1

_ResultT = TypeVar("_ResultT")


@dataclass(slots=True)
class WriteRequest:
    """Write request for FIFO queue."""

    changes: dict[str, Any]
    done: asyncio.Future[None]


class FreshIntelliventCoordinator(DataUpdateCoordinator[FreshIntelliVent]):
    """Coordinator that manages BLE I/O and state."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        address: str,
        auth_key: str | None,
        session_lock: asyncio.Lock | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(
                seconds=config_entry.options.get(
                    CONF_SCAN_INTERVAL,
                    DEFAULT_SCAN_INTERVAL,
                )
            ),
        )
        self._address = address
        self._auth_key = auth_key
        self._entry_lock = asyncio.Lock()
        self._session_lock = session_lock
        self._queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._poll_count = 0
        self._mode_fetch_every = max(
            1,
            int(
                config_entry.options.get(
                    CONF_MODE_FETCH_EVERY,
                    DEFAULT_MODE_FETCH_EVERY,
                )
            ),
        )
        self._device_info_fetch_every = max(
            1,
            int(
                config_entry.options.get(
                    CONF_DEVICE_INFO_FETCH_EVERY,
                    DEFAULT_DEVICE_INFO_FETCH_EVERY,
                )
            ),
        )
        self._write_debounce_seconds = (
            max(
                0,
                int(
                    config_entry.options.get(
                        CONF_WRITE_DEBOUNCE_MS,
                        DEFAULT_WRITE_DEBOUNCE_MS,
                    )
                ),
            )
            / 1000
        )
        self._error_cooldown_seconds = max(
            0,
            int(
                config_entry.options.get(
                    CONF_ERROR_COOLDOWN_SECONDS,
                    DEFAULT_ERROR_COOLDOWN_SECONDS,
                )
            ),
        )
        self._next_allowed_operation_at = 0.0

    def async_start_worker(self) -> None:
        """Start the write worker task."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = self.hass.async_create_task(self._write_worker())

    async def async_stop_worker(self) -> None:
        """Stop the write worker task."""
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None

    async def async_write(self, changes: dict[str, Any]) -> None:
        """Queue a write and wait for completion."""
        self.async_start_worker()
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(WriteRequest(changes=changes, done=future))
        await future

    async def _write_worker(self) -> None:
        """Process queued writes in FIFO order."""
        try:
            while True:
                try:
                    request = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=_WRITE_IDLE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    break
                batch = [request]
                if self._write_debounce_seconds > 0:
                    await asyncio.sleep(self._write_debounce_seconds)
                    while True:
                        try:
                            batch.append(self._queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                changes = self._merge_write_changes(
                    [write_request.changes for write_request in batch]
                )
                try:
                    await self._apply_changes(changes)
                    for queued_request in batch:
                        if not queued_request.done.done():
                            queued_request.done.set_result(None)
                except Exception as err:  # pylint: disable=broad-except
                    for queued_request in batch:
                        if not queued_request.done.done():
                            queued_request.done.set_exception(err)
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            self._worker_task = None

    async def _async_update_data(self) -> FreshIntelliVent:
        """Fetch data from the device."""
        if not self._queue.empty():
            if self.data is None:
                raise UpdateFailed("Update skipped while writes are pending")
            return self.data

        cooldown_remaining = self._cooldown_remaining_seconds()
        if cooldown_remaining > 0:
            if self.data is None:
                raise UpdateFailed(
                    f"Update skipped during cooldown ({cooldown_remaining:.1f}s)"
                )

            _LOGGER.debug(
                "Skipping poll for %s during cooldown (%.1fs remaining)",
                self._address,
                cooldown_remaining,
            )
            return self.data

        self._poll_count += 1
        full_refresh = self.data is None
        fetch_modes = full_refresh or self._poll_count % self._mode_fetch_every == 0
        fetch_device_info = (
            full_refresh or self._poll_count % self._device_info_fetch_every == 0
        )

        async def _fetch_data(client: FreshIntelliVent) -> FreshIntelliVent:
            await client.fetch_sensor_data()
            if fetch_device_info:
                await client.fetch_device_information()
            if fetch_modes:
                await client.fetch_airing()
                await client.fetch_constant_speed()
                await client.fetch_humidity()
                await client.fetch_light_and_voc()
                await client.fetch_timer()
            return client

        try:
            client = await self._run_session_operation("poll", _fetch_data)

            if self.data is not None:
                if not fetch_modes and self.data.modes:
                    client.modes = copy.deepcopy(self.data.modes)

                if not fetch_device_info:
                    client.name = self.data.name
                    client.manufacturer = self.data.manufacturer
                    client.fw_version = self.data.fw_version
                    client.hw_version = self.data.hw_version
                    client.sw_version = self.data.sw_version

            return client
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(f"Unable to fetch data: {err}") from err

    async def _apply_changes(self, changes: dict[str, Any]) -> None:
        """Apply a set of changes to the device."""

        async def _write_changes(client: FreshIntelliVent) -> None:
            await self._apply_change_payload(client, changes)

        await self._run_session_operation("write", _write_changes)

        self._apply_optimistic_updates(changes)

    async def _run_session_operation(
        self,
        phase: str,
        operation: Callable[[FreshIntelliVent], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Run an operation in a connect/auth/disconnect session."""
        if self._session_lock is not None:
            async with self._session_lock:
                return await self._run_session_operation_locked(phase, operation)

        return await self._run_session_operation_locked(phase, operation)

    async def _run_session_operation_locked(
        self,
        phase: str,
        operation: Callable[[FreshIntelliVent], Awaitable[_ResultT]],
    ) -> _ResultT:
        """Run operation with per-entry lock acquired."""
        async with self._entry_lock:
            for attempt in range(1, _OPERATION_RETRY_ATTEMPTS + 1):
                client: FreshIntelliVent | None = None
                started_at = time.monotonic()
                cooldown_remaining = self._cooldown_remaining_seconds()
                if phase == "write" and cooldown_remaining > 0:
                    _LOGGER.debug(
                        "Delaying %s for %s by %.1fs due to cooldown",
                        phase,
                        self._address,
                        cooldown_remaining,
                    )
                    await asyncio.sleep(cooldown_remaining)
                try:
                    _LOGGER.debug(
                        "Starting %s attempt %s/%s for %s",
                        phase,
                        attempt,
                        _OPERATION_RETRY_ATTEMPTS,
                        self._address,
                    )
                    client = await self._async_connect()
                    result = await operation(client)
                    _LOGGER.debug(
                        "Completed %s attempt %s/%s for %s in %.2fs",
                        phase,
                        attempt,
                        _OPERATION_RETRY_ATTEMPTS,
                        self._address,
                        time.monotonic() - started_at,
                    )
                    return result
                except Exception as err:  # pylint: disable=broad-except
                    elapsed = time.monotonic() - started_at
                    is_transient_error = self._is_transient_error(err)
                    should_retry = (
                        attempt < _OPERATION_RETRY_ATTEMPTS and is_transient_error
                    )
                    if not should_retry:
                        if is_transient_error:
                            self._activate_cooldown()
                        _LOGGER.debug(
                            "Failed %s attempt %s/%s for %s in %.2fs: %s",
                            phase,
                            attempt,
                            _OPERATION_RETRY_ATTEMPTS,
                            self._address,
                            elapsed,
                            err,
                        )
                        raise

                    backoff = _OPERATION_RETRY_BACKOFF_SECONDS * attempt
                    _LOGGER.debug(
                        "Transient %s failure on attempt %s/%s for %s after %.2fs: %s. "
                        "Retrying in %ss",
                        phase,
                        attempt,
                        _OPERATION_RETRY_ATTEMPTS,
                        self._address,
                        elapsed,
                        err,
                        backoff,
                    )
                finally:
                    if client is not None:
                        await self._async_disconnect(client)

                await asyncio.sleep(backoff)

        raise RuntimeError("Operation retry loop exited without result")

    @staticmethod
    def _is_transient_error(err: Exception) -> bool:
        """Return True if an exception likely represents a transient BLE issue."""
        root_error: BaseException = err
        seen: set[int] = set()

        while id(root_error) not in seen:
            seen.add(id(root_error))
            next_error = root_error.__cause__ or root_error.__context__
            if next_error is None:
                break
            root_error = next_error

        if isinstance(
            root_error,
            (asyncio.TimeoutError, TimeoutError, BleakError, ConnectionError),
        ):
            return True

        if root_error.__class__.__name__ in {
            "FreshIntelliventError",
            "FreshIntelliventTimeoutError",
        }:
            return True

        if isinstance(root_error, UpdateFailed) and "Unable to find device" in str(
            root_error
        ):
            return True

        return False

    @staticmethod
    def _merge_write_changes(changesets: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge multiple queued write payloads into one payload."""

        def _merge_dict(target: dict[str, Any], source: dict[str, Any]) -> None:
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    _merge_dict(target[key], value)
                    continue
                target[key] = copy.deepcopy(value)

        merged: dict[str, Any] = {}
        for changes in changesets:
            _merge_dict(merged, changes)
        return merged

    def _activate_cooldown(self) -> None:
        """Activate cooldown after a transient failure."""
        if self._error_cooldown_seconds <= 0:
            return

        self._next_allowed_operation_at = (
            time.monotonic() + self._error_cooldown_seconds
        )
        _LOGGER.debug(
            "Activated cooldown for %s: %.1fs",
            self._address,
            self._error_cooldown_seconds,
        )

    def _cooldown_remaining_seconds(self) -> float:
        """Return remaining cooldown time in seconds."""
        return max(0.0, self._next_allowed_operation_at - time.monotonic())

    async def _async_connect(self) -> FreshIntelliVent:
        """Connect to the BLE device."""
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise UpdateFailed(f"Unable to find device: {self._address}")

        client = FreshIntelliVent(ble_device=ble_device)
        try:
            await asyncio.wait_for(client.connect(timeout=TIMEOUT), timeout=TIMEOUT)
            if self._auth_key is not None:
                await asyncio.wait_for(
                    client.authenticate(authentication_code=self._auth_key),
                    timeout=TIMEOUT,
                )
        except Exception:
            await self._async_disconnect(client)
            raise

        return client

    async def _async_disconnect(self, client: FreshIntelliVent) -> None:
        """Disconnect from the BLE device."""
        try:
            await client.disconnect()
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Couldn't disconnect %s: %s", self._address, err)

    async def _apply_change_payload(
        self, client: FreshIntelliVent, changes: dict[str, Any]
    ) -> None:
        """Apply nested payload changes to the client."""
        if "constant_speed" in changes:
            constant_speed = changes["constant_speed"]
            await client.update_constant_speed(
                enabled=bool(constant_speed[ENABLED_KEY]),
                rpm=int(constant_speed[RPM_KEY]),
            )

        if "airing" in changes:
            airing = changes["airing"]
            await client.update_airing(
                enabled=bool(airing[ENABLED_KEY]),
                minutes=int(airing[MINUTES_KEY]),
                rpm=int(airing[RPM_KEY]),
            )

        if "humidity" in changes:
            humidity = changes["humidity"]
            await client.update_humidity(
                enabled=bool(humidity[ENABLED_KEY]),
                detection=humidity[DETECTION_KEY],
                rpm=int(humidity[RPM_KEY]),
            )

        if "light_and_voc" in changes:
            light_and_voc = changes["light_and_voc"]
            light = light_and_voc["light"]
            voc = light_and_voc["voc"]
            await client.update_light_and_voc(
                light_enabled=bool(light[ENABLED_KEY]),
                light_detection=light[DETECTION_KEY],
                voc_enabled=bool(voc[ENABLED_KEY]),
                voc_detection=voc[DETECTION_KEY],
            )

        if "timer" in changes:
            timer = changes["timer"]
            delay = timer[DELAY_KEY]
            await client.update_timer(
                minutes=int(timer[MINUTES_KEY]),
                delay_enabled=bool(delay[ENABLED_KEY]),
                delay_minutes=int(delay[MINUTES_KEY]),
                rpm=int(timer[RPM_KEY]),
            )

    def _apply_optimistic_updates(self, changes: dict[str, Any]) -> None:
        """Apply changes to the cached data and notify entities."""
        if self.data is None:
            return

        modes = self.data.modes

        if "constant_speed" in changes and "constant_speed" in modes:
            modes["constant_speed"].update(changes["constant_speed"])

        if "airing" in changes and "airing" in modes:
            modes["airing"].update(changes["airing"])

        if "humidity" in changes and "humidity" in modes:
            modes["humidity"].update(changes["humidity"])

        if "light_and_voc" in changes and "light_and_voc" in modes:
            light_and_voc = changes["light_and_voc"]
            if "light" in light_and_voc and "light" in modes["light_and_voc"]:
                modes["light_and_voc"]["light"].update(light_and_voc["light"])
            if "voc" in light_and_voc and "voc" in modes["light_and_voc"]:
                modes["light_and_voc"]["voc"].update(light_and_voc["voc"])

        if "timer" in changes and "timer" in modes:
            timer_update = changes["timer"]
            delay_update = timer_update.get(DELAY_KEY, {})
            modes["timer"].update(
                {
                    MINUTES_KEY: timer_update.get(
                        MINUTES_KEY,
                        modes["timer"][MINUTES_KEY],
                    ),
                    RPM_KEY: timer_update.get(RPM_KEY, modes["timer"][RPM_KEY]),
                }
            )
            if DELAY_KEY in modes["timer"] and delay_update:
                modes["timer"][DELAY_KEY].update(delay_update)

        self.async_set_updated_data(self.data)

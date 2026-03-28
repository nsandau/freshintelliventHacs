"""Data coordinator for Fresh Intellivent Sky."""

from __future__ import annotations

import asyncio
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
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
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
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

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
                try:
                    await self._apply_changes(request.changes)
                    if not request.done.done():
                        request.done.set_result(None)
                except Exception as err:  # pylint: disable=broad-except
                    if not request.done.done():
                        request.done.set_exception(err)
                finally:
                    self._queue.task_done()
        finally:
            self._worker_task = None

    async def _async_update_data(self) -> FreshIntelliVent:
        """Fetch data from the device."""
        if not self._queue.empty():
            if self.data is None:
                raise UpdateFailed("Update skipped while writes are pending")
            return self.data

        async def _fetch_data(client: FreshIntelliVent) -> FreshIntelliVent:
            await client.fetch_sensor_data()
            await client.fetch_device_information()
            await client.fetch_airing()
            await client.fetch_constant_speed()
            await client.fetch_humidity()
            await client.fetch_light_and_voc()
            await client.fetch_timer()
            return client

        try:
            return await self._run_session_operation("poll", _fetch_data)
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
        async with self._lock:
            for attempt in range(1, _OPERATION_RETRY_ATTEMPTS + 1):
                client: FreshIntelliVent | None = None
                started_at = time.monotonic()
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
                    should_retry = (
                        attempt < _OPERATION_RETRY_ATTEMPTS
                        and self._is_transient_error(err)
                    )
                    if not should_retry:
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

"""Data coordinator for Fresh Intellivent Sky."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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

        async with self._lock:
            client = await self._async_connect()
            try:
                await client.fetch_sensor_data()
                await client.fetch_device_information()
                await client.fetch_airing()
                await client.fetch_constant_speed()
                await client.fetch_humidity()
                await client.fetch_light_and_voc()
                await client.fetch_timer()
                return client
            except Exception as err:  # pylint: disable=broad-except
                raise UpdateFailed(f"Unable to fetch data: {err}")
            finally:
                await self._async_disconnect(client)

    async def _apply_changes(self, changes: dict[str, Any]) -> None:
        """Apply a set of changes to the device."""
        async with self._lock:
            client = await self._async_connect()
            try:
                await self._apply_change_payload(client, changes)
            finally:
                await self._async_disconnect(client)

        self._apply_optimistic_updates(changes)

    async def _async_connect(self) -> FreshIntelliVent:
        """Connect to the BLE device."""
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self._address)
        if ble_device is None:
            raise UpdateFailed(f"Unable to find device: {self._address}")

        client = FreshIntelliVent(ble_device=ble_device)
        await client.connect(timeout=TIMEOUT)
        if self._auth_key is not None:
            await client.authenticate(authentication_code=self._auth_key)
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

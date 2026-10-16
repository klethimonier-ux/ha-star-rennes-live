# custom_components/star_rennes_live/coordinator_parking.py

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import StarApiError, StarClient
from .api.parking_api import StarParkingAPI

_LOGGER = logging.getLogger(__name__)


class ParkingCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for STAR park-and-ride."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: StarClient) -> None:
        self.api = StarParkingAPI(client)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="STAR Parking",
            update_interval=timedelta(seconds=120),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return await self.api.async_get_all()
        except StarApiError as err:
            raise UpdateFailed(f"Erreur API STAR (parcs relais) : {err}") from err

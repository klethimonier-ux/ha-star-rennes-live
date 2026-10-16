# custom_components/star_rennes_live/coordinator_bike.py

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.bike_api import StarBikeAPI
from .api.client import StarApiError, StarClient

_LOGGER = logging.getLogger(__name__)


class BikeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for STAR bike stations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: StarClient) -> None:
        self.api = StarBikeAPI(client)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="STAR Bike",
            update_interval=timedelta(seconds=240),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return await self.api.async_get_stations()
        except StarApiError as err:
            raise UpdateFailed(f"Erreur API STAR (vélo) : {err}") from err

"""Alertes trafic STAR des lignes de bus suivies (rafraîchies toutes les 5 minutes)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.alerts_api import active_alerts, async_get_alerts
from .api.client import StarApiError, StarClient

_LOGGER = logging.getLogger(__name__)


class AlertsCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Une requête toutes les 5 minutes, filtrée sur les lignes suivies."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: StarClient,
        line_names: list[str],
    ) -> None:
        self._client = client
        self._line_names = sorted(set(line_names))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="STAR Rennes alertes trafic",
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            records = await async_get_alerts(self._client, self._line_names)
        except StarApiError as err:
            raise UpdateFailed(f"Erreur API STAR (alertes) : {err}") from err
        return active_alerts(records, dt_util.utcnow())

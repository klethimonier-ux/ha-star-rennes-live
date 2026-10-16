from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.bus_api import StarBusAPI
from .api.client import StarApiError, StarClient
from .const import (
    DESTINATION_KEY,
    ICON_URL_KEY,
    LINE_KEY,
    LINE_NAME_KEY,
    SENS_KEY,
    STOP_NAME_KEY,
)

_LOGGER = logging.getLogger(__name__)


class StarBusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator STAR Rennes Bus (une ligne / un arrêt / un sens)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: StarClient,
        bus_entry: dict[str, Any],
    ) -> None:
        self.entry = entry

        self.line_id = bus_entry[LINE_KEY]  # ex : "0001"
        self.line_name = bus_entry[LINE_NAME_KEY]  # ex : "C1"
        self.stop_name = bus_entry[STOP_NAME_KEY]  # ex : "CHP Saint-Grégoire"
        self.destination = bus_entry[DESTINATION_KEY]
        self.sens = bus_entry[SENS_KEY]
        self.icon_url = bus_entry[ICON_URL_KEY]

        self.api = StarBusAPI(client)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"STAR Bus {self.line_name} {self.stop_name}",
            update_interval=timedelta(seconds=60),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            results = await self.api.async_get_passages(
                self.line_name,
                self.sens,
                self.stop_name,
                limit=5,
            )
        except (StarApiError, ValueError) as err:
            raise UpdateFailed(f"Erreur API STAR (bus) : {err}") from err

        # Garde-fou : uniquement les passages de l'arrêt choisi
        filtered = [rec for rec in results if rec.get("nomarret") == self.stop_name]

        # (heure de départ, suivi temps réel ?) ; « precision » vaut
        # « Temps réel » ou « Applicable » (horaire théorique)
        departs: list[tuple[datetime, bool]] = []
        for rec in filtered:
            try:
                when = datetime.fromisoformat(rec["depart"])
            except (KeyError, TypeError, ValueError):
                continue
            realtime = str(rec.get("precision") or "").lower().startswith("temps")
            departs.append((when, realtime))

        departs.sort(key=lambda item: item[0])
        departures = [when for when, _ in departs]
        _LOGGER.debug("%s : %d passage(s) à venir", self.name, len(departures))

        return {
            "next_departure": departures[0] if departures else None,
            "next_realtime": departs[0][1] if departs else None,
            "departures": departures,
            "departs": [
                {"depart": when.isoformat(), "temps_reel": realtime}
                for when, realtime in departs
            ],
            "raw": filtered,
            "icon_url": self.icon_url,
            "line_id": self.line_id,
            "line_name": self.line_name,
            "stop_name": self.stop_name,
            "destination": self.destination,
            "sens": self.sens,
        }

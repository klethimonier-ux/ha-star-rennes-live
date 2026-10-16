import logging
from datetime import datetime
from typing import Any, Dict

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from ..const import (
    DOMAIN,
    LINE_KEY,
    LINE_NAME_KEY,
    STOP_NAME_KEY,
    DESTINATION_KEY,
    SENS_KEY,
    ICON_URL_KEY,
)
from ..coordinator_bus import StarBusCoordinator

_LOGGER = logging.getLogger(__name__)


def create_bus_entities(hass, entry):
    """Création des entités Bus (coordinators créés et rafraîchis dans __init__)."""
    coordinators = hass.data[DOMAIN][entry.entry_id].get("bus", [])
    return [StarBusSensor(coordinator) for coordinator in coordinators]



class StarBusSensor(CoordinatorEntity, SensorEntity):
    """Sensor STAR Rennes Bus."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    # Ces listes changent à chaque mise à jour : inutile de les historiser
    _unrecorded_attributes = frozenset({"departures", "departs", "prochains_departs_min"})

    def __init__(self, coordinator: StarBusCoordinator):
        super().__init__(coordinator)
        self._coordinator = coordinator

        self._attr_name = f"{coordinator.stop_name} → {coordinator.destination}"
        self._attr_unique_id = (
            f"star_bus_{coordinator.line_id}_{coordinator.stop_name}_{coordinator.sens}"
        )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"bus_line_{self._coordinator.line_id}")},
            "name": f"Ligne {self._coordinator.line_name}",
            "manufacturer": "STAR Rennes",
            "model": "Bus",
        }

    @property
    def native_value(self) -> datetime | None:
        return self._coordinator.data.get("next_departure")

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data = self._coordinator.data or {}
        now = dt_util.utcnow()
        minutes = [
            max(0, int((when - now).total_seconds() // 60))
            for when in data.get("departures", [])
        ]
        return {
            "ligne": self._coordinator.line_name,
            "stop_name": self._coordinator.stop_name,
            "destination": self._coordinator.destination,
            "sens": self._coordinator.sens,
            "departures": data.get("departures", []),
            # Pour la carte : heure ISO + suivi temps réel de chaque passage
            "departs": data.get("departs", []),
            # Calculé à chaque mise à jour (toutes les 60 s)
            "prochains_departs_min": minutes,
            "temps_reel": data.get("next_realtime"),
        }

    @property
    def entity_picture(self) -> str | None:
        return self._coordinator.data.get("icon_url")

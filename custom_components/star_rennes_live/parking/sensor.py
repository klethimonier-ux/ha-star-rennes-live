import os
from ..icons import PARKING_ICON
from homeassistant.util import file as file_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..coordinator_parking import ParkingCoordinator


# ---------------------------------------------------------------------------
#  FACTORY
# ---------------------------------------------------------------------------

async def create_parking_entities(hass: HomeAssistant, entry, config):
    """Create all parking-related entities for the selected parkings."""

    parking_ids = config.get("parking_ids", [])

    if not parking_ids:
        return []

    coordinator = hass.data[DOMAIN][entry.entry_id].get("parking")
    if coordinator is None:
        return []

    entities = []

    for parking_id in parking_ids:
        entities.extend([
            StarParkingSensor(coordinator, parking_id),
        ])

    return entities


# ---------------------------------------------------------------------------
#  BASE
# ---------------------------------------------------------------------------

class StarParkingBase:
    """Base class for parking entities."""

    def __init__(self, coordinator, parking_id):
        self.coordinator = coordinator
        self.parking_id = str(parking_id)
        self._attr_device_class = "opening"


    @property
    def parking(self):
        return self.coordinator.data.get(self.parking_id, {})

    @property
    def entity_picture(self):
        return PARKING_ICON

    @property
    def device_info(self):
        prename=self.parking.get("name", f"Parking {self.parking_id}")
        return DeviceInfo(
            identifiers={(DOMAIN, f"parking_{self.parking_id}")},
            name=f"Parc relais {prename}",
            manufacturer="STAR Rennes",
            model="Parc relais",
        )


# ---------------------------------------------------------------------------
#  ENTITÉS
# ---------------------------------------------------------------------------

class StarParkingSensor(StarParkingBase, SensorEntity):
    """Entité unique pour un parking : ouvert/fermé + attributs."""

    _attr_icon = "mdi:parking"

    @property
    def name(self):
        name = f"Parc relais {self.parking.get('name', self.parking_id)}"
        return name

    @property
    def native_value(self):
        etat = self.parking.get("etat_ouverture", "").lower()
        return "Ouvert" if etat in ("ouvert", "en service") else "Fermé"

    @property
    def icon(self):
        return "mdi:parking" if self.native_value == "Ouvert" else "mdi:parking-off"

    @property
    def unique_id(self):
        return f"star_parking_{self.parking_id}"

    @property
    def entity_picture(self):
        return PARKING_ICON

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def extra_state_attributes(self):
        libres = self.parking.get("places_disponibles")
        capacite = self.parking.get("capacite")

        if libres is not None and capacite:
            occupation = round((capacite - libres) / capacite * 100)
        else:
            occupation = None

        return {
            "etat_ouverture": self.parking.get("etat_ouverture"),
            "places_libres": libres,
            "capacite": capacite,
            "places_libres_libelle": f"{libres} places libres",
            "capacite_libelle":  f"Capacité : {capacite} places",
            "occupation_pct": occupation,
            "occupation_pct_libelle": f"Occupé à {occupation}%",
        }

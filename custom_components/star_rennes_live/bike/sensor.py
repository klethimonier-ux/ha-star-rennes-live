import os
from ..icons import VELO_ICON

from homeassistant.helpers import device_registry as dr
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..coordinator_bike import BikeCoordinator


# ---------------------------------------------------------------------------
#  FACTORY
# ---------------------------------------------------------------------------

async def create_bike_entities(hass: HomeAssistant, entry, config):
    station_ids = config.get("bike_stations", [])

    # Le coordinator n'existe que si au moins une station est suivie
    coordinator = hass.data[DOMAIN][entry.entry_id].get("bike")

    # Device parent STAR Vélo
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "bike_root")},
        name="STAR Vélo",
        manufacturer="STAR Rennes",
        model="Réseau vélo",
    )

    if not station_ids or coordinator is None:
        return [StarBikePlaceholderSensor()]

    return [
        StarBikeStationSensor(coordinator, station_id)
        for station_id in station_ids
    ]


# ---------------------------------------------------------------------------
#  PLACEHOLDER
# ---------------------------------------------------------------------------

class StarBikePlaceholderSensor(SensorEntity):
    _attr_name = "STAR Vélo – aucune station sélectionnée"
    _attr_unique_id = "star_rennes_bike_placeholder"
    _attr_icon = "mdi:bike"

    @property
    def native_value(self):
        return "Aucune station sélectionnée"

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, "bike_root")},
            name="STAR Vélo",
            manufacturer="STAR Rennes",
            model="Réseau vélo",
        )


# ---------------------------------------------------------------------------
#  ENTITÉ UNIQUE PAR STATION
# ---------------------------------------------------------------------------

class StarBikeStationSensor(SensorEntity):
    """Une seule entité par station vélo."""

    def __init__(self, coordinator, station_id):
        self.coordinator = coordinator
        self.station_id = str(station_id)
        self.hass = coordinator.hass

    @property
    def station(self):
        return self.coordinator.data.get(self.station_id, {})

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def name(self):
        return self.station.get("name", f"Station {self.station_id}")

    @property
    def unique_id(self):
        return f"star_bike_station_{self.station_id}"

    @property
    def native_value(self):
        """État principal : Ouvert / En panne / Maintenance / Fermé."""
        status = (self.station.get("status") or "").lower()

        if "panne" in status:
            return "En panne"
        if "fonctionnement" in status:
            return "Ouvert"
        if "maintenance" in status:
            return "Maintenance"
        return "Fermé"

    @property
    def extra_state_attributes(self):
        """Attributs : vélos, places, statut brut."""
        return {
            "velos_disponibles": self.station.get("bikes"),
            "places_libres": self.station.get("docks"),
            "velos_disponibles_libelle": f"{self.station.get('bikes')} vélos",
            "places_libres_libelle": f"{self.station.get('docks')} places",
            "statut_brut": self.station.get("status"),
        }

    @property
    def icon(self):
        """Icône dynamique selon l'état."""
        state = self.native_value
        if state == "Ouvert":
            return "mdi:bicycle"
        if state == "En panne":
            return "mdi:alert-circle"
        if state == "Maintenance":
            return "mdi:tools"
        return "mdi:close-octagon"

    @property
    def entity_picture(self):
        return VELO_ICON

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, f"bike_station_{self.station_id}")},
            name=self.station.get("name", f"Station {self.station_id}"),
            manufacturer="STAR Rennes",
            model="Station vélo",
            via_device=(DOMAIN, "bike_root"),
        )

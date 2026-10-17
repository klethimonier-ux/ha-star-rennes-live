from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..api.gtfs_metro import _normalize_name
from ..api.metro_api import line_is_disrupted
from ..const import DOMAIN, MANUFACTURER
from ..coordinator_metro import MetroCoordinator
from ..icons import METRO_A_ICON, METRO_B_ICON, METRO_GENERIC_ICON

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  FACTORY : appelée depuis sensor.py (racine)
# ---------------------------------------------------------------------------

def create_metro_entities(hass: HomeAssistant, entry, config):
    """Create metro entities for the selected lines and stations only."""

    coordinator: MetroCoordinator | None = hass.data[DOMAIN][entry.entry_id].get("metro")
    if coordinator is None or not coordinator.data:
        _LOGGER.error("MetroCoordinator returned no data")
        return []

    lines = coordinator.data.get("lines", {})
    stations = coordinator.data.get("stations", {})

    selected_lines = set(config.get("metro_lines", []))
    selected_stations = {str(sid) for sid in config.get("metro_stations", [])}

    # 0) Synthèse des perturbations du réseau métro (toujours créée)
    entities: list[SensorEntity] = [StarMetroPerturbationsSensor(coordinator, entry.entry_id)]

    # 1) LIGNES sélectionnées
    for code in lines:
        if code in selected_lines:
            entities.append(StarMetroLineSensor(coordinator, entry.entry_id, code))

    # 2) STATIONS sélectionnées + leurs équipements
    for sid, station in stations.items():
        if sid not in selected_stations:
            continue

        entities.append(
            StarMetroStationSensor(
                coordinator,
                entry.entry_id,
                sid,
                hass.data[DOMAIN][entry.entry_id].get("metro_rames"),
            )
        )

        for equipment in station.get("equipments", []):
            entities.append(
                StarMetroEquipmentSensor(coordinator, entry.entry_id, equipment)
            )

    _LOGGER.debug("Created %d metro sensors", len(entities))
    return entities


# =============================================================================
# BASE : les entités relisent toujours coordinator.data (jamais une copie)
# =============================================================================
class _StarMetroEntity(CoordinatorEntity[MetroCoordinator], SensorEntity):
    """Base commune des capteurs métro."""

    def _section(self, section: str, key: str) -> dict[str, Any] | None:
        data = self.coordinator.data or {}
        return data.get(section, {}).get(key)


# =============================================================================
# SENSOR : LIGNE
# =============================================================================
class StarMetroLineSensor(_StarMetroEntity):
    """Sensor pour une ligne de métro (A ou B)."""

    _attr_icon = "mdi:subway"

    def __init__(self, coordinator: MetroCoordinator, entry_id: str, line_code: str) -> None:
        super().__init__(coordinator)
        self._line_code = line_code

        self._attr_unique_id = f"{entry_id}_metro_line_{line_code}"
        self._attr_name = f"Métro Ligne {line_code}"

    @property
    def _line(self) -> dict[str, Any] | None:
        return self._section("lines", self._line_code)

    @property
    def available(self) -> bool:
        return super().available and self._line is not None

    @property
    def native_value(self):
        return (self._line or {}).get("state")

    @property
    def extra_state_attributes(self):
        line = self._line or {}
        return {
            "line_id": line.get("id"),
            "line_code": self._line_code,
            "perturbation": line_is_disrupted(line.get("state")),
            "pannedepuis": line.get("pannedepuis"),
            "finpanneprevue": line.get("finpanneprevue"),
            "lastupdate": line.get("lastupdate"),
        }

    @property
    def entity_picture(self):
        if self._line_code == "A":
            return METRO_A_ICON
        if self._line_code == "B":
            return METRO_B_ICON
        return METRO_GENERIC_ICON

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, f"metro_line_{self._line_code}")},
            name=f"STAR Métro – Ligne {self._line_code}",
            manufacturer=MANUFACTURER,
            model="Ligne de métro",
        )


# =============================================================================
# SENSOR : STATION
# =============================================================================
class StarMetroStationSensor(_StarMetroEntity):
    """Sensor pour une station de métro."""

    _attr_icon = "mdi:subway-variant"
    _unrecorded_attributes = frozenset({"prochains_passages"})

    def __init__(
        self,
        coordinator: MetroCoordinator,
        entry_id: str,
        station_id: str,
        trains_coordinator=None,
    ) -> None:
        super().__init__(coordinator)
        self._station_id = station_id
        # Horaires GTFS (prochains passages) : second coordinator, facultatif
        self._trains = trains_coordinator

        # Le nom est figé à la création (la topologie est statique)
        station_name = (self._station or {}).get("name")
        self._station_name = station_name

        self._attr_unique_id = f"{entry_id}_metro_station_{station_id}"
        self._attr_name = f"Station {station_name}"

    @property
    def _station(self) -> dict[str, Any] | None:
        return self._section("stations", self._station_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._trains is not None:
            self.async_on_remove(self._trains.async_add_listener(self._handle_coordinator_update))

    @property
    def _passages(self) -> list[dict[str, Any]]:
        data = getattr(self._trains, "data", None) or {}
        return data.get("passages", {}).get(_normalize_name(self._station_name or ""), [])

    @property
    def available(self) -> bool:
        return super().available and self._station is not None

    @property
    def native_value(self):
        return (self._station or {}).get("state")

    @property
    def entity_picture(self):
        # Icône de la ligne (générique pour une correspondance A/B)
        line_code = (self._station or {}).get("line_code")
        if line_code == "A":
            return METRO_A_ICON
        if line_code == "B":
            return METRO_B_ICON
        return METRO_GENERIC_ICON

    @property
    def extra_state_attributes(self):
        station = self._station or {}
        coords = station.get("coords") or {}
        return {
            "station_id": self._station_id,
            "station_name": self._station_name,
            "line_code": station.get("line_code"),
            "etats_par_ligne": station.get("states_by_line"),
            "fermee": station.get("closed", False),
            "lignes_fermees": station.get("closed_lines", []),
            # Fermeture déduite : tous les ascenseurs et escaliers HS (non publiée par STAR)
            "fermeture_deduite": station.get("closed_by_equipment", []),
            # Pour la carte : par direction, ascenseurs / escaliers / distributeurs
            "directions": station.get("directions", []),
            # Prochains départs par sens (horaires théoriques GTFS)
            "prochains_passages": self._passages,
            "lastupdate": station.get("lastupdate"),
            "realtime_available": station.get("realtime_available"),
            "lat": coords.get("lat"),
            "lon": coords.get("lon"),
        }

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, f"metro_station_{self._station_id}")},
            name=f"Station de métro {self._station_name}",
            manufacturer=MANUFACTURER,
            model="Station de métro",
        )


# =============================================================================
# SENSOR : ÉQUIPEMENT
# =============================================================================
class StarMetroEquipmentSensor(_StarMetroEntity):
    """Sensor pour un équipement de station (ascenseur, escalator…)."""

    def __init__(
        self, coordinator: MetroCoordinator, entry_id: str, equipment: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._equipment_id = equipment["id"]
        self._station_id = equipment.get("station_id")
        self._type_short = equipment.get("type_short")

        # unique_id inchangé : seuls les noms affichés changent, les
        # entity_id et les automatisations existantes restent valides.
        self._attr_unique_id = f"{entry_id}_metro_equipment_{self._equipment_id}"

        # Nom de station + identifiant : certains équipements portent le même
        # nom (ex. deux ascenseurs « Quai vers Kennedy <-> Salle des billets »)
        station_name = equipment.get("station_name") or f"Station {self._station_id}"
        self._attr_name = (
            f"{station_name} – {self._type_short} – {equipment.get('name')} "
            f"({self._equipment_id})"
        )

    @property
    def _equipment(self) -> dict[str, Any] | None:
        return self._section("equipments", self._equipment_id)

    @property
    def available(self) -> bool:
        return super().available and self._equipment is not None

    @property
    def icon(self):
        if self._type_short == "ASC":
            return "mdi:elevator-passenger"
        if self._type_short == "PR_ASC":  # ascenseur de parc relais
            return "mdi:elevator"
        if self._type_short == "ESC":
            return "mdi:escalator"
        if self._type_short == "DAT":
            return "mdi:ticket-confirmation"
        return "mdi:help-circle"

    @property
    def native_value(self):
        return (self._equipment or {}).get("state")

    @property
    def extra_state_attributes(self):
        equipment = self._equipment or {}
        coords = equipment.get("coords") or {}
        return {
            "equipment_id": self._equipment_id,
            "station_id": self._station_id,
            "type": equipment.get("type"),
            "type_short": self._type_short,
            "lastupdate": equipment.get("lastupdate"),
            "lat": coords.get("lat"),
            "lon": coords.get("lon"),
        }

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, f"metro_station_{self._station_id}")},
        )


# =============================================================================
# SENSOR : SYNTHÈSE DES PERTURBATIONS (réseau métro)
# =============================================================================
class StarMetroPerturbationsSensor(_StarMetroEntity):
    """Nombre de perturbations en cours + liste prête à afficher.

    Couvre les deux lignes et toutes les stations du réseau, même celles qui
    ne sont pas suivies (ex. une station fermée sur le trajet).
    """

    _attr_icon = "mdi:alert-circle-outline"
    _attr_native_unit_of_measurement = "perturbations"
    _attr_state_class = "measurement"

    def __init__(self, coordinator: MetroCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_metro_perturbations"
        self._attr_name = "STAR Perturbations"

    @property
    def _items(self) -> list[dict[str, Any]]:
        return (self.coordinator.data or {}).get("perturbations", [])

    @property
    def native_value(self):
        return len(self._items)

    @property
    def icon(self):
        return "mdi:alert-circle" if self._items else "mdi:check-circle-outline"

    @property
    def extra_state_attributes(self):
        return {"perturbations": self._items}

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, "metro_network")},
            name="STAR Métro – Réseau",
            manufacturer=MANUFACTURER,
            model="Réseau métro",
        )


# =============================================================================
# SENSOR : RAMES EN CIRCULATION (schéma linéaire)
# =============================================================================
def create_metro_trains_entities(hass: HomeAssistant, entry) -> list[SensorEntity]:
    coordinator = hass.data[DOMAIN][entry.entry_id].get("metro_rames")
    return [StarMetroTrainsSensor(coordinator, entry.entry_id)] if coordinator else []


class StarMetroTrainsSensor(CoordinatorEntity, SensorEntity):
    """Nombre de rames en circulation + données pour la carte star-metro-card.

    Positions estimées d'après les horaires théoriques (GTFS) : la carte
    interpole chaque seconde entre les heures de passage aux stations.
    """

    _attr_icon = "mdi:subway-variant"
    _attr_native_unit_of_measurement = "rames"
    _attr_state_class = "measurement"
    # Données volumineuses et changeantes : hors historique
    _unrecorded_attributes = frozenset({"lignes", "rames", "t0", "en_service"})

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_metro_rames"
        self._attr_name = "STAR Métro – Rames"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        return sum(data.get("running", {}).values())

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        sources = {line.get("source") for line in data.get("lines", {}).values()}
        return {
            "source": "temps_reel" if "temps_reel" in sources else "horaires_theoriques",
            "gtfs_charge_le": data.get("loaded_at"),
            "en_service": data.get("running", {}),
            "t0": data.get("t0"),
            "lignes": data.get("lines", {}),
            "rames": data.get("trains", []),
        }

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, "metro_network")},
            name="STAR Métro – Réseau",
            manufacturer=MANUFACTURER,
            model="Réseau métro",
        )

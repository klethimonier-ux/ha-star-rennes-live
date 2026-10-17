from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bike.sensor import create_bike_entities
from .bus.sensor import create_bus_entities
from .const import get_entry_config
from .metro.sensor import create_metro_entities, create_metro_trains_entities
from .parking.sensor import create_parking_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    entities = []

    # Config effective : options (prioritaire) ou data (fallback)
    config = get_entry_config(entry)

    if config.get("metro_lines") or config.get("metro_stations"):
        entities.extend(create_metro_entities(hass, entry, config))

    if config.get("bike"):
        entities.extend(create_bike_entities(hass, entry, config))

    if config.get("parking_ids"):
        entities.extend(create_parking_entities(hass, entry, config))

    entities.extend(create_bus_entities(hass, entry))

    # Rames de métro (schéma linéaire), si l'option est cochée
    if config.get("metro_rames"):
        entities.extend(create_metro_trains_entities(hass, entry))

    if entities:
        async_add_entities(entities)

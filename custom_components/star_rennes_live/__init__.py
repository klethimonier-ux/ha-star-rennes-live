from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.client import StarClient
from .const import CONF_API_KEY, DOMAIN, get_entry_config
from .coordinator_bike import BikeCoordinator
from .coordinator_bus import StarBusCoordinator
from .coordinator_metro import MetroCoordinator
from .coordinator_metro_trains import MetroTrainsCoordinator
from .coordinator_parking import ParkingCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

# Intégration configurée uniquement via l'interface
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Carte Lovelace livrée avec l'intégration
CARD_FILE = Path(__file__).parent / "frontend" / "star-trajets-card.js"
CARD_URL = f"/{DOMAIN}/star-trajets-card.js"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Rend la carte custom:star-trajets-card disponible dans les tableaux de bord."""
    # La date du fichier sert de version : le navigateur recharge la carte
    # dès qu'elle change, sans vider le cache à la main.
    mtime = await hass.async_add_executor_job(lambda: int(CARD_FILE.stat().st_mtime))
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), False)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={mtime}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up STAR Rennes from a config entry."""
    config = get_entry_config(entry)
    client = StarClient(async_get_clientsession(hass), config.get(CONF_API_KEY) or None)

    # On ne crée que les coordinators des services réellement suivis :
    # rien n'est interrogé pour un service sans sélection.
    coordinators: dict[str, Any] = {}

    if config.get("bike_stations"):
        coordinators["bike"] = BikeCoordinator(hass, entry, client)

    if config.get("metro_lines") or config.get("metro_stations"):
        coordinators["metro"] = MetroCoordinator(
            hass,
            entry,
            client,
            station_ids=config.get("metro_stations", []),
        )

    if config.get("parking_ids"):
        coordinators["parking"] = ParkingCoordinator(hass, entry, client)

    coordinators["bus"] = [
        StarBusCoordinator(hass, entry, client, bus_entry)
        for bus_entry in config.get("bus_entries", [])
    ]

    # Premier rafraîchissement (obligatoire), en parallèle
    to_refresh = [c for key, c in coordinators.items() if key not in ("bus", "metro_rames")]
    to_refresh.extend(coordinators["bus"])
    await asyncio.gather(*(c.async_config_entry_first_refresh() for c in to_refresh))

    # Rames de métro : GTFS de ~8 Mo, chargé en tâche de fond pour ne pas
    # ralentir le démarrage (le capteur reste indisponible le temps du chargement)
    # Aussi créé quand des stations sont suivies : prochains passages temps réel
    if config.get("metro_rames") or config.get("metro_stations"):
        trains = MetroTrainsCoordinator(
            hass,
            entry,
            async_get_clientsession(hass),
            client,
            # GTFS (8 Mo / jour) seulement pour le schéma ; les passages en
            # station viennent du temps réel
            load_gtfs=bool(config.get("metro_rames")),
        )
        coordinators["metro_rames"] = trains
        entry.async_create_background_task(
            hass, trains.async_refresh(), "star_rennes_live_gtfs_metro"
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinators

    # Recharger l'intégration quand les options changent
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Applique immédiatement les nouvelles options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok

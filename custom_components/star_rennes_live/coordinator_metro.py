from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.client import StarApiError, StarClient
from .api.gtfs_metro import station_served
from .api.metro_api import (
    StarMetroAPI,
    build_metro_structure,
    drop_off_service_closures,
    within_service_hours,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# La topologie des stations est statique : on la recharge une fois par jour.
TOPOLOGY_MAX_AGE = timedelta(hours=24)
# Équipements de tout le réseau (fermetures déduites, ~3 requêtes) : toutes les 5 min.
NETWORK_EQUIPMENTS_MAX_AGE = timedelta(minutes=5)


class MetroCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for STAR Rennes metro data.

    Par cycle : 2 requêtes (lignes + état des stations) ou 3 requêtes
    (+ équipements des seules stations suivies) quand des stations sont
    suivies. La topologie est mise en cache.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: StarClient,
        station_ids: list[str] | None = None,
    ) -> None:
        self.api = StarMetroAPI(client)
        self._station_ids = [str(sid) for sid in station_ids or []]
        self._fetch_stations = bool(self._station_ids)
        self._topology: dict[str, dict[str, Any]] | None = None
        self._topology_fetched_at: datetime | None = None
        self._network_equipments: dict[str, dict[str, Any]] = {}
        self._network_fetched_at: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="STAR Rennes Metro",
            update_interval=timedelta(seconds=30),
        )

    async def _async_get_topology(self) -> dict[str, dict[str, Any]]:
        now = dt_util.utcnow()
        if (
            self._topology is not None
            and self._topology_fetched_at is not None
            and now - self._topology_fetched_at < TOPOLOGY_MAX_AGE
        ):
            return self._topology

        try:
            self._topology = await self.api.async_get_stations_topology()
            self._topology_fetched_at = now
        except StarApiError:
            if self._topology is None:
                raise
            # On garde l'ancienne topologie, elle ne change quasiment jamais.
            _LOGGER.debug("Rafraîchissement de la topologie métro impossible, cache conservé")
        return self._topology

    async def _async_get_network_equipments(self) -> dict[str, dict[str, Any]]:
        """Équipements de toutes les stations, rafraîchis toutes les 5 minutes."""
        now = dt_util.utcnow()
        if self._network_fetched_at is not None and now - self._network_fetched_at < NETWORK_EQUIPMENTS_MAX_AGE:
            return self._network_equipments
        try:
            self._network_equipments = await self.api.async_get_equipments()
            self._network_fetched_at = now
        except StarApiError as err:
            # Facultatif : sans eux, seules les fermetures publiées sont signalées
            _LOGGER.debug("Équipements du réseau indisponibles : %s", err)
        return self._network_equipments

    async def _async_update_data(self) -> dict[str, Any]:
        # Lignes + état des stations sont toujours lus (2 requêtes) : ils
        # servent au capteur de perturbations (station fermée n'importe où
        # sur le réseau). Topologie et équipements : stations suivies seulement.
        try:
            if not self._fetch_stations:
                lines, stations_rt, network = await asyncio.gather(
                    self.api.async_get_lines(),
                    self.api.async_get_stations_realtime(),
                    self._async_get_network_equipments(),
                )
                return self._without_night_closures(
                    build_metro_structure(lines, {}, stations_rt, dict(network), _format_time)
                )

            topology = await self._async_get_topology()
            lines, stations_rt, equipments, network = await asyncio.gather(
                self.api.async_get_lines(),
                self.api.async_get_stations_realtime(),
                self.api.async_get_equipments(self._station_ids),
                self._async_get_network_equipments(),
            )
            # Stations suivies : état à 30 s ; autres stations : état à 5 min
            equipments = {**network, **equipments}
        except StarApiError as err:
            raise UpdateFailed(f"Erreur API STAR (métro) : {err}") from err

        return self._without_night_closures(
            build_metro_structure(lines, topology, stations_rt, equipments, _format_time)
        )

    # ------------------------------------------------------------------
    def _without_night_closures(self, data: dict[str, Any]) -> dict[str, Any]:
        """Les stations ferment la nuit : ce n'est pas une perturbation."""
        data["perturbations"] = drop_off_service_closures(
            data.get("perturbations", []), self._is_served
        )
        return data

    def _is_served(self, line: str, station: str) -> bool:
        """Des rames sont-elles prévues à cette station en ce moment ?

        Horaires GTFS quand le schéma des rames est activé, sinon heures de
        service de la fiche horaire.
        """
        now = dt_util.utcnow()
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        schedule = getattr(entry_data.get("metro_rames"), "schedule", None)
        if schedule is not None:
            served = station_served(schedule, now, line, station)
            if served is not None:
                return served
        return within_service_hours(dt_util.as_local(now))


def _format_time(value: Any) -> str | None:
    """« 22:30 » (heure locale) à partir d'une date ISO ; sinon la valeur brute."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return dt_util.as_local(parsed).strftime("%H:%M")

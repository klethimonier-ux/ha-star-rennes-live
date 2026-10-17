"""Coordinator « métro en circulation » : temps réel + horaires GTFS.

- Temps réel (toutes les 30 s, 1 requête) : deux prochains passages à chaque
  quai. Sert aux prochains passages des stations et à la position des rames.
- GTFS (8 Mo, une fois par jour, seulement si le schéma est activé) : ordre
  des stations, temps de parcours, et secours quand le temps réel manque.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.client import StarApiError, StarClient
from .api.gtfs_metro import (
    GTFS_URL,
    MetroSchedule,
    codes_by_name,
    next_departures,
    parse_gtfs_metro,
    station_code,
    train_position,
    trains_window,
)
from .api.metro_api import StarMetroAPI
from .api.metro_realtime import (
    DATASET as REALTIME_DATASET,
    build_realtime_trains,
    is_fresh,
    parse_passages,
    passages_by_station,
)

_LOGGER = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=180)
RETRY_DELAY = timedelta(minutes=30)  # après un échec de téléchargement du GTFS
RELOAD_AFTER = timedelta(hours=20)  # le GTFS est régénéré chaque nuit avant 2 h
RELOAD_FROM_HOUR = 3  # heure locale minimale pour recharger


class MetroTrainsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Prochains passages (temps réel) et rames en circulation."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        client: StarClient | None = None,
        load_gtfs: bool = True,
    ) -> None:
        self._session = session
        self._client = client
        self._load_gtfs = load_gtfs
        self._codes: dict[str, str] = {}
        self._schedule: MetroSchedule | None = None
        self._loaded_at: datetime | None = None
        self._last_attempt: datetime | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="STAR Rennes Metro rames",
            update_interval=timedelta(seconds=30),
        )

    # ------------------------------------------------------------------ GTFS
    def _needs_reload(self, now: datetime) -> bool:
        if self._schedule is None or self._loaded_at is None:
            return True
        local_hour = dt_util.as_local(now).hour
        return now - self._loaded_at >= RELOAD_AFTER and local_hour >= RELOAD_FROM_HOUR

    @property
    def schedule(self) -> MetroSchedule | None:
        """Horaires GTFS chargés (None tant qu'ils ne le sont pas)."""
        return self._schedule

    async def _async_load_schedule(self, now: datetime) -> None:
        """Charge le GTFS si nécessaire. Lève UpdateFailed si jamais chargé."""
        if not self._needs_reload(now):
            return
        if self._last_attempt is not None and now - self._last_attempt < RETRY_DELAY:
            if self._schedule is None:
                raise UpdateFailed("Horaires GTFS du métro indisponibles, nouvel essai plus tard")
            return

        self._last_attempt = now
        try:
            async with self._session.get(GTFS_URL, timeout=DOWNLOAD_TIMEOUT) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"GTFS : HTTP {resp.status}")
                data = await resp.read()
            schedule = await self.hass.async_add_executor_job(parse_gtfs_metro, data)
            if not schedule.lines or not schedule.trips:
                raise UpdateFailed("GTFS : aucune ligne de métro trouvée")
        except (aiohttp.ClientError, TimeoutError, zipfile.BadZipFile, KeyError, ValueError, UpdateFailed) as err:
            if self._schedule is None:
                raise UpdateFailed(f"Chargement du GTFS métro impossible : {err}") from err
            _LOGGER.warning("Mise à jour du GTFS impossible, horaires précédents conservés : %s", err)
            return

        self._schedule = schedule
        self._loaded_at = now
        await self._async_load_codes()
        _LOGGER.debug("GTFS métro chargé : %d lignes, %d courses", len(schedule.lines), len(schedule.trips))

    async def _async_load_codes(self) -> None:
        """Acronymes officiels des stations (1 requête, au chargement du GTFS)."""
        if self._client is None:
            return
        try:
            topology = await StarMetroAPI(self._client).async_get_stations_topology()
        except StarApiError as err:
            _LOGGER.debug("Acronymes des stations indisponibles : %s", err)
            return
        self._codes = codes_by_name(topology) or self._codes

    # ------------------------------------------------------------- temps réel
    async def _async_fetch_realtime(self, now: datetime) -> list | None:
        if self._client is None:
            return None
        try:
            records = await self._client.async_get_all(REALTIME_DATASET)
        except StarApiError as err:
            _LOGGER.debug("Temps réel métro indisponible : %s", err)
            return None
        if not records or not is_fresh(records, now):
            _LOGGER.debug("Temps réel métro vide ou périmé")
            return None
        return parse_passages(records, now=now)

    # ---------------------------------------------------------------- cycle
    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        realtime = await self._async_fetch_realtime(now)

        gtfs_error: UpdateFailed | None = None
        if self._load_gtfs:
            try:
                await self._async_load_schedule(now)
            except UpdateFailed as err:
                gtfs_error = err
        if realtime is None and self._schedule is None:
            raise gtfs_error or UpdateFailed("Ni temps réel ni horaires disponibles pour le métro")

        t0 = int(now.timestamp())
        trains, running, sources = self._trains(now, realtime, t0)
        return {
            "passages": self._passages(now, realtime),
            "realtime": realtime is not None,
            "t0": t0,
            "trains": trains,
            "running": running,
            "lines": self._lines(sources),
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
        }

    def _passages(self, now: datetime, realtime: list | None) -> dict[str, list[dict]]:
        """Prochains passages : temps réel, sinon horaires théoriques (heures ISO)."""
        passages: dict[str, list[dict]] = {}
        if self._schedule is not None:
            for station, items in next_departures(self._schedule, now).items():
                passages[station] = [{**p, "temps_reel": False} for p in items]
        if realtime is not None:
            passages.update(passages_by_station(realtime, now))
        return {
            station: [
                {**p, "departs": [dt_util.utc_from_timestamp(t).isoformat() for t in p["departs"]]}
                for p in items
            ]
            for station, items in passages.items()
        }

    def _trains(self, now: datetime, realtime: list | None, t0: int) -> tuple[list[dict], dict[str, int], dict[str, str]]:
        """Rames : temps réel par ligne quand il est disponible, sinon GTFS.

        Retourne (rames, rames en circulation par ligne, source par ligne).
        """
        if self._schedule is None:
            return [], {}, {}
        _, theoretical, _ = trains_window(self._schedule, now)
        live = build_realtime_trains(realtime, self._schedule, now) if realtime else {}
        trains: list[dict] = []
        sources: dict[str, str] = {}
        for code in self._schedule.lines:
            if live.get(code):
                sources[code] = "temps_reel"
                trains.extend(
                    {"l": code, "h": t["h"], "d": t["d"], "s": [[i, int(a - t0), int(d - t0)] for i, a, d in t["s"]]}
                    for t in live[code]
                )
            else:
                sources[code] = "horaires_theoriques"
                trains.extend(t for t in theoretical if t["l"] == code)
        running = {
            code: sum(1 for t in trains if t["l"] == code and train_position(t["s"], 0) is not None)
            for code in self._schedule.lines
        }
        return trains, running, sources

    def _lines(self, sources: dict[str, str]) -> dict[str, dict]:
        """Description des lignes pour la carte (stations, acronymes, source des rames)."""
        if self._schedule is None:
            return {}
        return {
            code: {
                "nom": line.name,
                "couleur": line.color,
                "stations": line.stations,
                "codes": [station_code(n, self._codes) for n in line.stations],
                "source": sources.get(code),
            }
            for code, line in self._schedule.lines.items()
        }

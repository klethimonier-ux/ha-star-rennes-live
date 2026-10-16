"""Horaires théoriques du métro (GTFS STAR) et position estimée des rames.

Le GTFS bus + métro de la STAR est un zip d'environ 8 Mo régénéré chaque
nuit. On n'en garde que le métro (lignes a et b) : stations dans l'ordre,
courses et calendrier. La position d'une rame s'obtient en interpolant entre
ses heures de passage aux stations.

Module sans dépendance Home Assistant (testable seul).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

GTFS_URL = "https://eu.ftp.opendatasoft.com/star/gtfs/GTFS_STAR_BUS_METRO_EN_COURS.zip"

# route_type GTFS : 1 = métro ; 400-404 = types étendus « urban railway »
METRO_ROUTE_TYPES = {"1", "400", "401", "402", "403", "404"}
DEFAULT_TIMEZONE = "Europe/Paris"
_HEX_COLOR = re.compile(r"^[0-9A-Fa-f]{6}$")


@dataclass
class MetroLine:
    code: str  # "A", "B"
    name: str  # nom court GTFS ("a")
    color: str | None  # "#e2001a"
    stations: list[str]  # noms, dans l'ordre de la ligne


@dataclass
class MetroTrip:
    line: str
    service_id: str
    headsign: str
    direction: int  # +1 : vers la dernière station de la liste, -1 : vers la première
    stops: list[tuple[int, int, int]]  # (index station, arrivée s, départ s) depuis le début du jour de service
    block_id: str | None = None


@dataclass
class MetroSchedule:
    timezone: str
    lines: dict[str, MetroLine]
    trips: list[MetroTrip]
    calendar: dict[str, tuple[tuple[bool, ...], date, date]] = field(default_factory=dict)
    exceptions: dict[str, dict[date, int]] = field(default_factory=dict)
    # (ligne, index min, index max) -> temps de parcours médian entre deux stations voisines (s)
    segment_times: dict[tuple[str, int, int], float] = field(default_factory=dict)

    def services_on(self, day: date) -> set[str]:
        """service_id actifs un jour donné (calendar.txt + calendar_dates.txt)."""
        active = {
            sid
            for sid, (weekdays, start, end) in self.calendar.items()
            if start <= day <= end and weekdays[day.weekday()]
        }
        for sid, dates in self.exceptions.items():
            kind = dates.get(day)
            if kind == 1:
                active.add(sid)
            elif kind == 2:
                active.discard(sid)
        return active


# ---------------------------------------------------------------------------
# LECTURE DU ZIP
# ---------------------------------------------------------------------------
def _reader(zf: zipfile.ZipFile, filename: str) -> Iterator[dict[str, str]] | None:
    member = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1] == filename), None)
    if member is None:
        return None
    stream = io.TextIOWrapper(zf.open(member), encoding="utf-8-sig", newline="")
    reader = csv.reader(stream)
    header = [h.strip() for h in next(reader, [])]

    def rows() -> Iterator[dict[str, str]]:
        for row in reader:
            yield {key: (row[i].strip() if i < len(row) else "") for i, key in enumerate(header)}

    return rows()


def _seconds(value: str) -> int | None:
    """« 25:10:00 » -> 90600 (les horaires GTFS peuvent dépasser 24 h)."""
    if not value:
        return None
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def parse_gtfs_metro(data: bytes) -> MetroSchedule:
    """Extrait le métro d'un zip GTFS (fonction bloquante : à lancer dans un thread)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Fuseau du réseau
        timezone = DEFAULT_TIMEZONE
        agency = _reader(zf, "agency.txt")
        if agency is not None:
            first = next(agency, None)
            if first and first.get("agency_timezone"):
                timezone = first["agency_timezone"]

        # Lignes de métro
        routes: dict[str, tuple[str, str, str | None]] = {}
        for row in _reader(zf, "routes.txt") or []:
            if row.get("route_type") not in METRO_ROUTE_TYPES:
                continue
            name = row.get("route_short_name") or row.get("route_long_name") or row["route_id"]
            color = row.get("route_color", "")
            routes[row["route_id"]] = (
                name.strip().upper(),
                name.strip(),
                f"#{color.lower()}" if _HEX_COLOR.match(color) else None,
            )
        if not routes:
            return MetroSchedule(timezone, {}, [])

        # Courses de métro
        trips_info: dict[str, tuple[str, str, str]] = {}
        blocks: dict[str, str] = {}  # trip_id -> block_id (véhicule), si fourni
        for row in _reader(zf, "trips.txt") or []:
            route = routes.get(row.get("route_id"))
            if route is None:
                continue
            trips_info[row["trip_id"]] = (route[0], row.get("service_id", ""), row.get("trip_headsign", ""))
            if row.get("block_id"):
                blocks[row["trip_id"]] = row["block_id"]

        # Arrêts : un quai (stop) appartient à une station (parent_station)
        stops: dict[str, tuple[str, str]] = {}
        for row in _reader(zf, "stops.txt") or []:
            stops[row["stop_id"]] = (row.get("stop_name", ""), row.get("parent_station", ""))

        def station_of(stop_id: str) -> tuple[str, str]:
            """(clé de station, nom affiché) pour un quai."""
            name, parent = stops.get(stop_id, (stop_id, ""))
            if parent and parent in stops:
                return parent, stops[parent][0] or name
            return f"name:{name.strip().lower()}", name

        # Horaires (gros fichier : lecture en flux, métro seulement)
        raw: dict[str, list[tuple[int, str, int | None, int | None]]] = {}
        for row in _reader(zf, "stop_times.txt") or []:
            trip_id = row.get("trip_id")
            if trip_id not in trips_info:
                continue
            raw.setdefault(trip_id, []).append(
                (
                    int(row.get("stop_sequence") or 0),
                    row.get("stop_id", ""),
                    _seconds(row.get("arrival_time", "")),
                    _seconds(row.get("departure_time", "")),
                )
            )

        # Calendrier
        calendar: dict[str, tuple[tuple[bool, ...], date, date]] = {}
        for row in _reader(zf, "calendar.txt") or []:
            days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            calendar[row["service_id"]] = (
                tuple(row.get(d) == "1" for d in days),
                _date(row["start_date"]),
                _date(row["end_date"]),
            )
        exceptions: dict[str, dict[date, int]] = {}
        for row in _reader(zf, "calendar_dates.txt") or []:
            exceptions.setdefault(row["service_id"], {})[_date(row["date"])] = int(row.get("exception_type") or 0)

    # Séquence de stations de chaque course
    sequences: dict[str, list[tuple[str, int, int]]] = {}
    names: dict[str, str] = {}
    for trip_id, rows in raw.items():
        seq: list[tuple[str, int, int]] = []
        for _, stop_id, arr, dep in sorted(rows):
            if arr is None and dep is None:
                continue  # arrêt sans horaire : on interpole par-dessus
            key, name = station_of(stop_id)
            names.setdefault(key, name)
            arr = arr if arr is not None else dep
            dep = dep if dep is not None else arr
            if seq and seq[-1][0] == key:
                seq[-1] = (key, seq[-1][1], dep)  # même station deux fois de suite
            else:
                seq.append((key, arr, dep))
        if len(seq) >= 2:
            sequences[trip_id] = seq

    # Ordre des stations de chaque ligne : la course qui en dessert le plus
    # Une ligne peut avoir plusieurs route_id : une entrée par code
    line_meta: dict[str, tuple[str, str | None]] = {}
    for code, name, color in routes.values():
        known = line_meta.get(code)
        line_meta[code] = (name, color or (known[1] if known else None))

    lines: dict[str, MetroLine] = {}
    order: dict[str, dict[str, int]] = {}
    for code, (name, color) in sorted(line_meta.items()):
        candidates = [s for t, s in sequences.items() if trips_info[t][0] == code]
        if not candidates:
            continue
        longest = max(candidates, key=lambda s: (len({k for k, _, _ in s}), -s[0][1]))
        keys = [k for k, _, _ in longest]
        lines[code] = MetroLine(code, name, color, [names[k] for k in keys])
        order[code] = {k: i for i, k in enumerate(keys)}

    trips: list[MetroTrip] = []
    for trip_id, seq in sequences.items():
        code, service_id, headsign = trips_info[trip_id]
        index = order.get(code)
        if index is None:
            continue
        mapped = [(index[k], arr, dep) for k, arr, dep in seq if k in index]
        if len(mapped) < 2:
            continue
        direction = 1 if mapped[-1][0] > mapped[0][0] else -1
        trips.append(MetroTrip(code, service_id, headsign, direction, mapped, blocks.get(trip_id)))

    # Temps de parcours entre stations voisines (avant de prolonger les terminus)
    samples: dict[tuple[str, int, int], list[int]] = {}
    for trip in trips:
        for (i, _, dep), (j, arr, _) in zip(trip.stops, trip.stops[1:]):
            if abs(i - j) == 1 and arr > dep:
                samples.setdefault((trip.line, min(i, j), max(i, j)), []).append(arr - dep)
    segment_times = {key: float(sorted(v)[len(v) // 2]) for key, v in samples.items()}

    link_terminus_layovers(trips, {code: len(line.stations) for code, line in lines.items()})
    trips.sort(key=lambda t: t.stops[0][1])
    return MetroSchedule(timezone, lines, trips, calendar, exceptions, segment_times)


# ---------------------------------------------------------------------------
# STATIONNEMENT AU TERMINUS
# ---------------------------------------------------------------------------
MAX_LAYOVER = 30 * 60  # au-delà, ce n'est plus un retournement (fin de service…)


def link_terminus_layovers(trips: list[MetroTrip], station_counts: dict[str, int]) -> None:
    """Prolonge l'arrêt final d'une course jusqu'au départ de la suivante.

    Le GTFS décrit l'arrivée au terminus et le départ suivant comme deux
    courses distinctes : sans ce lien, la rame disparaîtrait pendant son
    stationnement à quai. On enchaîne par block_id (même véhicule) quand le
    GTFS le fournit, sinon dans l'ordre d'arrivée (premier arrivé, premier
    reparti), dans la limite de MAX_LAYOVER.
    """
    by_block: dict[tuple[str, str], list[MetroTrip]] = {}
    for trip in trips:
        if trip.block_id:
            by_block.setdefault((trip.service_id, trip.block_id), []).append(trip)

    linked: set[int] = set()
    for chain in by_block.values():
        chain.sort(key=lambda t: t.stops[0][1])
        for current, following in zip(chain, chain[1:]):
            _extend(current, following)
            linked.add(id(current))

    # Repli sans block_id : par ligne, terminus et jour de service
    arrivals: dict[tuple, list[MetroTrip]] = {}
    departures: dict[tuple, list[MetroTrip]] = {}
    for trip in trips:
        last_index = station_counts.get(trip.line, 0) - 1
        end, start = trip.stops[-1][0], trip.stops[0][0]
        if id(trip) not in linked and not trip.block_id and end in (0, last_index):
            arrivals.setdefault((trip.line, trip.service_id, end), []).append(trip)
        if not trip.block_id and start in (0, last_index):
            departures.setdefault((trip.line, trip.service_id, start), []).append(trip)

    for key, arriving in arrivals.items():
        leaving = sorted(departures.get(key, []), key=lambda t: t.stops[0][2])
        used = 0
        for trip in sorted(arriving, key=lambda t: t.stops[-1][1]):
            arr = trip.stops[-1][1]
            while used < len(leaving) and leaving[used].stops[0][2] < arr:
                used += 1
            if used < len(leaving):
                if _extend(trip, leaving[used]):
                    used += 1


def _extend(arriving: MetroTrip, leaving: MetroTrip) -> bool:
    idx, arr, dep = arriving.stops[-1]
    next_dep = leaving.stops[0][2]
    if leaving.stops[0][0] != idx or not 0 <= next_dep - arr <= MAX_LAYOVER:
        return False
    # La rame reste à quai jusqu'au départ suivant ; la course suivante
    # n'apparaît qu'à son départ (pas d'affichage en double)
    arriving.stops[-1] = (idx, arr, max(dep, next_dep - 1))
    leaving.stops[0] = (idx, next_dep, next_dep)
    return True


# ---------------------------------------------------------------------------
# RAMES EN CIRCULATION
# ---------------------------------------------------------------------------
def _service_day_start(day: date, tz: ZoneInfo) -> float:
    """Origine des horaires GTFS : « midi moins 12 h » (correct les jours de changement d'heure)."""
    return (datetime.combine(day, time(12), tzinfo=tz) - timedelta(hours=12)).timestamp()


def trains_window(
    schedule: MetroSchedule,
    now: datetime,
    before: int = 90,
    after: int = 600,
) -> tuple[int, list[dict], dict[str, int]]:
    """Courses utiles pour animer les rames de [now - before, now + after].

    Retourne (t0, rames, nombre de rames en circulation par ligne). Les heures
    de chaque rame sont en secondes relatives à t0 pour alléger l'attribut.
    """
    tz = ZoneInfo(schedule.timezone)
    t_now = now.timestamp()
    t0 = int(t_now)
    today = now.astimezone(tz).date()
    running = {code: 0 for code in schedule.lines}
    trains: list[dict] = []

    for day in (today - timedelta(days=1), today):
        services = schedule.services_on(day)
        if not services:
            continue
        base = _service_day_start(day, tz)
        for trip in schedule.trips:
            if trip.service_id not in services:
                continue
            first = base + trip.stops[0][1]
            last = base + trip.stops[-1][2]
            if last < t_now - before or first > t_now + after:
                continue
            if first <= t_now <= last:
                running[trip.line] = running.get(trip.line, 0) + 1

            # On ne garde que les arrêts qui encadrent la fenêtre
            start, end = 0, len(trip.stops) - 1
            for k, (_, _, dep) in enumerate(trip.stops):
                if base + dep <= t_now - before:
                    start = k
            for k, (_, arr, _) in enumerate(trip.stops):
                if base + arr >= t_now + after:
                    end = k
                    break
            trains.append(
                {
                    "l": trip.line,
                    "h": trip.headsign,
                    "d": trip.direction,
                    "s": [
                        [idx, int(base + arr - t0), int(base + dep - t0)]
                        for idx, arr, dep in trip.stops[start : end + 1]
                    ],
                }
            )

    return t0, trains, running


def train_position(stops: list[list[int]], t: float) -> float | None:
    """Position (index de station, fractionnaire) d'une rame à l'instant t (relatif à t0).

    Même calcul que la carte, utile pour les tests et un éventuel usage côté HA.
    """
    if not stops or t < stops[0][1] or t > stops[-1][2]:
        return None
    for k, (idx, arr, dep) in enumerate(stops):
        if arr <= t <= dep:
            return float(idx)
        if k + 1 < len(stops):
            nidx, narr, _ = stops[k + 1]
            if dep < t < narr:
                return idx + (t - dep) / (narr - dep) * (nidx - idx)
    return None


# ---------------------------------------------------------------------------
# ACRONYMES DE STATIONS (« Italie » -> « ITA »)
# ---------------------------------------------------------------------------
def _normalize_name(name: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]", "", text)


def station_code(name: str, known_codes: dict[str, str] | None = None) -> str:
    """Acronyme officiel STAR si connu (topologie), sinon 3 premières lettres."""
    if known_codes:
        code = known_codes.get(_normalize_name(name))
        if code:
            return code.upper()
    letters = re.sub(r"[^A-Za-z]", "", _normalize_name(name).upper())
    return letters[:3] or "?"


def codes_by_name(topology: dict[str, dict]) -> dict[str, str]:
    """Table nom normalisé -> acronyme, depuis la topologie des stations STAR."""
    return {
        _normalize_name(st.get("name") or ""): st["code"]
        for st in topology.values()
        if st.get("code") and st.get("name")
    }


# ---------------------------------------------------------------------------
# PROCHAINS PASSAGES PAR STATION
# ---------------------------------------------------------------------------
# Libellés de direction identiques à ceux des équipements (metro_api)
_DIRECTION_KEYWORDS = {
    "kennedy": "J.F. Kennedy",
    "poterie": "La Poterie",
    "viasilva": "Cesson - Viasilva",
    "gaite": "Saint-Jacques - Gaîté",
}


def direction_label(terminus: str) -> str:
    norm = _normalize_name(terminus)
    for keyword, label in _DIRECTION_KEYWORDS.items():
        if keyword in norm:
            return label
    return terminus


def station_served(
    schedule: MetroSchedule,
    now: datetime,
    line: str,
    station: str,
    before: int = 900,
    after: int = 300,
) -> bool | None:
    """Une rame est-elle prévue à cette station autour de maintenant ?

    Vrai si un passage théorique tombe entre now - after et now + before.
    Sert à distinguer une fermeture nocturne (normale) d'une fermeture en
    pleine exploitation. None si la station n'est pas sur la ligne.
    """
    meta = schedule.lines.get(line)
    if meta is None:
        return None
    target = _normalize_name(station)
    indexes = {i for i, name in enumerate(meta.stations) if _normalize_name(name) == target}
    if not indexes:
        return None
    tz = ZoneInfo(schedule.timezone)
    t_now = now.timestamp()
    today = now.astimezone(tz).date()
    for day in (today - timedelta(days=1), today):
        services = schedule.services_on(day)
        if not services:
            continue
        base = _service_day_start(day, tz)
        for trip in schedule.trips:
            if trip.line != line or trip.service_id not in services:
                continue
            if base + trip.stops[0][1] > t_now + before or base + trip.stops[-1][2] < t_now - after:
                continue
            for idx, arr, dep in trip.stops:
                if idx in indexes and t_now - after <= base + dep and base + arr <= t_now + before:
                    return True
    return False


def next_departures(
    schedule: MetroSchedule,
    now: datetime,
    count: int = 2,
    horizon: int = 3 * 3600,
) -> dict[str, list[dict]]:
    """Prochains départs de chaque station, par ligne et par sens.

    Retourne {nom de station normalisé: [{"ligne", "direction", "departs": [epoch, …]}]}.
    Un terminus n'affiche pas les arrivées (on ne peut pas y monter).
    """
    tz = ZoneInfo(schedule.timezone)
    t_now = now.timestamp()
    today = now.astimezone(tz).date()
    buckets: dict[tuple[str, int, int], list[float]] = {}

    for day in (today - timedelta(days=1), today):
        services = schedule.services_on(day)
        if not services:
            continue
        base = _service_day_start(day, tz)
        for trip in schedule.trips:
            if trip.service_id not in services:
                continue
            if base + trip.stops[0][2] > t_now + horizon or base + trip.stops[-1][1] < t_now:
                continue
            for idx, _, dep in trip.stops[:-1]:  # pas le dernier arrêt
                when = base + dep
                if t_now <= when <= t_now + horizon:
                    buckets.setdefault((trip.line, idx, trip.direction), []).append(when)

    result: dict[str, list[dict]] = {}
    for (code, idx, direction), times in sorted(buckets.items()):
        line = schedule.lines.get(code)
        if line is None:
            continue
        terminus = line.stations[-1] if direction == 1 else line.stations[0]
        result.setdefault(_normalize_name(line.stations[idx]), []).append(
            {
                "ligne": code,
                "direction": direction_label(terminus),
                "departs": [int(t) for t in sorted(times)[:count]],
            }
        )
    return result

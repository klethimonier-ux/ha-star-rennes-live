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
        timezone = _read_timezone(zf)
        routes = _read_routes(zf)
        if not routes:
            return MetroSchedule(timezone, {}, [])
        trips_info, blocks = _read_trips(zf, routes)
        stops = _read_stops(zf)
        raw = _read_stop_times(zf, trips_info)
        calendar, exceptions = _read_calendar(zf)

    sequences, names = _station_sequences(raw, stops)
    lines, order = _metro_lines(routes, sequences, trips_info, names)
    trips = _metro_trips(sequences, trips_info, order, blocks)
    # Temps de parcours entre stations voisines (avant de prolonger les terminus)
    segment_times = _segment_times(trips)
    link_terminus_layovers(trips, {code: len(line.stations) for code, line in lines.items()})
    trips.sort(key=lambda t: t.stops[0][1])
    return MetroSchedule(timezone, lines, trips, calendar, exceptions, segment_times)


def _read_timezone(zf: zipfile.ZipFile) -> str:
    """Fuseau du réseau (agency.txt)."""
    agency = _reader(zf, "agency.txt")
    first = next(agency, None) if agency is not None else None
    if first and first.get("agency_timezone"):
        return first["agency_timezone"]
    return DEFAULT_TIMEZONE


def _read_routes(zf: zipfile.ZipFile) -> dict[str, tuple[str, str, str | None]]:
    """Lignes de métro : route_id -> (code, nom court, couleur)."""
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
    return routes


def _read_trips(zf: zipfile.ZipFile, routes: dict) -> tuple[dict[str, tuple[str, str, str]], dict[str, str]]:
    """Courses de métro : trip_id -> (ligne, service, girouette), et trip_id -> block_id (véhicule)."""
    trips_info: dict[str, tuple[str, str, str]] = {}
    blocks: dict[str, str] = {}
    for row in _reader(zf, "trips.txt") or []:
        route = routes.get(row.get("route_id"))
        if route is None:
            continue
        trips_info[row["trip_id"]] = (route[0], row.get("service_id", ""), row.get("trip_headsign", ""))
        if row.get("block_id"):
            blocks[row["trip_id"]] = row["block_id"]
    return trips_info, blocks


def _read_stops(zf: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    """Arrêts : stop_id -> (nom, station parente) ; un quai appartient à une station."""
    return {row["stop_id"]: (row.get("stop_name", ""), row.get("parent_station", "")) for row in _reader(zf, "stops.txt") or []}


def _station_of(stops: dict[str, tuple[str, str]], stop_id: str) -> tuple[str, str]:
    """(clé de station, nom affiché) pour un quai."""
    name, parent = stops.get(stop_id, (stop_id, ""))
    if parent and parent in stops:
        return parent, stops[parent][0] or name
    return f"name:{name.strip().lower()}", name


def _read_stop_times(zf: zipfile.ZipFile, trips_info: dict) -> dict[str, list[tuple[int, str, int | None, int | None]]]:
    """Horaires (gros fichier : lecture en flux, métro seulement)."""
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
    return raw


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _read_calendar(zf: zipfile.ZipFile) -> tuple[dict, dict]:
    """Calendrier (calendar.txt) et exceptions (calendar_dates.txt)."""
    calendar: dict[str, tuple[tuple[bool, ...], date, date]] = {}
    for row in _reader(zf, "calendar.txt") or []:
        calendar[row["service_id"]] = (
            tuple(row.get(d) == "1" for d in _WEEKDAYS),
            _date(row["start_date"]),
            _date(row["end_date"]),
        )
    exceptions: dict[str, dict[date, int]] = {}
    for row in _reader(zf, "calendar_dates.txt") or []:
        exceptions.setdefault(row["service_id"], {})[_date(row["date"])] = int(row.get("exception_type") or 0)
    return calendar, exceptions


def _station_sequences(raw: dict, stops: dict) -> tuple[dict[str, list[tuple[str, int, int]]], dict[str, str]]:
    """Séquence de stations de chaque course, et nom affiché de chaque station."""
    sequences: dict[str, list[tuple[str, int, int]]] = {}
    names: dict[str, str] = {}
    for trip_id, rows in raw.items():
        seq = _trip_sequence(rows, stops, names)
        if len(seq) >= 2:
            sequences[trip_id] = seq
    return sequences, names


def _trip_sequence(rows: list, stops: dict, names: dict[str, str]) -> list[tuple[str, int, int]]:
    seq: list[tuple[str, int, int]] = []
    for _, stop_id, arr, dep in sorted(rows):
        if arr is None and dep is None:
            continue  # arrêt sans horaire : on interpole par-dessus
        key, name = _station_of(stops, stop_id)
        names.setdefault(key, name)
        arr = arr if arr is not None else dep
        dep = dep if dep is not None else arr
        if seq and seq[-1][0] == key:
            seq[-1] = (key, seq[-1][1], dep)  # même station deux fois de suite
        else:
            seq.append((key, arr, dep))
    return seq


def _metro_lines(routes: dict, sequences: dict, trips_info: dict, names: dict) -> tuple[dict[str, MetroLine], dict[str, dict[str, int]]]:
    """Ordre des stations de chaque ligne : la course qui en dessert le plus.

    Une ligne peut avoir plusieurs route_id : une entrée par code.
    """
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
    return lines, order


def _metro_trips(sequences: dict, trips_info: dict, order: dict, blocks: dict) -> list[MetroTrip]:
    """Courses rapportées aux index de station de leur ligne."""
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
    return trips


def _segment_times(trips: list[MetroTrip]) -> dict[tuple[str, int, int], float]:
    """Temps de parcours médian entre stations voisines."""
    samples: dict[tuple[str, int, int], list[int]] = {}
    for trip in trips:
        for (i, _, dep), (j, arr, _) in zip(trip.stops, trip.stops[1:]):
            if abs(i - j) == 1 and arr > dep:
                samples.setdefault((trip.line, min(i, j), max(i, j)), []).append(arr - dep)
    return {key: float(sorted(v)[len(v) // 2]) for key, v in samples.items()}


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
    linked = _link_by_block(trips)
    arrivals, departures = _terminus_candidates(trips, station_counts, linked)
    for key, arriving in arrivals.items():
        _link_first_in_first_out(arriving, departures.get(key, []))


def _link_by_block(trips: list[MetroTrip]) -> set[int]:
    """Enchaîne les courses d'un même véhicule (block_id) ; retourne les id() des courses prolongées."""
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
    return linked


def _terminus_candidates(trips: list[MetroTrip], station_counts: dict[str, int], linked: set[int]) -> tuple[dict, dict]:
    """Repli sans block_id : arrivées et départs aux terminus, par ligne, terminus et jour de service."""
    arrivals: dict[tuple, list[MetroTrip]] = {}
    departures: dict[tuple, list[MetroTrip]] = {}
    for trip in trips:
        if trip.block_id:
            continue
        ends = (0, station_counts.get(trip.line, 0) - 1)
        end, start = trip.stops[-1][0], trip.stops[0][0]
        if id(trip) not in linked and end in ends:
            arrivals.setdefault((trip.line, trip.service_id, end), []).append(trip)
        if start in ends:
            departures.setdefault((trip.line, trip.service_id, start), []).append(trip)
    return arrivals, departures


def _link_first_in_first_out(arriving: list[MetroTrip], departing: list[MetroTrip]) -> None:
    """Premier arrivé, premier reparti."""
    leaving = sorted(departing, key=lambda t: t.stops[0][2])
    used = 0
    for trip in sorted(arriving, key=lambda t: t.stops[-1][1]):
        arr = trip.stops[-1][1]
        while used < len(leaving) and leaving[used].stops[0][2] < arr:
            used += 1
        if used < len(leaving) and _extend(trip, leaving[used]):
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


def _service_trips(schedule: MetroSchedule, now: datetime) -> Iterator[tuple[float, MetroTrip]]:
    """(origine des horaires, course) des courses de la veille et du jour.

    Une course de la veille peut encore circuler après minuit.
    """
    tz = ZoneInfo(schedule.timezone)
    today = now.astimezone(tz).date()
    for day in (today - timedelta(days=1), today):
        services = schedule.services_on(day)
        if not services:
            continue
        base = _service_day_start(day, tz)
        for trip in schedule.trips:
            if trip.service_id in services:
                yield base, trip


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
    t_now = now.timestamp()
    t0 = int(t_now)
    running = {code: 0 for code in schedule.lines}
    trains: list[dict] = []
    for base, trip in _service_trips(schedule, now):
        first = base + trip.stops[0][1]
        last = base + trip.stops[-1][2]
        if last < t_now - before or first > t_now + after:
            continue
        if first <= t_now <= last:
            running[trip.line] = running.get(trip.line, 0) + 1
        start, end = _window_stops(trip, base, t_now - before, t_now + after)
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


def _window_stops(trip: MetroTrip, base: float, low: float, high: float) -> tuple[int, int]:
    """Indices des arrêts qui encadrent la fenêtre [low, high]."""
    start = max((k for k, (_, _, dep) in enumerate(trip.stops) if base + dep <= low), default=0)
    end = next((k for k, (_, arr, _) in enumerate(trip.stops) if base + arr >= high), len(trip.stops) - 1)
    return start, end


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
    low, high = now.timestamp() - after, now.timestamp() + before
    return any(
        _stops_at(trip, base, indexes, low, high)
        for base, trip in _service_trips(schedule, now)
        if trip.line == line
    )


def _stops_at(trip: MetroTrip, base: float, indexes: set[int], low: float, high: float) -> bool:
    """La course marque-t-elle l'arrêt à l'une de ces stations entre low et high ?"""
    if base + trip.stops[0][1] > high or base + trip.stops[-1][2] < low:
        return False
    return any(idx in indexes and low <= base + dep and base + arr <= high for idx, arr, dep in trip.stops)


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
    t_now = now.timestamp()
    buckets: dict[tuple[str, int, int], list[float]] = {}
    for base, trip in _service_trips(schedule, now):
        if base + trip.stops[0][2] > t_now + horizon or base + trip.stops[-1][1] < t_now:
            continue
        for idx, _, dep in trip.stops[:-1]:  # pas le dernier arrêt
            when = base + dep
            if t_now <= when <= t_now + horizon:
                buckets.setdefault((trip.line, idx, trip.direction), []).append(when)
    return _departures_by_station(schedule, buckets, count)


def _departures_by_station(schedule: MetroSchedule, buckets: dict, count: int) -> dict[str, list[dict]]:
    """{station normalisée: [{"ligne", "direction", "departs"}]} à partir des départs par (ligne, station, sens)."""
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

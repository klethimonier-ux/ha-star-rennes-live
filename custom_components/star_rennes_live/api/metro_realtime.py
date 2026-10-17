"""Temps réel métro : deux prochains passages à chaque quai.

Jeu STAR « tco-metro-circulation-deux-prochains-passages-tr » : une fiche par
(ligne, sens, arrêt) avec l'arrivée et le départ des deux prochaines rames.

On s'en sert pour :
- les prochains passages affichés pour une station ;
- reconstituer la position des rames sur le schéma : le métro ne se double
  pas, donc l'ordre des arrivées station après station suffit à suivre
  chaque rame.

Module sans dépendance Home Assistant (testable seul).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .gtfs_metro import MetroSchedule, _normalize_name, direction_label

DATASET = "tco-metro-circulation-deux-prochains-passages-tr"
LINE_CODES = {"1001": "A", "1002": "B"}
DEFAULT_SEGMENT = 100  # s entre deux stations, si le GTFS n'est pas chargé
MAX_AGE = 300  # au-delà de 5 min, l'extraction est jugée périmée
TERMINUS_DWELL = 60  # s affichés à quai au terminus d'arrivée, à défaut de mieux
ESTIMATED_DWELL = 20  # s d'arrêt supposés quand l'arrivée est estimée
MIN_RUN_RATIO = 0.75  # part minimale du temps de parcours (calibré) entre deux stations
MIN_RUN = 40  # s : aucune rame ne relie deux stations plus vite
MIN_HEADWAY = 60  # s : deux rames ne se suivent jamais à moins d'une minute à un même quai
TERMINUS_APPROACH = 150  # s : une arrivée au terminus plus lointaine n'est pas dessinée (position inconnue)
TURNBACK_MATCH = 120  # s : écart max entre arrivée estimée et arrivée publiée au terminus
CONSISTENCY = 20  # s : écart toléré entre les temps de parcours de deux rames successives


@dataclass
class Passage:
    line: str  # "A" / "B"
    station: str  # nom STAR
    destination: str
    times: list[tuple[float, float]]  # (arrivée, départ) en secondes epoch, triées
    ids: list[str | None] = field(default_factory=list)  # n° de course STAR, aligné sur times
    fills: list[str | None] = field(default_factory=list)  # remplissage STAR (« Vert Clair »…), aligné sur times


def _parse_local(value: str | None, tz: ZoneInfo) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.timestamp()


def _fill_value(value) -> str | None:
    """Remplissage publié par STAR ; « NA » ou vide = non mesuré."""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.upper() in ("NA", "N/A", "NULL") else text


def parse_passages(
    records: list[dict],
    timezone: str = "Europe/Paris",
    now: datetime | None = None,
) -> list[Passage]:
    """Passages publiés ; avec now, les fiches figées sont ignorées.

    Une station fermée (manifestation, incident) garde sa dernière fiche :
    ses horaires, vieux de plusieurs dizaines de minutes, fausseraient le
    suivi des rames et les prochains passages affichés.
    """
    tz = ZoneInfo(timezone)
    passages: list[Passage] = []
    for rec in records:
        line = LINE_CODES.get(str(rec.get("idligne")))
        if line is None or not rec.get("nomarret"):
            continue
        if now is not None and not _record_is_fresh(rec, now, tz):
            continue
        times = _record_times(rec, tz)
        if times:
            passages.append(
                Passage(
                    line,
                    rec["nomarret"],
                    rec.get("destination") or "",
                    [(a, d) for a, d, _, _ in times],
                    [t for _, _, t, _ in times],
                    [f for _, _, _, f in times],
                )
            )
    return passages


def _record_times(rec: dict, tz: ZoneInfo) -> list[tuple[float, float, str | None, str | None]]:
    """(arrivée, départ, n° de course, remplissage) des deux rames d'une fiche, dans l'ordre."""
    times = []
    for rank in ("first", "second"):
        arr = _parse_local(rec.get(f"arrivee{rank}train"), tz)
        dep = _parse_local(rec.get(f"depart{rank}train"), tz)
        if arr is None and dep is None:
            continue
        arr = arr if arr is not None else dep
        dep = dep if dep is not None else arr
        tid = rec.get(f"id{rank}train")
        fill = _fill_value(rec.get(f"couleurremplissage{rank}trainarrivee"))
        times.append((arr, max(arr, dep), str(tid) if tid not in (None, "") else None, fill))
    times.sort()
    return times


def _record_is_fresh(rec: dict, now: datetime, tz: ZoneInfo) -> bool:
    """Fiche extraite il y a moins de MAX_AGE (heureextraction « HH:MM:SS »)."""
    value = rec.get("heureextraction")
    if not value:
        return True  # pas d'horodatage : on garde
    try:
        h, m, s = (int(x) for x in str(value).split(":"))
    except ValueError:
        return True
    local = now.astimezone(tz)
    age = (local - local.replace(hour=h, minute=m, second=s, microsecond=0)).total_seconds()
    if age < -3600:  # extraction de la veille au passage de minuit
        age += 86400
    return -120 <= age <= MAX_AGE


def is_fresh(records: list[dict], now: datetime, timezone: str = "Europe/Paris") -> bool:
    """Au moins une fiche récente (une station fermée peut garder une fiche figée)."""
    tz = ZoneInfo(timezone)
    return any(rec.get("heureextraction") and _record_is_fresh(rec, now, tz) for rec in records)


def passages_by_station(passages: list[Passage], now: datetime, count: int = 2) -> dict[str, list[dict]]:
    """{station normalisée: [{"ligne", "direction", "departs": [epoch…], "affluence", "temps_reel": True}]}.

    « affluence » est alignée sur « departs » : niveau de remplissage publié
    par STAR pour chaque rame (None quand il n'est pas mesuré).
    """
    t_now = now.timestamp()
    result: dict[str, list[dict]] = {}
    for p in passages:
        fills = p.fills or [None] * len(p.times)
        kept = [(dep, fills[k] if k < len(fills) else None) for k, (_, dep) in enumerate(p.times) if dep >= t_now - 30][:count]
        result.setdefault(_normalize_name(p.station), []).append(
            {
                "ligne": p.line,
                "direction": direction_label(p.destination),
                "departs": [int(t) for t, _ in kept],
                "affluence": [f for _, f in kept],
                "temps_reel": True,
            }
        )
    return result


# ---------------------------------------------------------------------------
# RECONSTITUTION DES RAMES
# ---------------------------------------------------------------------------
def build_realtime_trains(
    passages: list[Passage],
    schedule: MetroSchedule,
    now: datetime,
) -> dict[str, list[dict]]:
    """Rames de chaque ligne, au format de trains_window (heures en epoch).

    Pour chaque sens, on parcourt les stations dans l'ordre de circulation.
    À une station, les arrivées antérieures au passage possible des rames
    déjà suivies sont des rames nouvelles (situées entre la station
    précédente et celle-ci) ; les suivantes sont les rames déjà suivies,
    dans le même ordre (pas de dépassement).
    """
    t_now = now.timestamp()
    result: dict[str, list[dict]] = {}
    for code, line in schedule.lines.items():
        by_station = _passages_by_direction(passages, code, line.stations)
        if not by_station:
            continue
        tracker = _DirectionTracker(by_station, schedule, code, line.stations, t_now)
        trains = tracker.run(1) + tracker.run(-1)
        _link_terminus_turnbacks(trains, line.stations, t_now)
        # Rames visibles maintenant ou dans les prochaines minutes uniquement
        result[code] = [
            t for t in trains if t["s"] and t["s"][-1][2] >= t_now - 30 and t["s"][0][1] <= t_now + 900
        ]
    return result


def _passages_by_direction(
    passages: list[Passage], code: str, stations: list[str]
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """(sens, index de station) -> horaires publiés (arrivée, départ) d'une ligne."""
    index = {_normalize_name(name): i for i, name in enumerate(stations)}
    forward_label = direction_label(stations[-1])
    by_station: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for p in passages:
        idx = index.get(_normalize_name(p.station)) if p.line == code else None
        if idx is None:
            continue
        direction = 1 if direction_label(p.destination) == forward_label else -1
        by_station[(direction, idx)] = list(p.times)
    return by_station


class _DirectionTracker:
    """Suivi des rames d'une ligne, station après station, dans un sens."""

    def __init__(self, by_station, schedule: MetroSchedule, code: str, stations: list[str], t_now: float) -> None:
        self.by_station = by_station
        self.schedule = schedule
        self.code = code
        self.stations = stations
        self.last = len(stations) - 1
        self.t_now = t_now

    def run(self, direction: int) -> list[dict]:
        order = list(range(0, self.last + 1)) if direction == 1 else list(range(self.last, -1, -1))
        seg_of = _calibrated_segments(self.by_station, self.schedule, self.code, direction, order)
        terminus = self.stations[self.last] if direction == 1 else self.stations[0]
        trains: list[dict] = []
        active: list[list[tuple[int, float, float]]] = []  # rames suivies, de l'avant vers l'arrière
        prev_idx: int | None = None
        for idx in order:
            times = self.by_station.get((direction, idx))
            if times is None:
                continue  # pas de fiche (ex. terminus d'arrivée)
            segment = seg_of(prev_idx, idx) if prev_idx is not None else DEFAULT_SEGMENT
            new_trains, following = self._new_trains(active, list(times), idx, prev_idx, segment)
            assigned = [stops + [(idx, arr, dep)] for stops, (arr, dep) in zip(active, following)]
            for stops in active[len(assigned):]:
                # Plus de créneau à cette station (2 passages publiés seulement,
                # ex. deux rames dans le tronçon) : arrivée estimée, fin de suivi
                arrive = stops[-1][2] + segment
                trains.append(_finish(stops + [(idx, arrive, arrive + ESTIMATED_DWELL)], terminus))
            active = new_trains + assigned
            prev_idx = idx
        end_idx = self.last if direction == 1 else 0
        trains += [self._end_of_line(stops, end_idx, prev_idx, seg_of, terminus) for stops in active]
        for train in trains:
            train["d"] = direction
        return trains

    def _new_trains(self, active, following, idx, prev_idx, segment):
        """Arrivées trop tôt pour une rame déjà suivie : rames nouvelles
        (situées entre la station précédente et celle-ci)."""
        min_run = max(MIN_RUN, segment * MIN_RUN_RATIO)
        new_trains = []
        while (
            following
            and not _matches(active, following, min_run)
            and not _too_close_to_front(active, following[0], segment)
        ):
            arr, dep = following.pop(0)
            stops = [(idx, arr, dep)]
            if prev_idx is not None:
                # Rame déjà dans le tronçon : départ estimé de la station
                # précédente, jamais dans le futur (rame retenue = visible)
                start = min(arr - segment, self.t_now)
                stops.insert(0, (prev_idx, start, start))
            new_trains.append(stops)
        return new_trains, following

    @staticmethod
    def _end_of_line(stops, end_idx, prev_idx, seg_of, terminus) -> dict:
        """Arrivée au terminus (pas de fiche pour un quai d'arrivée)."""
        if stops[-1][0] == end_idx or prev_idx is None:
            return _finish(stops, terminus)
        arrive = stops[-1][2] + seg_of(prev_idx, end_idx)
        train = _finish(stops + [(end_idx, arrive, arrive + TERMINUS_DWELL)], terminus)
        train["_arrival"] = end_idx  # arrivée au terminus estimée
        return train


def _calibrated_segments(by_station, schedule, code, direction, order):
    """Temps de parcours entre stations, calés sur le temps réel.

    Les temps du GTFS sont arrondis à la minute et comprennent l'arrêt en
    station : ils surestiment souvent le parcours réel, ce qui ferait prendre
    une rame suivie pour une nouvelle (doublons sur le schéma). Quand deux
    rames successives mettent le même temps entre deux stations publiées, ce
    temps est retenu ; ailleurs, le GTFS est corrigé du rapport médian observé.
    """
    published = [i for i in order if (direction, i) in by_station]
    direct: dict[tuple[int, int], float] = {}
    ratios: list[float] = []
    for prev, cur in zip(published, published[1:]):
        p, c = by_station[(direction, prev)], by_station[(direction, cur)]
        if len(p) < 2 or len(c) < 2:
            continue
        r1, r2 = c[0][0] - p[0][1], c[1][0] - p[1][1]
        if min(r1, r2) >= MIN_RUN and abs(r1 - r2) <= CONSISTENCY:
            run = (r1 + r2) / 2
            direct[(prev, cur)] = run
            ratios.append(run / _segment_time(schedule, code, prev, cur))
    ratios.sort()
    factor = min(1.5, max(0.3, ratios[len(ratios) // 2])) if ratios else 1.0

    def seg_of(a: int | None, b: int) -> float:
        if a is not None and (a, b) in direct:
            return direct[(a, b)]
        return _segment_time(schedule, code, a, b) * factor

    return seg_of


def _link_terminus_turnbacks(trains: list[dict], stations: list[str] | None = None, t_now: float | None = None) -> None:
    """Au terminus, la rame qui arrive est celle qui repart dans l'autre sens.

    Le quai de départ publie l'heure d'arrivée de la rame : elle remplace
    l'arrivée estimée de la rame entrante, qui disparaît à cet instant au
    profit de la rame au départ (sinon la même rame serait dessinée deux fois
    pendant le retournement).
    """
    departing = [t for t in trains if "_arrival" not in t and t["s"]]
    used: set[int] = set()
    for train in trains:
        end_idx = train.pop("_arrival", None)
        if end_idx is None:
            continue
        k = _turnback_partner(train, end_idx, departing, used)
        if k is not None:
            used.add(k)
            arrival = departing[k]["s"][0][1]
            train["s"][-1] = [end_idx, arrival, arrival]
    if stations and t_now is not None:
        _add_terminus_approaches(trains, departing, used, stations, t_now)


def _turnback_partner(train: dict, end_idx: int, departing: list[dict], used: set[int]) -> int | None:
    """Rame au départ du terminus la plus proche de l'arrivée estimée (indice dans departing)."""
    estimated = train["s"][-1][1]
    best = None
    for k, other in enumerate(departing):
        first = other["s"][0]
        if k in used or other["d"] == train["d"] or first[0] != end_idx:
            continue
        gap = abs(first[1] - estimated)
        if gap <= TURNBACK_MATCH and (best is None or gap < best[0]):
            best = (gap, k)
    return best[1] if best is not None else None


def _add_terminus_approaches(
    trains: list[dict], departing: list[dict], used: set[int], stations: list[str], t_now: float
) -> None:
    """Rame qui approche du terminus sans être suivie (elle a dépassé la dernière
    station publiée) : le quai de départ donne son heure d'arrivée, on la
    dessine sur la voie d'arrivée pendant son dernier tronçon."""
    last = len(stations) - 1
    for k, other in enumerate(departing):
        first = other["s"][0]
        start_idx = 0 if other["d"] == 1 else last
        arrival = first[1]
        if k in used or first[0] != start_idx or not t_now < arrival <= t_now + TERMINUS_APPROACH:
            continue
        neighbour = 1 if start_idx == 0 else last - 1
        leave = min(arrival - DEFAULT_SEGMENT, t_now)
        trains.append({"h": stations[start_idx], "d": -other["d"], "s": [[neighbour, leave, leave], [start_idx, arrival, arrival]]})


def _too_close_to_front(active: list, arrival: tuple, segment: float) -> bool:
    """Une rame « nouvelle » arriverait-elle trop près de la rame suivie en tête ?

    Si oui, c'est en fait la rame suivie, plus rapide que prévu sur un tronçon
    court (ex. Clemenceau → Jacques Cartier en 44 s) : pas de rame fantôme.
    """
    if not active:
        return False
    front_dep = active[0][-1][2]
    run = arrival[0] - front_dep
    expected = front_dep + segment
    return run >= MIN_RUN and arrival[0] > expected - MIN_HEADWAY


def _matches(active: list, following: list, min_run: float) -> bool:
    """Les arrivées publiées correspondent-elles aux rames suivies, dans l'ordre ?

    Chaque rame suivie doit mettre au moins min_run entre son départ de la
    station précédente et son arrivée ici. On vérifie les deux premières
    paires : une seule paire plausible ne suffit pas quand la seconde serait
    impossible (signe d'une rame nouvelle intercalée).
    """
    if not active:
        return False
    # Le n° publié (idfirsttrain) ne suit pas une rame d'une station à l'autre
    # (vérifié sur la ligne a le 17/10/2026) : seuls les horaires font foi.
    for stops, (arr, _dep) in list(zip(active, following))[:2]:
        if arr - stops[-1][2] < min_run:
            return False
    return True


def _finish(stops: list[tuple[int, float, float]], terminus: str) -> dict:
    return {"h": terminus, "s": [list(s) for s in stops]}


def _segment_time(schedule: MetroSchedule, code: str, a: int | None, b: int) -> float:
    if a is None:
        return DEFAULT_SEGMENT
    # Somme des tronçons entre les deux stations (une station sans fiche
    # peut séparer deux stations publiées)
    low, high = min(a, b), max(a, b)
    return sum(
        schedule.segment_times.get((code, i, i + 1)) or DEFAULT_SEGMENT
        for i in range(low, high)
    ) or DEFAULT_SEGMENT

"""API STAR Métro (lignes, stations, équipements)."""

from __future__ import annotations

import logging
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .client import StarClient, odsql_quote

_LOGGER = logging.getLogger(__name__)

LINE_A_ID = "1001"


def _line_code(line_id: Any) -> str:
    return "A" if str(line_id) == LINE_A_ID else "B"


def _combine_states(states_by_line: dict[str, Any]) -> Any:
    """État unique d'une station à partir de l'état de chaque ligne.

    Même état sur toutes les lignes -> cet état ; sinon « A : x, B : y ».
    """
    distinct = {state for state in states_by_line.values()}
    if len(distinct) <= 1:
        return next(iter(distinct), None)
    return ", ".join(f"{code} : {state}" for code, state in sorted(states_by_line.items()))


class StarMetroAPI:
    """Accès aux jeux de données métro de la STAR."""

    DATASET_LINES = "tco-metro-lignes-etat-tr"  # lignes, temps réel
    DATASET_STATIONS_RT = "tco-metro-stations-etat-tr"  # stations, temps réel (ligne A)
    DATASET_STATIONS_TOPO = "tco-metro-topologie-stations-td"  # stations A + B, statique
    DATASET_EQUIP = "tco-metro-equipements-etat-tr"  # équipements A + B, temps réel

    def __init__(self, client: StarClient) -> None:
        self._client = client

    # -------------------------------------------------------------------------
    # LIGNES (temps réel)
    # -------------------------------------------------------------------------
    async def async_get_lines(self) -> dict[str, dict[str, Any]]:
        records = await self._client.async_get_all(self.DATASET_LINES)

        lines: dict[str, dict[str, Any]] = {}
        for rec in records:
            nomcourt = rec.get("nomcourt")
            if not nomcourt:
                continue
            code = nomcourt.upper()  # "a" -> "A"
            lines[code] = {
                "id": rec.get("idligne"),
                "code": code,
                "state": rec.get("etat"),
                "pannedepuis": rec.get("pannedepuis"),
                "finpanneprevue": rec.get("finpanneprevue"),
                "lastupdate": rec.get("lastupdate"),
            }
        return lines

    # -------------------------------------------------------------------------
    # STATIONS : TOPOLOGIE (A + B, statique)
    # -------------------------------------------------------------------------
    async def async_get_stations_topology(self) -> dict[str, dict[str, Any]]:
        records = await self._client.async_get_all(self.DATASET_STATIONS_TOPO)

        stations: dict[str, dict[str, Any]] = {}
        for rec in records:
            raw_id = rec.get("id")
            if not raw_id:
                _LOGGER.debug("Station de topologie sans id ignorée : %s", rec)
                continue
            sid = str(raw_id)

            on_a = bool(rec.get("codetechniquestationlignea"))
            on_b = bool(rec.get("codetechniquestationligneb"))
            if on_a and on_b:
                line_code = "A/B"  # correspondance (Gares, Sainte-Anne)
            elif on_a:
                line_code = "A"
            elif on_b:
                line_code = "B"
            else:
                line_code = None

            stations[sid] = {
                "id": sid,
                "name": rec.get("nom") or "Station inconnue",
                "coords": rec.get("coordonnees"),
                "stop_id": rec.get("stop_id"),
                "code": rec.get("code"),  # acronyme STAR (ex. « PON »)
                "line_code": line_code,
            }
        return stations

    # -------------------------------------------------------------------------
    # STATIONS : TEMPS RÉEL (lignes A + B)
    # Les stations de correspondance (Gares, Sainte-Anne) ont une fiche par
    # ligne avec le même idstation : on regroupe les états par ligne.
    # -------------------------------------------------------------------------
    async def async_get_stations_realtime(self) -> dict[str, dict[str, Any]]:
        records = await self._client.async_get_all(self.DATASET_STATIONS_RT)

        stations_rt: dict[str, dict[str, Any]] = {}
        for rec in records:
            raw_id = rec.get("idstation")
            if raw_id is None:
                continue
            sid = str(raw_id)
            station = stations_rt.setdefault(
                sid,
                {
                    "id": sid,
                    "name": rec.get("nom"),
                    "coords": rec.get("coordonnees"),
                    "states_by_line": {},
                    "lastupdate": None,
                },
            )
            station["states_by_line"][_line_code(rec.get("idligne"))] = rec.get("etat")

            lastupdate = rec.get("lastupdate")
            if lastupdate and (station["lastupdate"] is None or lastupdate > station["lastupdate"]):
                station["lastupdate"] = lastupdate

        for station in stations_rt.values():
            station["state"] = _combine_states(station["states_by_line"])

        return stations_rt

    # -------------------------------------------------------------------------
    # ÉQUIPEMENTS : TEMPS RÉEL (A + B), requête paginée
    # -------------------------------------------------------------------------
    async def async_get_equipments(
        self, station_ids: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Équipements, limités aux stations données si station_ids est fourni.

        Sans filtre, le jeu complet fait ~280 lignes (3 pages) ; filtré sur
        quelques stations, il tient en une seule page.
        """
        where = None
        if station_ids:
            where = " OR ".join(
                f"idstation={odsql_quote(sid)}" for sid in sorted(set(station_ids))
            )

        records = await self._client.async_get_all(self.DATASET_EQUIP, where=where)

        equipments: dict[str, dict[str, Any]] = {}
        for rec in records:
            raw_id = rec.get("idequipement")
            if raw_id is None:
                continue
            eid = str(raw_id)

            raw_state = rec.get("etat")
            if isinstance(raw_state, list):
                state = ", ".join(str(s) for s in raw_state) or "Inconnu"
            else:
                state = raw_state or "Inconnu"

            equipments[eid] = {
                "id": eid,
                "name": rec.get("nom"),
                "station_id": str(rec.get("idstation")),
                "station_name": rec.get("nomstation"),
                "coords": rec.get("coordonnees"),
                "state": state,
                "type": rec.get("type"),
                "type_short": rec.get("typecourt"),
                "line_id": rec.get("idligne"),
                "line_code": _line_code(rec.get("idligne")),
                "lastupdate": rec.get("lastupdate"),
            }
        return equipments


# ---------------------------------------------------------------------------
# ÉTATS : ligne normale, station ouverte, équipement en service
# ---------------------------------------------------------------------------
LINE_OK_STATE = "ok"  # valeur observée : "OK"


def line_is_disrupted(state: Any) -> bool:
    """Ligne perturbée : tout état connu autre que « OK »."""
    return state is not None and str(state).strip().lower() != LINE_OK_STATE


def station_state_is_open(state: Any) -> bool:
    """Station ouverte : valeur observée « Ouverte »."""
    return state is None or str(state).strip().lower().startswith("ouvert")


def equipment_status(state: Any) -> str:
    """'ok', 'hs' ou 'inconnu' à partir de l'état STAR d'un équipement."""
    text = str(state or "").lower()
    if "fonctionnement" in text:
        return "ok"
    if not text or text == "inconnu":
        return "inconnu"
    return "hs"


def closed_lines(states_by_line: dict[str, Any]) -> list[str]:
    """Lignes sur lesquelles la station n'est pas ouverte."""
    return sorted(
        code for code, state in states_by_line.items() if not station_state_is_open(state)
    )


# ---------------------------------------------------------------------------
# DIRECTIONS : regroupement des équipements par sens de circulation
# ---------------------------------------------------------------------------
# Terminus de chaque ligne, avec le mot-clé qui apparaît dans les noms
# d'équipements STAR (« Quai vers Kennedy <-> Salle des billets »…).
LINE_DIRECTIONS: dict[str, list[tuple[str, str]]] = {
    "A": [("kennedy", "J.F. Kennedy"), ("poterie", "La Poterie")],
    "B": [("viasilva", "Cesson - Viasilva"), ("gaite", "Saint-Jacques - Gaîté")],
}

# typecourt STAR -> catégorie affichée. Les ascenseurs de parc relais
# (PR_ASC) ne desservent pas un quai : ils restent hors directions.
EQUIPMENT_CATEGORIES = {"ASC": "ascenseurs", "ESC": "escaliers", "DAT": "distributeurs"}


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def equipment_directions(equipment: dict[str, Any]) -> list[tuple[str, str]]:
    """Directions (ligne, terminus) desservies par un équipement.

    Le nom STAR indique la direction (« Quai vers Kennedy… »). Un équipement
    sans direction dans son nom (« Salle des billets -> Extérieur ») sert
    les deux sens de sa ligne.
    """
    name = _normalize(equipment.get("name") or "")
    found = [
        (code, label)
        for code, directions in LINE_DIRECTIONS.items()
        for keyword, label in directions
        if keyword in name
    ]
    if found:
        return found
    code = equipment.get("line_code")
    return [(code, label) for _, label in LINE_DIRECTIONS.get(code, [])]


def summarize_directions(
    station_line_code: str | None, equipments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Résumé par direction : pour chaque catégorie, total / HS / noms HS."""
    codes = [c for c in (station_line_code or "").split("/") if c in LINE_DIRECTIONS]
    for equipment in equipments:
        code = equipment.get("line_code")
        if code in LINE_DIRECTIONS and code not in codes:
            codes.append(code)

    summary: dict[str, dict[str, Any]] = {}
    for code in sorted(codes):
        for _, label in LINE_DIRECTIONS[code]:
            summary[label] = {
                "direction": label,
                "ligne": code,
                **{cat: {"total": 0, "hs": 0, "hs_noms": []} for cat in EQUIPMENT_CATEGORIES.values()},
            }

    for equipment in equipments:
        category = EQUIPMENT_CATEGORIES.get(equipment.get("type_short"))
        if category is None:
            continue
        status = equipment_status(equipment.get("state"))
        for _, label in equipment_directions(equipment):
            if label not in summary:
                continue
            bucket = summary[label][category]
            bucket["total"] += 1
            if status == "hs":
                bucket["hs"] += 1
                bucket["hs_noms"].append(equipment.get("name"))

    return list(summary.values())


# ---------------------------------------------------------------------------
# PERTURBATIONS (réseau métro complet)
# ---------------------------------------------------------------------------
def _lines_label(codes: list[str]) -> str:
    lowered = [c.lower() for c in codes]
    if len(lowered) == 1:
        return f"Ligne {lowered[0]}"
    return "Lignes " + " et ".join(lowered)


# Heures de service du métro (fiches horaires STAR 2026-2027) : premiers
# départs vers 5 h 10, derniers passages vers 0 h 50, ou vers 1 h 50 les nuits
# du jeudi, vendredi et samedi. Marge de quelques minutes de part et d'autre.
SERVICE_START = (5, 0)
SERVICE_END = (0, 55)
SERVICE_END_LATE = (1, 55)  # vendredi, samedi et dimanche matin


def within_service_hours(local: datetime) -> bool:
    """Le métro circule-t-il à cette heure locale (sans GTFS) ?"""
    minutes = local.hour * 60 + local.minute
    start = SERVICE_START[0] * 60 + SERVICE_START[1]
    late = local.weekday() in (4, 5, 6)
    end_h, end_m = SERVICE_END_LATE if late else SERVICE_END
    return not (end_h * 60 + end_m <= minutes < start)


def drop_off_service_closures(
    perturbations: list[dict[str, Any]],
    is_served: Callable[[str, str], bool],
) -> list[dict[str, Any]]:
    """Retire les fermetures de station hors exploitation (nuit, avant la première rame).

    is_served(ligne, nom de station) indique si des rames sont prévues à la
    station en ce moment. Une station reste signalée si au moins une de ses
    lignes fermées devrait la desservir.
    """
    kept = []
    for item in perturbations:
        if item.get("type") == "station":
            lines = [code for code in item.get("lignes", []) if is_served(code, item.get("station") or "")]
            if not lines:
                continue
            if lines != item.get("lignes"):
                item = {**item, "lignes": lines, "detail": f"{_lines_label(lines)} · les rames ne s'y arrêtent pas"}
        kept.append(item)
    return kept


def build_perturbations(
    lines: dict[str, dict[str, Any]],
    stations_rt: dict[str, dict[str, Any]],
    format_time: Callable[[Any], str | None] = lambda value: value,
) -> list[dict[str, Any]]:
    """Liste des perturbations prêtes à afficher (lignes puis stations)."""
    items: list[dict[str, Any]] = []

    for code, line in sorted(lines.items()):
        state = line.get("state")
        if not line_is_disrupted(state):
            continue
        state_text = str(state)
        detail = None
        end = format_time(line.get("finpanneprevue"))
        start = format_time(line.get("pannedepuis"))
        if end:
            detail = f"Reprise estimée vers {end}"
        elif start:
            detail = f"Depuis {start}"
        items.append(
            {
                "type": "ligne",
                "niveau": "critique",
                "ligne": code,
                "lignes": [code],
                "titre": f"Ligne {code.lower()} · {state_text[:1].lower()}{state_text[1:]}",
                "detail": detail,
                "etat": state,
            }
        )

    for sid, station in sorted(stations_rt.items(), key=lambda kv: kv[1].get("name") or ""):
        closed = closed_lines(station.get("states_by_line", {}))
        if not closed:
            continue
        items.append(
            {
                "type": "station",
                "niveau": "alerte",
                "station_id": sid,
                "station": station.get("name"),
                "lignes": closed,
                "titre": f"Station {station.get('name')} fermée",
                "detail": f"{_lines_label(closed)} · les rames ne s'y arrêtent pas",
            }
        )

    return items


def build_metro_structure(
    lines: dict[str, dict[str, Any]],
    topology: dict[str, dict[str, Any]],
    stations_rt: dict[str, dict[str, Any]],
    equipments: dict[str, dict[str, Any]],
    format_time: Callable[[Any], str | None] = lambda value: value,
) -> dict[str, Any]:
    """Fusionne topologie, temps réel et équipements.

    Construit des dictionnaires neufs à chaque appel : la topologie mise en
    cache par le coordinator n'est jamais modifiée.
    """
    station_to_equip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for equipment in equipments.values():
        station_to_equip[equipment["station_id"]].append(equipment)

    stations: dict[str, dict[str, Any]] = {}
    for sid, topo in topology.items():
        rt = stations_rt.get(sid)
        states_by_line = rt["states_by_line"] if rt else {}
        station_equipments = station_to_equip.get(sid, [])
        closed = closed_lines(states_by_line)
        stations[sid] = {
            **topo,
            "state": rt["state"] if rt else "N/A",
            "states_by_line": states_by_line,
            "closed_lines": closed,
            "closed": bool(states_by_line) and len(closed) == len(states_by_line),
            "lastupdate": rt["lastupdate"] if rt else None,
            "realtime_available": rt is not None,
            "equipments": station_equipments,
            "directions": summarize_directions(topo.get("line_code"), station_equipments),
        }

    return {
        "lines": lines,
        "stations": stations,
        "equipments": equipments,
        "perturbations": build_perturbations(lines, stations_rt, format_time),
    }

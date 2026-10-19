"""Alertes trafic STAR (bus et métro).

Jeu « tco-busmetro-trafic-alertes-tr » : une fiche par (perturbation, ligne),
avec un titre, une description, un niveau (« Bloquante »…) et une période de
validité. Une même perturbation (ex. manifestation, toutes les lignes) est
publiée une fois par ligne : on les regroupe.

Module sans dépendance Home Assistant (testable seul).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from .client import StarClient, odsql_quote

DATASET = "tco-busmetro-trafic-alertes-tr"
BLOCKING_LEVELS = ("bloquante",)

# « Bloquante » couvre aussi bien une ligne arrêtée qu'un arrêt reporté : la
# suspension se reconnaît au texte de l'alerte.
SUSPENSION_PATTERNS = (
    r"arret (total )?de (la )?circulation",
    r"circulation (est |sont )?(interrompue|suspendue|arretee)",
    r"ne (circule|circulent|roule|roulent) (plus|pas)",
    r"\b(suspendue?s?|suspension|interrompue?s?|interruption)\b",
    r"aucun (bus|metro|passage)",
    r"ligne (est )?(fermee|arretee)",
)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _levels(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _normalize(text: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def is_suspension(title: Any, description: Any) -> bool:
    """La ligne ne circule plus (et non un simple report d'arrêt ou une déviation)."""
    text = _normalize(f"{title} {description}")
    return any(re.search(pattern, text) for pattern in SUSPENSION_PATTERNS)


# En-tête de section d'une alerte : « Ligne C4 vers Grand Quartier »
_SECTION_HEADER = re.compile(r"^\s*ligne\s+\S+\s+vers\s+(.+?)\s*[.:]?\s*$", re.IGNORECASE)
# Arrêts touchés : « L'arrêt Les Préales est reporté… », « Les arrêts A et B ne sont pas desservis »
_AFFECTED_STOPS = re.compile(
    r"\barr[eê]ts?\s+(.+?)\s+(?:est|sont|sera|seront|n['’]est|ne\s+sont|ne\s+sera|ne\s+seront|non)\b",
    re.IGNORECASE,
)


def split_sections(description: Any) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(lignes d'introduction, [(direction, lignes de la section)]).

    Les alertes STAR décrivent souvent chaque sens à part :
    « Ligne C4 vers Grand Quartier » puis les arrêts reportés de ce sens.
    """
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for raw in str(description or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        header = _SECTION_HEADER.match(line) if line else None
        if header:
            sections.append((header.group(1).strip(), []))
        elif line:
            (sections[-1][1] if sections else intro).append(line)
    return intro, sections


def mentioned_directions(description: Any) -> list[str]:
    """Directions citées (« Ligne 11 vers Roazhon Park/ZI Ouest » -> ["Roazhon Park/ZI Ouest"])."""
    found: list[str] = []
    for direction, _ in split_sections(description)[1]:
        if direction not in found:
            found.append(direction)
    return found


def _same_direction(direction: str, destination: str) -> bool:
    """« Roazhon Park/ZI Ouest » vaut pour « Roazhon Park » comme pour « ZI Ouest »."""
    dest = _normalize(destination)
    parts = (part.strip() for part in re.split(r"[/|]", _normalize(direction)))
    return any(part and (part in dest or dest in part) for part in parts)


def concerns_direction(alert: dict[str, Any], destination: str | None) -> bool:
    """L'alerte vaut-elle pour ce sens ? Oui si aucune direction n'est citée."""
    directions = alert.get("directions") or []
    if not directions or not destination:
        return True
    return any(_same_direction(direction, destination) for direction in directions)


def affected_stops(text: Any) -> list[str]:
    """Arrêts reportés ou non desservis cités dans un texte d'alerte."""
    stops: list[str] = []
    for match in _AFFECTED_STOPS.finditer(str(text or "")):
        for name in re.split(r",\s*|\s+et\s+", match.group(1)):
            name = name.strip()
            if name and name not in stops:
                stops.append(name)
    return stops


def relevant_text(alert: dict[str, Any], stop: str | None, destination: str | None) -> str | None:
    """Partie de l'alerte qui concerne cet arrêt dans ce sens, ou None.

    - seules les sections du sens suivi comptent (« Ligne C4 vers … ») ;
    - si elles citent des arrêts touchés, l'alerte ne vaut que si l'arrêt
      suivi est cité (arrêt reporté, ou arrêt de report) ;
    - une alerte sans arrêt cité (déviation générale…) vaut pour tous.
    """
    sections = alert.get("sections") or []
    texts = [s["texte"] for s in sections if not destination or _same_direction(s["direction"], destination)]
    if sections and not texts:
        return None  # sections pour l'autre sens seulement
    text = " · ".join(t for t in texts if t) if sections else alert.get("description") or ""
    if stop and affected_stops(text) and _normalize(stop) not in _normalize(text):
        return None  # arrêts touchés, mais pas celui-ci
    return text


def _clean(text: Any) -> str:
    """Description sur une ligne : lignes vides retirées, séparées par « · »."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    return " · ".join(line for line in lines if line)


async def async_get_alerts(client: StarClient, line_names: list[str]) -> list[dict[str, Any]]:
    """Fiches d'alerte des lignes données (noms courts : « C4 », « 11 »…)."""
    names = sorted({str(n) for n in line_names if n})
    if not names:
        return []
    where = " OR ".join(f"nomcourtligne={odsql_quote(n)}" for n in names)
    return await client.async_get_all(DATASET, where=where)


def _is_active(rec: dict[str, Any], now: datetime) -> bool:
    """La fiche est-elle dans sa période de validité ?"""
    start = _parse_dt(rec.get("debutvalidite"))
    end = _parse_dt(rec.get("finvalidite"))
    return not (start and now < start) and not (end and now > end)


def _new_group(rec: dict[str, Any], levels: list[str]) -> dict[str, Any]:
    """Entrée regroupée d'une perturbation (sans ses lignes)."""
    return {
        "id": str(rec.get("idperturbation") or ""),
        "titre": rec.get("titre") or "Perturbation",
        "description": _clean(rec.get("description")),
        "niveau": ", ".join(levels),
        "bloquante": any(level.lower() in BLOCKING_LEVELS for level in levels),
        "type": "suspension" if is_suspension(rec.get("titre"), rec.get("description")) else "info",
        "directions": mentioned_directions(rec.get("description")),
        **_structured(rec.get("description")),
        "lignes": [],
        "debut": rec.get("debutvalidite"),
        "fin": rec.get("finvalidite"),
        "url": rec.get("url"),
    }


def active_alerts(records: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Alertes en cours de validité, regroupées (une entrée par perturbation).

    Retourne [{"id", "titre", "description", "niveau", "bloquante", "type",
    "directions", "lignes", "debut", "fin", "url"}], suspensions puis
    bloquantes d'abord.
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        if not _is_active(rec, now):
            continue
        levels = _levels(rec.get("niveau"))
        key = (rec.get("titre"), rec.get("description"), tuple(levels), rec.get("debutvalidite"), rec.get("finvalidite"))
        group = groups.get(key)
        if group is None:
            group = groups[key] = _new_group(rec, levels)
        line = rec.get("nomcourtligne")
        if line and line not in group["lignes"]:
            group["lignes"].append(line)
    for group in groups.values():
        group["lignes"].sort(key=_line_sort_key)
    return sorted(groups.values(), key=lambda g: (g["type"] != "suspension", not g["bloquante"], g["titre"]))


def _structured(description: Any) -> dict[str, Any]:
    """Introduction (période…) et sections par sens d'une description d'alerte."""
    intro, sections = split_sections(description)
    return {
        "intro": " · ".join(intro),
        "sections": [{"direction": d, "texte": " · ".join(lines)} for d, lines in sections],
    }


def alerts_for_line(alerts: list[dict[str, Any]], line_name: str) -> list[dict[str, Any]]:
    """Alertes qui concernent une ligne (nom court)."""
    return [a for a in alerts if line_name in a["lignes"]]


def _line_sort_key(name: str) -> tuple:
    """Chronobus (C1…C7) d'abord, puis lignes numérotées dans l'ordre."""
    match = re.match(r"([A-Za-z]*)(\d*)", name or "")
    prefix, number = (match.group(1), match.group(2)) if match else (name, "")
    return (0 if prefix else 1, prefix.upper(), int(number) if number else 0, name)

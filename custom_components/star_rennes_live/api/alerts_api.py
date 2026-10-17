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


def mentioned_directions(description: Any) -> list[str]:
    """Directions citées (« Ligne 11 vers Roazhon Park/ZI Ouest » -> ["Roazhon Park/ZI Ouest"])."""
    found = []
    for line in str(description or "").splitlines():
        for match in re.finditer(r"\bvers\s+([^.,;:·\n]+)", line, flags=re.IGNORECASE):
            value = match.group(1).strip()
            if value and value not in found:
                found.append(value)
    return found


def concerns_direction(alert: dict[str, Any], destination: str | None) -> bool:
    """L'alerte vaut-elle pour ce sens ? Oui si aucune direction n'est citée."""
    directions = alert.get("directions") or []
    if not directions or not destination:
        return True
    dest = _normalize(destination)
    for direction in directions:
        for part in re.split(r"[/|]", _normalize(direction)):
            part = part.strip()
            if part and (part in dest or dest in part):
                return True
    return False


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


def active_alerts(records: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Alertes en cours de validité, regroupées (une entrée par perturbation).

    Retourne [{"id", "titre", "description", "niveau", "bloquante", "lignes",
    "debut", "fin", "url"}], les bloquantes d'abord.
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        start = _parse_dt(rec.get("debutvalidite"))
        end = _parse_dt(rec.get("finvalidite"))
        if start and now < start:
            continue
        if end and now > end:
            continue
        levels = _levels(rec.get("niveau"))
        key = (rec.get("titre"), rec.get("description"), tuple(levels), rec.get("debutvalidite"), rec.get("finvalidite"))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "id": str(rec.get("idperturbation") or ""),
                "titre": rec.get("titre") or "Perturbation",
                "description": _clean(rec.get("description")),
                "niveau": ", ".join(levels),
                "bloquante": any(level.lower() in BLOCKING_LEVELS for level in levels),
                "type": "suspension" if is_suspension(rec.get("titre"), rec.get("description")) else "info",
                "directions": mentioned_directions(rec.get("description")),
                "lignes": [],
                "debut": rec.get("debutvalidite"),
                "fin": rec.get("finvalidite"),
                "url": rec.get("url"),
            }
        line = rec.get("nomcourtligne")
        if line and line not in group["lignes"]:
            group["lignes"].append(line)
    for group in groups.values():
        group["lignes"].sort(key=_line_sort_key)
    return sorted(groups.values(), key=lambda g: (g["type"] != "suspension", not g["bloquante"], g["titre"]))


def alerts_for_line(alerts: list[dict[str, Any]], line_name: str) -> list[dict[str, Any]]:
    """Alertes qui concernent une ligne (nom court)."""
    return [a for a in alerts if line_name in a["lignes"]]


def _line_sort_key(name: str) -> tuple:
    """Chronobus (C1…C7) d'abord, puis lignes numérotées dans l'ordre."""
    match = re.match(r"([A-Za-z]*)(\d*)", name or "")
    prefix, number = (match.group(1), match.group(2)) if match else (name, "")
    return (0 if prefix else 1, prefix.upper(), int(number) if number else 0, name)

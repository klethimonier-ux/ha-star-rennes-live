"""API STAR Vélo (état temps réel des stations)."""

from __future__ import annotations

from typing import Any

from .client import StarClient


def _first_state(raw: Any) -> str:
    """Normalise le champ « etat » (liste ou chaîne selon les jeux de données)."""
    if isinstance(raw, list):
        return str(raw[0]) if raw else "Inconnu"
    if raw:
        return str(raw)
    return "Inconnu"


class StarBikeAPI:
    """Accès aux stations Le vélo STAR."""

    DATASET_STATUS = "vls-stations-etat-tr"

    def __init__(self, client: StarClient) -> None:
        self._client = client

    async def async_get_stations(self) -> dict[str, dict[str, Any]]:
        """Retourne l'état temps réel de toutes les stations, indexé par id."""
        records = await self._client.async_get_all(self.DATASET_STATUS)

        stations: dict[str, dict[str, Any]] = {}
        for rec in records:
            raw_id = rec.get("idstation")
            if raw_id is None:
                continue
            sid = str(raw_id)
            stations[sid] = {
                "id": sid,
                "name": rec.get("nom"),
                "status": _first_state(rec.get("etat")),
                "bikes": rec.get("nombrevelosdisponibles"),
                "docks": rec.get("nombreemplacementsdisponibles"),
                "capacity": rec.get("nombreemplacementsactuels"),
                "coords": rec.get("coordonnees"),
                "lastupdate": rec.get("lastupdate"),
            }
        return stations

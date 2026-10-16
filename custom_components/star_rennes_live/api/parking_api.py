"""API STAR Parcs relais (état temps réel)."""

from __future__ import annotations

from typing import Any

from .client import StarClient


class StarParkingAPI:
    """Accès aux parcs relais STAR."""

    DATASET_PARKINGS = "tco-parcsrelais-star-etat-tr"

    def __init__(self, client: StarClient) -> None:
        self._client = client

    async def async_get_all(self) -> dict[str, dict[str, Any]]:
        """Retourne les parcs relais normalisés, indexés par id."""
        records = await self._client.async_get_all(self.DATASET_PARKINGS)

        parkings: dict[str, dict[str, Any]] = {}
        for rec in records:
            raw_id = rec.get("idparc")
            if not raw_id:
                continue
            pid = str(raw_id)

            parkings[pid] = {
                "id": pid,
                "name": rec.get("nom"),
                "coords": rec.get("coordonnees"),
                "lastupdate": rec.get("lastupdate"),
                # État principal : l'état d'ouverture
                "state": rec.get("etatouverture") or "INCONNU",
                # Autres états
                "etat_ouverture": rec.get("etatouverture"),
                "etat_remplissage": rec.get("etatremplissage"),
                # Capacités
                "capacite": rec.get("capaciteparking"),
                "places_disponibles": rec.get("jrdinfosoliste"),
                # PMR
                "capacite_pmr": rec.get("capacitepmr"),
                "places_pmr_disponibles": rec.get("jrdinfopmr"),
                # VE
                "capacite_ve": rec.get("capaciteve"),
                "places_ve_disponibles": rec.get("jrdinfoelectrique"),
                # Covoiturage
                "capacite_covoiturage": rec.get("capacitecovoiturage"),
                "places_covoiturage_disponibles": rec.get("jrdinfocovoiturage"),
                # Ascenseurs
                "nb_ascenseurs": rec.get("nbascenseur"),
                "nb_ascenseurs_dispo": rec.get("nbascenseurdispo"),
            }
        return parkings

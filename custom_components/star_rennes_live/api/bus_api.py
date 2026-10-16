"""API STAR Bus (topologie + passages temps réel)."""

from __future__ import annotations

from typing import Any

from .client import StarClient, odsql_quote


class StarBusAPI:
    """Accès aux jeux de données bus de la STAR."""

    DATASET_LINES = "tco-bus-topologie-lignes-td"
    DATASET_DESSERTES = "tco-bus-topologie-dessertes-td"
    DATASET_PARCOURS = "tco-bus-topologie-parcours-td"
    DATASET_PICTOS = "tco-bus-lignes-pictogrammes-dm"
    DATASET_PASSAGES = "tco-bus-circulation-passages-tr"

    def __init__(self, client: StarClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # LIGNES : mapping nomcourt -> id
    # ------------------------------------------------------------------
    async def async_get_lines(self) -> dict[str, str]:
        records = await self._client.async_get_all(
            self.DATASET_LINES, select="id,nomcourt", order_by="id ASC"
        )

        line_map: dict[str, str] = {}
        for rec in records:
            nomcourt = rec.get("nomcourt")
            line_id = rec.get("id")
            if nomcourt and line_id:
                line_map[nomcourt] = line_id
        return line_map

    # ------------------------------------------------------------------
    # ARRÊTS d'une ligne : mapping nomarret -> idarret
    # ------------------------------------------------------------------
    async def async_get_stops(self, line_id: str) -> dict[str, str]:
        records = await self._client.async_get_all(
            self.DATASET_DESSERTES,
            select="idarret,nomarret",
            where=f"idligne={odsql_quote(line_id)}",
            order_by="nomarret ASC",
        )

        stop_map: dict[str, str] = {}
        for rec in records:
            nomarret = rec.get("nomarret")
            idarret = rec.get("idarret")
            if nomarret and idarret and nomarret not in stop_map:
                stop_map[nomarret] = idarret
        return stop_map

    # ------------------------------------------------------------------
    # DESTINATIONS d'une ligne : mapping nomarretarrivee -> sens
    # ------------------------------------------------------------------
    async def async_get_destinations(self, line_id: str) -> dict[str, int]:
        records = await self._client.async_get_all(
            self.DATASET_PARCOURS,
            select="idarretarrivee,nomarretarrivee,sens",
            where=f"idligne={odsql_quote(line_id)}",
            order_by="nomarretarrivee ASC",
        )

        dest_map: dict[str, int] = {}
        for rec in records:
            name = rec.get("nomarretarrivee")
            sens = rec.get("sens")
            if name is not None and sens is not None and name not in dest_map:
                dest_map[name] = sens
        return dest_map

    # ------------------------------------------------------------------
    # PICTOGRAMME d'une ligne
    # ------------------------------------------------------------------
    async def async_get_icon_url(self, line_id: str) -> str:
        data = await self._client.async_get_page(
            self.DATASET_PICTOS,
            select="idligne,image",
            where=f"idligne={odsql_quote(line_id)}",
            limit=1,
        )
        results = data.get("results") or []
        if not results:
            return ""
        image = results[0].get("image") or {}
        return image.get("url", "") or ""

    # ------------------------------------------------------------------
    # PASSAGES TEMPS RÉEL à un arrêt, pour une ligne et un sens
    # ------------------------------------------------------------------
    async def async_get_passages(
        self,
        line_name: str,  # ex : "C4"
        sens: int,  # ex : 1
        stop_name: str,  # ex : "Villejean-Université"
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        where = (
            f"nomcourtligne={odsql_quote(line_name)} AND "
            f"sens={int(sens)} AND "
            f"nomarret={odsql_quote(stop_name)}"
        )
        data = await self._client.async_get_page(
            self.DATASET_PASSAGES,
            select="depart,departtheorique,nomarret,nomcourtligne,sens,destination,precision",
            where=where,
            order_by="departtheorique ASC",
            limit=limit,
            lang="fr",
            timezone="Europe/Paris",
        )
        return data.get("results") or []

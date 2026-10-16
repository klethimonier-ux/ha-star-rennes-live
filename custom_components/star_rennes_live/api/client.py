"""Client HTTP commun pour l'API Explore v2.1 de data.explore.star.fr.

Toutes les requêtes passent par ici :
- la clé d'API (facultative) est envoyée dans l'en-tête Authorization,
  jamais dans l'URL, pour qu'elle n'apparaisse pas dans les logs ;
- la pagination est gérée (l'endpoint /records renvoie 100 lignes maximum) ;
- les erreurs réseau / HTTP sont converties en StarApiError.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://data.explore.star.fr/api/explore/v2.1/catalog/datasets"
PAGE_SIZE = 100  # maximum accepté par /records
MAX_RECORDS = 10_000  # limite offset + limit imposée par l'API
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Jeu de données léger utilisé pour tester une clé d'API.
VALIDATION_DATASET = "tco-metro-lignes-etat-tr"


class StarApiError(Exception):
    """Erreur lors d'un appel à l'API STAR."""


class StarApiAuthError(StarApiError):
    """Clé d'API refusée par l'API STAR."""


def odsql_quote(value: Any) -> str:
    """Retourne une chaîne ODSQL entre guillemets, correctement échappée."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class StarClient:
    """Accès bas niveau aux jeux de données STAR."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str | None = None) -> None:
        self._session = session
        self._headers = {"Authorization": f"Apikey {api_key}"} if api_key else {}

    async def async_get_page(self, dataset: str, **params: Any) -> dict[str, Any]:
        """Retourne une page brute (dict JSON) d'un jeu de données."""
        url = f"{BASE_URL}/{dataset}/records"
        query = {key: value for key, value in params.items() if value is not None}

        try:
            async with self._session.get(
                url, params=query, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise StarApiAuthError(
                        f"{dataset} : accès refusé (HTTP {resp.status}), vérifiez la clé d'API"
                    )
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise StarApiError(f"{dataset} : HTTP {resp.status} – {body}")
                return await resp.json()
        except TimeoutError as err:
            raise StarApiError(f"{dataset} : délai de réponse dépassé") from err
        except aiohttp.ClientError as err:
            raise StarApiError(f"{dataset} : {err}") from err

    async def async_get_all(self, dataset: str, **params: Any) -> list[dict[str, Any]]:
        """Retourne tous les enregistrements d'un jeu de données (pagination incluse)."""
        records: list[dict[str, Any]] = []
        offset = 0

        while offset < MAX_RECORDS:
            data = await self.async_get_page(
                dataset, limit=PAGE_SIZE, offset=offset, **params
            )
            results = data.get("results") or []
            records.extend(results)
            offset += PAGE_SIZE

            total = data.get("total_count")
            if len(results) < PAGE_SIZE or (total is not None and offset >= total):
                break

        _LOGGER.debug("%s : %d enregistrement(s)", dataset, len(records))
        return records


async def async_validate_api_key(
    session: aiohttp.ClientSession, api_key: str
) -> str | None:
    """Teste une clé d'API. Retourne une clé d'erreur ou None si tout va bien."""
    client = StarClient(session, api_key)
    try:
        await client.async_get_page(VALIDATION_DATASET, limit=1)
    except StarApiAuthError:
        return "invalid_auth"
    except StarApiError:
        return "cannot_connect"
    return None

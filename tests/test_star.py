"""Banc de test : HA simulé + fausse API STAR (pagination à 100)."""
import asyncio
from datetime import datetime, timezone
import re
import sys
import unittest
from urllib.parse import urlparse

from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "stubs"))  # faux Home Assistant minimal
sys.path.insert(0, str(HERE.parent))  # racine du dépôt (custom_components)
sys.path.insert(0, str(HERE))  # imports entre fichiers de test

from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.helpers.update_coordinator import ConfigEntryNotReady  # noqa: E402

DOMAIN = "star_rennes_live"


# --------------------------------------------------------------------------
# Fausse API
# --------------------------------------------------------------------------
def make_db():
    topo = [
        {"id": f"S{i}", "nom": f"Station {i}", "codetechniquestationlignea": "x" if i < 15 else None,
         "codetechniquestationligneb": None if i < 15 else "y", "coordonnees": {"lat": 48.1, "lon": -1.6}}
        for i in range(30)
    ]
    equip = [
        {"idequipement": f"E{i}", "nom": f"Ascenseur {i}", "idstation": f"S{i % 30}", "nomstation": f"Station {i % 30}",
         "etat": ["En fonctionnement"], "typecourt": "ASC", "type": "Ascenseur", "idligne": "1001"}
        for i in range(150)
    ]
    return {
        "tco-metro-lignes-etat-tr": [
            {"nomcourt": "a", "idligne": "1001", "etat": "OK"},
            {"nomcourt": "b", "idligne": "1002", "etat": "OK"},
        ],
        "tco-metro-topologie-stations-td": topo,
        "tco-metro-stations-etat-tr": [{"idstation": f"S{i}", "nom": f"Station {i}", "etat": "Ouverte", "idligne": "1001"} for i in range(15)],
        "tco-metro-equipements-etat-tr": equip,
        "vls-stations-etat-tr": [
            {"idstation": i, "nom": f"Vélo {i}", "etat": "En fonctionnement" if i % 2 else ["En fonctionnement"],
             "nombrevelosdisponibles": 3} for i in range(120)
        ],
        "tco-parcsrelais-star-etat-tr": [{"idparc": "P1", "nom": "Henri Fréville", "etatouverture": "OUVERT"}],
        "tco-bus-topologie-lignes-td": [{"id": "0001", "nomcourt": "C1"}, {"id": "0004", "nomcourt": "C4"}],
        "tco-bus-topologie-dessertes-td": [{"idarret": "1", "nomarret": "République"}, {"idarret": "2", "nomarret": "Place de l'Église"}],
        "tco-bus-topologie-parcours-td": [{"nomarretarrivee": "Cesson", "sens": 0}, {"nomarretarrivee": "Rennes", "sens": 1}],
        "tco-bus-lignes-pictogrammes-dm": [{"idligne": "0001", "image": {"url": "https://img/c1.png"}}],
        "tco-bus-circulation-passages-tr": [
            {"nomarret": "République", "depart": "2026-10-14T20:30:00+02:00"},
            {"nomarret": "République", "depart": "2026-10-14T20:20:00+02:00"},
        ],
    }


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, db, status=200, valid_key=None):
        self.db = db
        self.status = status
        self.valid_key = valid_key
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith(".zip"):  # GTFS : non simulé ici (voir test_gtfs.py)
            return FakeResp(404, {})
        dataset = urlparse(url).path.split("/")[-2]
        self.calls.append((dataset, dict(params or {}), dict(headers or {})))
        if "apikey" in url or "apikey" in (params or {}):
            raise AssertionError("clé d'API dans l'URL")
        auth = (headers or {}).get("Authorization")
        if self.valid_key and auth and auth != f"Apikey {self.valid_key}":
            return FakeResp(401, {"error": "bad key"})
        if self.status != 200:
            return FakeResp(self.status, {"error": "boom"})
        limit = int(params.get("limit", 10))
        assert limit <= 100, f"limit {limit} > 100"
        offset = int(params.get("offset", 0))
        rows = self.db.get(dataset, [])
        where = params.get("where")
        if where and where.startswith("idstation="):
            wanted = set(re.findall(r'idstation="([^"]*)"', where))
            self.assertion_where = where
            rows = [r for r in rows if r.get("idstation") in wanted]
        if where and dataset == "tco-busmetro-trafic-alertes-tr" and where.startswith("nomcourtligne="):
            wanted = set(re.findall(r'nomcourtligne="([^"]*)"', where))
            rows = [r for r in rows if r.get("nomcourtligne") in wanted]
        return FakeResp(200, {"total_count": len(rows), "results": rows[offset:offset + limit]})

    def datasets(self):
        return [c[0] for c in self.calls]


class FakeConfigEntries:
    def __init__(self):
        self.forwarded = []
        self.reloaded = []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry.entry_id, platforms))

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)

    async def async_unload_platforms(self, entry, platforms):
        return True


class FakeHass:
    def __init__(self, session):
        self.session = session
        self.data = {}
        self.config_entries = FakeConfigEntries()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
class ClientTests(unittest.TestCase):
    def test_pagination_header_and_errors(self):
        from custom_components.star_rennes_live.api.client import StarClient, StarApiError, StarApiAuthError, odsql_quote

        s = FakeSession(make_db())
        recs = run(StarClient(s, "KEY").async_get_all("tco-metro-equipements-etat-tr"))
        self.assertEqual(len(recs), 150)
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(s.calls[0][2], {"Authorization": "Apikey KEY"})

        run(StarClient(s, None).async_get_page("tco-metro-lignes-etat-tr", limit=1))
        self.assertEqual(s.calls[-1][2], {})

        with self.assertRaises(StarApiError):
            run(StarClient(FakeSession(make_db(), status=500)).async_get_all("x"))
        with self.assertRaises(StarApiAuthError):
            run(StarClient(FakeSession(make_db(), valid_key="GOOD"), "BAD").async_get_page("x"))

        self.assertEqual(odsql_quote('Place de l\'Église "x"'), '"Place de l\'Église \\"x\\""')

    def test_bus_query_is_escaped(self):
        from custom_components.star_rennes_live.api.client import StarClient
        from custom_components.star_rennes_live.api.bus_api import StarBusAPI

        s = FakeSession(make_db())
        run(StarBusAPI(StarClient(s)).async_get_passages("C1", "1", "Place de l'Église"))
        where = s.calls[-1][1]["where"]
        self.assertEqual(where, 'nomcourtligne="C1" AND sens=1 AND nomarret="Place de l\'Église"')

    def test_bike_state_list_or_string(self):
        from custom_components.star_rennes_live.api.client import StarClient
        from custom_components.star_rennes_live.api.bike_api import StarBikeAPI

        st = run(StarBikeAPI(StarClient(FakeSession(make_db()))).async_get_stations())
        self.assertEqual(len(st), 120)  # 2 pages
        self.assertEqual({v["status"] for v in st.values()}, {"En fonctionnement"})


def setup_entry(config, session=None):
    from custom_components.star_rennes_live import async_setup_entry
    from custom_components.star_rennes_live.sensor import async_setup_entry as sensor_setup

    session = session or FakeSession(make_db())
    hass = FakeHass(session)
    entry = ConfigEntry(data=config)
    added = []

    async def go():
        await async_setup_entry(hass, entry)
        await sensor_setup(hass, entry, lambda ents, *a: added.extend(ents))

    run(go())
    return hass, entry, session, added


class SetupTests(unittest.TestCase):
    def test_bus_only_queries_only_bus(self):
        bus = {"line_id": "0001", "line_code": "C1", "stop_name": "République", "dest_name": "Cesson", "sens": 0, "icon_url": ""}
        hass, entry, session, added = setup_entry({"bus": True, "bus_entries": [bus]})
        # passages + alertes trafic de la ligne suivie
        self.assertEqual(sorted(session.datasets()), ["tco-bus-circulation-passages-tr", "tco-busmetro-trafic-alertes-tr"])
        alert_call = next(c for c in session.calls if c[0] == "tco-busmetro-trafic-alertes-tr")
        self.assertEqual(alert_call[1]["where"], 'nomcourtligne="C1"')
        coords = hass.data[DOMAIN][entry.entry_id]
        self.assertNotIn("metro", coords)
        self.assertNotIn("bike", coords)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].native_value.isoformat(), "2026-10-14T20:20:00+02:00")
        self.assertEqual(len(entry.update_listeners), 1)

    def test_metro_lines_only(self):
        hass, entry, session, added = setup_entry({"metro": True, "metro_lines": ["A"], "metro_stations": []})
        # lignes + stations + équipements de tout le réseau (fermetures déduites, sans filtre)
        datasets = sorted(set(session.datasets()))
        self.assertEqual(datasets, ["tco-metro-equipements-etat-tr", "tco-metro-lignes-etat-tr", "tco-metro-stations-etat-tr"])
        self.assertTrue(all("where" not in c[1] or c[1]["where"] is None for c in session.calls if c[0] == "tco-metro-equipements-etat-tr"))
        self.assertEqual([e._attr_name for e in added], ["STAR Perturbations", "Métro Ligne A"])
        self.assertEqual(added[0].native_value, 0)
        line = added[1].extra_state_attributes
        self.assertEqual((line["line_code"], line["perturbation"]), ("A", False))

    def test_metro_nothing_selected_creates_nothing(self):
        hass, entry, session, added = setup_entry({"metro": True, "metro_lines": [], "metro_stations": []})
        self.assertEqual(session.calls, [])
        self.assertEqual(added, [])

    def test_metro_stations_requests_and_live_updates(self):
        hass, entry, session, added = setup_entry(
            {"metro": True, "metro_lines": ["B"], "metro_stations": ["S3"]}
        )
        # 1er cycle : topologie + lignes + stations RT + équipements filtrés (1 page)
        # + équipements de tout le réseau (fermetures déduites, en cache 5 min)
        first = sorted(session.datasets())
        self.assertEqual(first.count("tco-metro-topologie-stations-td"), 1)
        equip_calls = [c for c in session.calls if c[0] == "tco-metro-equipements-etat-tr"]
        filtered = [c for c in equip_calls if c[1].get("where")]
        network_pages = len(equip_calls) - len(filtered)
        self.assertEqual(len(filtered), 1)
        self.assertGreaterEqual(network_pages, 1)
        self.assertEqual(len(first), 5 + network_pages)  # + temps réel métro (prochains passages)
        self.assertIn("tco-metro-circulation-deux-prochains-passages-tr", first)
        self.assertEqual(filtered[0][1]["where"], 'idstation="S3"')

        names = [e._attr_name for e in added]
        self.assertIn("Métro Ligne B", names)
        self.assertIn("Station Station 3", names)
        equip = [e for e in added if type(e).__name__ == "StarMetroEquipmentSensor"]
        self.assertEqual(len(equip), 5)  # E3, E33, E63, E93, E123
        line_b = next(e for e in added if e._attr_name == "Métro Ligne B")
        station = next(e for e in added if e._attr_name == "Station Station 3")
        self.assertEqual(line_b.native_value, "OK")
        self.assertEqual(station.native_value, "Ouverte")
        self.assertEqual(equip[0].native_value, "En fonctionnement")

        # Les données changent côté API -> les entités suivent (bug n°1)
        session.db["tco-metro-lignes-etat-tr"][1]["etat"] = "En panne"
        session.db["tco-metro-stations-etat-tr"][3]["etat"] = "Fermée"
        for r in session.db["tco-metro-equipements-etat-tr"]:
            r["etat"] = ["Hors service"]
        session.calls.clear()
        coord = hass.data[DOMAIN][entry.entry_id]["metro"]
        run(coord.async_refresh())
        self.assertEqual(line_b.native_value, "En panne")
        self.assertEqual(station.native_value, "Fermée")
        self.assertEqual(equip[0].native_value, "Hors service")

        # Station fermée : alerte en journée, rien la nuit (fin de service)
        import homeassistant.util.dt as dt_util
        pert = next(e for e in added if e._attr_name == "STAR Perturbations")
        titles = lambda: [p["titre"] for p in pert.extra_state_attributes["perturbations"]]
        try:
            dt_util._now[0] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # mercredi 12 h
            run(coord.async_refresh())
            self.assertTrue(any(t.endswith("fermée") for t in titles()), titles())
            dt_util._now[0] = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)  # jeudi 3 h
            run(coord.async_refresh())
            self.assertFalse(any(t.endswith("fermée") for t in titles()), titles())
            self.assertTrue(any(t.startswith("Ligne b") for t in titles()))  # la panne de ligne reste
        finally:
            dt_util._now[0] = None
        session.calls.clear()
        run(coord.async_refresh())
        # 2e cycle : pas de topologie (cache) -> 3 requêtes
        self.assertNotIn("tco-metro-topologie-stations-td", session.datasets())
        self.assertEqual(len(session.calls), 3)

        # Panne API -> entités indisponibles
        session.status = 503
        run(coord.async_refresh())
        self.assertFalse(line_b.available)

    def test_metro_equipment_filter_several_stations(self):
        from custom_components.star_rennes_live.api.client import StarClient
        from custom_components.star_rennes_live.api.metro_api import StarMetroAPI

        s = FakeSession(make_db())
        eq = run(StarMetroAPI(StarClient(s)).async_get_equipments(["S3", "S1", "S3"]))
        self.assertEqual(s.calls[-1][1]["where"], 'idstation="S1" OR idstation="S3"')
        self.assertEqual(len(eq), 10)
        self.assertEqual({e["station_id"] for e in eq.values()}, {"S1", "S3"})

    def test_metro_interchange_station_keeps_both_lines(self):
        db = make_db()
        db["tco-metro-topologie-stations-td"].append(
            {"id": "15008", "nom": "Gares", "codetechniquestationlignea": "GAR", "codetechniquestationligneb": "GARB"}
        )
        gares_b = {"idstation": "15008", "nom": "Gares", "etat": "Ouverte", "lastupdate": "2026-10-14T18:57:09+00:00", "idligne": "1002"}
        gares_a = {"idstation": "15008", "nom": "Gares", "etat": "Ouverte", "lastupdate": "2026-10-14T18:57:07+00:00", "idligne": "1001"}
        db["tco-metro-stations-etat-tr"] += [gares_b, gares_a]
        db["tco-metro-equipements-etat-tr"] += [
            {"idequipement": "GAR_DAT03103", "nom": "Distributeur 3", "idstation": "15008", "etat": ["En fonctionnement"], "typecourt": "DAT", "idligne": "1001"},
            {"idequipement": "ESC_GARB_1", "nom": "Quai vers Viasilva -> Salle des billets", "idstation": "15008", "nomstation": "Gares", "etat": ["En fonctionnement"], "typecourt": "ESC", "idligne": "1002"},
            {"idequipement": "PR_ASC_JFK_1", "nom": "Ascenseur 1 P+R", "idstation": "15008", "etat": ["En fonctionnement"], "typecourt": "PR_ASC", "idligne": "1001"},
        ]
        session = FakeSession(db)
        hass, entry, session, added = setup_entry({"metro": True, "metro_stations": ["15008"]}, session)
        station = next(e for e in added if e._attr_name == "Station Gares")
        self.assertEqual(station.native_value, "Ouverte")
        attrs = station.extra_state_attributes
        self.assertEqual(attrs["etats_par_ligne"], {"A": "Ouverte", "B": "Ouverte"})
        self.assertEqual(attrs["line_code"], "A/B")
        self.assertEqual(attrs["lastupdate"], "2026-10-14T18:57:09+00:00")
        equip = {e._equipment_id: e for e in added if type(e).__name__ == "StarMetroEquipmentSensor"}
        self.assertEqual(set(equip), {"GAR_DAT03103", "ESC_GARB_1", "PR_ASC_JFK_1"})
        self.assertEqual(equip["PR_ASC_JFK_1"].icon, "mdi:elevator")
        self.assertEqual(
            equip["ESC_GARB_1"]._attr_name,
            "Gares – ESC – Quai vers Viasilva -> Salle des billets (ESC_GARB_1)",
        )
        # Sans nomstation : repli sur l'identifiant de station
        self.assertEqual(
            equip["GAR_DAT03103"]._attr_name,
            "Station 15008 – DAT – Distributeur 3 (GAR_DAT03103)",
        )

        # Ligne B fermée, ligne A ouverte : les deux états restent visibles
        gares_b["etat"] = "Fermée"
        run(hass.data[DOMAIN][entry.entry_id]["metro"].async_refresh())
        self.assertEqual(station.native_value, "A : Ouverte, B : Fermée")
        self.assertEqual(station.extra_state_attributes["etats_par_ligne"], {"A": "Ouverte", "B": "Fermée"})

    def test_equipment_name_with_station(self):
        from custom_components.star_rennes_live.metro.sensor import StarMetroEquipmentSensor

        class C:
            data = {}
        eq = {"id": "ASC_ANF_2", "name": "Quai vers Kennedy <-> Salle des billets", "station_id": "15004",
              "station_name": "Anatole France", "type_short": "ASC"}
        e = StarMetroEquipmentSensor(C(), "e1", eq)
        self.assertEqual(e._attr_name, "Anatole France – ASC – Quai vers Kennedy <-> Salle des billets (ASC_ANF_2)")
        self.assertEqual(e._attr_unique_id, "e1_metro_equipment_ASC_ANF_2")

    def test_bike_en_panne(self):
        db = make_db()
        db["vls-stations-etat-tr"][5]["etat"] = ["En panne"]
        db["vls-stations-etat-tr"][6]["etat"] = ["En maintenance"]
        db["vls-stations-etat-tr"][7]["etat"] = ["Fermée"]
        hass, entry, session, added = setup_entry(
            {"bike": True, "bike_stations": ["4", "5", "6", "7"]}, FakeSession(db)
        )
        states = {e.station_id: (e.native_value, e.icon) for e in added}
        self.assertEqual(states["4"], ("Ouvert", "mdi:bicycle"))
        self.assertEqual(states["5"], ("En panne", "mdi:alert-circle"))
        self.assertEqual(states["6"], ("Maintenance", "mdi:tools"))
        self.assertEqual(states["7"], ("Fermé", "mdi:close-octagon"))

    def test_api_down_at_startup_raises_not_ready(self):
        with self.assertRaises(ConfigEntryNotReady):
            setup_entry({"parking": True, "parking_ids": ["P1"]}, FakeSession(make_db(), status=500))

    def test_bike_and_parking(self):
        hass, entry, session, added = setup_entry(
            {"bike": True, "bike_stations": ["5"], "parking": True, "parking_ids": ["P1"]}
        )
        self.assertEqual(sorted(type(e).__name__ for e in added), ["StarBikeStationSensor", "StarParkingSensor"])
        bike = next(e for e in added if type(e).__name__ == "StarBikeStationSensor")
        self.assertEqual(bike.native_value, "Ouvert")
        park = next(e for e in added if type(e).__name__ == "StarParkingSensor")
        self.assertEqual(park.native_value, "Ouvert")

    def test_update_listener_reloads(self):
        hass, entry, session, added = setup_entry({"metro": True, "metro_lines": ["A"]})
        run(entry.update_listeners[0](hass, entry))
        self.assertEqual(hass.config_entries.reloaded, [entry.entry_id])


class CardDataTests(unittest.TestCase):
    """Données exposées pour la carte, sur des noms d'équipements réels."""

    def eq(self, eid, name, typ, line, state="En fonctionnement"):
        return {"id": eid, "name": name, "type_short": typ, "line_code": line, "state": state, "station_id": "X"}

    def test_directions_italie_like(self):
        from custom_components.star_rennes_live.api.metro_api import summarize_directions

        equipments = [
            self.eq("ASC_ITA_1", "Quai vers Kennedy <-> Salle des billets", "ASC", "A"),
            self.eq("ASC_ITA_2", "Quai vers La Poterie <-> Salle des billets", "ASC", "A"),
            self.eq("ESC_ITA_1", "Quai vers Kennedy -> Extérieur", "ESC", "A"),
            self.eq("ESC_ITA_2", "Quai vers La Poterie -> Extérieur", "ESC", "A", "Indisponible"),
            self.eq("ESC_ITA_3", "Salle des billets -> Extérieur", "ESC", "A"),  # commun aux 2 sens
            self.eq("ITA_DAT1", "Distributeur 1", "DAT", "A"),
            self.eq("PR_ASC_X", "Ascenseur 1 P+R  La Poterie", "PR_ASC", "A"),  # ignoré
        ]
        dirs = {d["direction"]: d for d in summarize_directions("A", equipments)}
        self.assertEqual(list(dirs), ["J.F. Kennedy", "La Poterie"])
        k, p = dirs["J.F. Kennedy"], dirs["La Poterie"]
        self.assertEqual((k["ascenseurs"]["total"], k["ascenseurs"]["hs"]), (1, 0))
        self.assertEqual((k["escaliers"]["total"], k["escaliers"]["hs"]), (2, 0))
        self.assertEqual((p["escaliers"]["total"], p["escaliers"]["hs"]), (2, 1))
        self.assertEqual(p["escaliers"]["hs_noms"], ["Quai vers La Poterie -> Extérieur"])
        self.assertEqual(k["distributeurs"]["total"], 1)

    def test_directions_gares_interchange(self):
        from custom_components.star_rennes_live.api.metro_api import summarize_directions

        equipments = [
            self.eq("ESC_GARB_10", "Gares ligne b -> Quai vers Kennedy (ligne a)", "ESC", "B"),
            self.eq("ESC_GARB_7", "Quais vers Viasilva et vers Gaîté -> Salle des billets", "ESC", "B"),
            self.eq("STAB", "Quai direction Gaîté (Droite)", "DAT", "B"),
        ]
        dirs = {d["direction"]: d for d in summarize_directions("A/B", equipments)}
        self.assertEqual(set(dirs), {"J.F. Kennedy", "La Poterie", "Cesson - Viasilva", "Saint-Jacques - Gaîté"})
        self.assertEqual(dirs["J.F. Kennedy"]["escaliers"]["total"], 1)  # classé ligne a malgré idligne B
        self.assertEqual(dirs["Cesson - Viasilva"]["escaliers"]["total"], 1)
        self.assertEqual(dirs["Saint-Jacques - Gaîté"]["escaliers"]["total"], 1)
        self.assertEqual(dirs["Saint-Jacques - Gaîté"]["distributeurs"]["total"], 1)
        self.assertEqual(dirs["La Poterie"]["escaliers"]["total"], 0)

    def test_perturbations_and_closed_station(self):
        from custom_components.star_rennes_live.api.metro_api import build_metro_structure
        from custom_components.star_rennes_live.coordinator_metro import _format_time

        lines = {
            "A": {"state": "OK", "finpanneprevue": None},
            "B": {"state": "Trafic interrompu", "finpanneprevue": "2026-10-14T20:30:00+00:00", "pannedepuis": None},
        }
        rt = {
            "15005": {"name": "Sainte-Anne", "states_by_line": {"A": "Fermée", "B": "Fermée"}, "state": "Fermée", "lastupdate": None},
            "15012": {"name": "Italie", "states_by_line": {"A": "Ouverte"}, "state": "Ouverte", "lastupdate": None},
            "15008": {"name": "Gares", "states_by_line": {"A": "Ouverte", "B": "Fermée"}, "state": "A : Ouverte, B : Fermée", "lastupdate": None},
        }
        topo = {sid: {"id": sid, "name": s["name"], "line_code": "A"} for sid, s in rt.items()}
        data = build_metro_structure(lines, topo, rt, {}, _format_time)
        p = data["perturbations"]
        self.assertEqual([x["titre"] for x in p], [
            "Ligne b · trafic interrompu", "Station Gares fermée", "Station Sainte-Anne fermée"])
        self.assertEqual(p[0]["detail"], "Reprise estimée vers 22:30")  # 20:30 UTC -> 22:30 Paris
        self.assertEqual(p[2]["detail"], "Lignes a et b · les rames ne s'y arrêtent pas")
        self.assertEqual(p[1]["detail"], "Ligne b · les rames ne s'y arrêtent pas")
        self.assertTrue(data["stations"]["15005"]["closed"])
        self.assertFalse(data["stations"]["15008"]["closed"])
        self.assertEqual(data["stations"]["15008"]["closed_lines"], ["B"])
        self.assertFalse(data["stations"]["15012"]["closed"])

    def test_format_time_raw_fallback(self):
        from custom_components.star_rennes_live.coordinator_metro import _format_time
        self.assertEqual(_format_time("22:30"), "22:30")
        self.assertIsNone(_format_time(None))

    def test_bus_attributes_for_card(self):
        from datetime import datetime, timezone
        import homeassistant.util.dt as dt_util
        db = make_db()
        db["tco-bus-circulation-passages-tr"] = [
            {"nomarret": "République", "depart": "2026-10-14T19:12:48+00:00", "precision": "Temps réel"},
            {"nomarret": "République", "depart": "2026-10-14T19:27:00+00:00", "precision": "Applicable"},
        ]
        dt_util._now[0] = datetime(2026, 10, 14, 19, 9, 30, tzinfo=timezone.utc)
        try:
            bus = {"line_id": "0001", "line_code": "C1", "stop_name": "République", "dest_name": "Chantepie", "sens": 0, "icon_url": ""}
            hass, entry, session, added = setup_entry({"bus": True, "bus_entries": [bus]}, FakeSession(db))
            a = added[0].extra_state_attributes
        finally:
            dt_util._now[0] = None
        self.assertEqual(a["ligne"], "C1")
        self.assertEqual(a["prochains_departs_min"], [3, 17])
        self.assertTrue(a["temps_reel"])
        self.assertEqual(a["departs"], [
            {"depart": "2026-10-14T19:12:48+00:00", "temps_reel": True},
            {"depart": "2026-10-14T19:27:00+00:00", "temps_reel": False},
        ])
        passages_call = next(c for c in session.calls if c[0] == "tco-bus-circulation-passages-tr")
        self.assertIn("precision", passages_call[1]["select"])

    def test_card_registered(self):
        from custom_components.star_rennes_live import async_setup, CARD_URL
        from homeassistant.components import frontend

        registered = []

        class Http:
            async def async_register_static_paths(self, paths):
                registered.extend(paths)

        class H:
            http = Http()

            async def async_add_executor_job(self, fn):
                return fn()

        self.assertTrue(run(async_setup(H(), {})))
        self.assertEqual(registered[0].url_path, CARD_URL)
        self.assertTrue(registered[0].path.endswith("frontend/star-trajets-card.js"))
        self.assertTrue(frontend._urls[-1].startswith(CARD_URL + "?v="))


def options_flow(entry_data, session=None):
    from custom_components.star_rennes_live.options_flow import StarRennesOptionsFlow

    flow = StarRennesOptionsFlow(ConfigEntry(data=entry_data))
    flow.hass = FakeHass(session or FakeSession(make_db()))
    return flow


BUS = {"line_id": "0004", "line_code": "C4", "stop_name": "République", "dest_name": "Rennes", "sens": 1, "icon_url": ""}


class OptionsFlowTests(unittest.TestCase):
    def test_enable_metro_sets_flag_and_creates_entry(self):
        flow = options_flow({"metro": False, "bus": True, "bus_entries": [BUS]})
        r = run(flow.async_step_init({"bus": True, "metro": True, "bike": False, "parking": False}))
        self.assertEqual(r["step_id"], "configure")
        r = run(flow.async_step_configure({"bus_existing": ["0"], "add_bus": False, "metro_lines": ["A"], "metro_stations": []}))
        self.assertEqual(r["type"], "create_entry")
        self.assertTrue(r["data"]["metro"])
        self.assertEqual(r["data"]["metro_lines"], ["A"])
        self.assertEqual(r["data"]["bus_entries"], [BUS])

    def test_disable_metro_clears_flag(self):
        flow = options_flow({"metro": True, "metro_lines": ["A"], "metro_stations": ["S1"]})
        run(flow.async_step_init({"bus": False, "metro": False, "bike": False, "parking": False}))
        r = run(flow.async_step_configure({}))
        self.assertFalse(r["data"]["metro"])
        self.assertEqual(r["data"]["metro_stations"], [])

    def test_add_bus_keeps_other_changes(self):
        flow = options_flow({"metro": True, "metro_lines": ["A"], "bus": True, "bus_entries": [BUS, dict(BUS, stop_name="Gares")]})
        run(flow.async_step_init({"bus": True, "metro": True, "bike": False, "parking": False}))
        # on supprime le 2e bus, on passe la ligne B, et on ajoute un bus
        r = run(flow.async_step_configure({"bus_existing": ["0"], "add_bus": True, "metro_lines": ["B"], "metro_stations": []}))
        self.assertEqual(r["step_id"], "bus_line")
        run(flow.async_step_bus_line({"bus_line": "C1"}))
        r = run(flow.async_step_bus_stop({"bus_stop": "Place de l'Église"}))
        self.assertEqual(r["step_id"], "bus_destination")
        r = run(flow.async_step_bus_destination({"bus_destination": "Cesson"}))
        self.assertEqual(r["type"], "create_entry")
        self.assertEqual(r["data"]["metro_lines"], ["B"])
        self.assertEqual([b["stop_name"] for b in r["data"]["bus_entries"]], ["République", "Place de l'Église"])
        new_bus = r["data"]["bus_entries"][1]
        self.assertEqual((new_bus["line_id"], new_bus["sens"], new_bus["icon_url"]), ("0001", 0, "https://img/c1.png"))

    def test_api_key_validation_and_clearing(self):
        flow = options_flow({"api_key": "GOOD", "metro_lines": ["A"]}, FakeSession(make_db(), valid_key="GOOD"))
        r = run(flow.async_step_init({"metro": True, "api_key": "BAD"}))
        self.assertEqual(r["errors"], {"api_key": "invalid_auth"})
        r = run(flow.async_step_init({"metro": True}))  # champ vidé
        self.assertEqual(r["step_id"], "configure")
        r = run(flow.async_step_configure({"metro_lines": ["A"], "metro_stations": []}))
        self.assertEqual(r["data"]["api_key"], "")


class ConfigFlowTests(unittest.TestCase):
    def test_full_flow_with_key(self):
        from custom_components.star_rennes_live.config_flow import StarRennesConfigFlow

        session = FakeSession(make_db(), valid_key="GOOD")
        flow = StarRennesConfigFlow()
        flow.hass = FakeHass(session)
        r = run(flow.async_step_user({"bus": True, "metro": True, "bike": True, "parking": False, "api_key": "BAD"}))
        self.assertEqual(r["errors"], {"api_key": "invalid_auth"})
        r = run(flow.async_step_user({"bus": True, "metro": True, "bike": True, "parking": False, "api_key": " GOOD "}))
        self.assertEqual(r["step_id"], "bus_line")
        run(flow.async_step_bus_line({"bus_line": "C4"}))
        run(flow.async_step_bus_stop({"bus_stop": "République"}))
        r = run(flow.async_step_bus_destination({"bus_destination": "Rennes"}))
        self.assertEqual(r["step_id"], "select_equipment")
        r = run(flow.async_step_select_equipment({"metro_lines": ["A"], "bike_stations": ["5"]}))
        self.assertEqual(r["type"], "create_entry")
        self.assertEqual(r["data"]["api_key"], "GOOD")
        self.assertEqual(r["data"]["bus_entries"][0]["sens"], 1)
        self.assertTrue(all(c[2] == {"Authorization": "Apikey GOOD"} for c in session.calls[1:]))


class DeducedClosureTests(unittest.TestCase):
    """Station fermée non publiée par STAR : tous les ascenseurs et escaliers HS."""

    def eq(self, eid, sid, kind, state, line="A", name="Quai vers Kennedy"):
        return {"id": eid, "station_id": sid, "station_name": "République", "type_short": kind,
                "state": state, "line_code": line, "name": name}

    def test_all_access_hs_closes_station(self):
        from custom_components.star_rennes_live.api.metro_api import build_metro_structure

        rt = {"R": {"id": "R", "name": "République", "states_by_line": {"A": "Ouverte"}, "state": "Ouverte", "lastupdate": None}}
        equipments = {
            "1": self.eq("1", "R", "ASC", "Hors service"),
            "2": self.eq("2", "R", "ESC", "Hors service"),
            "3": self.eq("3", "R", "ESC", "En panne"),
            "4": self.eq("4", "R", "DAT", "En fonctionnement"),  # distributeur : ne compte pas
        }
        topo = {"R": {"id": "R", "name": "République", "line_code": "A"}}
        data = build_metro_structure({}, topo, rt, equipments)
        station = data["stations"]["R"]
        self.assertEqual(station["state"], "Fermée")
        self.assertTrue(station["closed"])
        self.assertEqual(station["closed_by_equipment"], ["A"])
        p = data["perturbations"][0]
        self.assertEqual(p["titre"], "Station République fermée")
        self.assertTrue(p["deduite"])
        self.assertIn("ascenseurs et escaliers", p["detail"])
        self.assertEqual(rt["R"]["state"], "Ouverte")  # l'entrée d'origine n'est pas modifiée

    def test_partial_or_unknown_keeps_open(self):
        from custom_components.star_rennes_live.api.metro_api import access_closed_lines

        self.assertEqual(access_closed_lines([self.eq("1", "R", "ASC", "Hors service"),
                                              self.eq("2", "R", "ESC", "En fonctionnement")]), [])
        self.assertEqual(access_closed_lines([self.eq("1", "R", "ASC", "Hors service"),
                                              self.eq("2", "R", "ESC", "Inconnu")]), [])
        # Un seul équipement HS : pas assez pour conclure
        self.assertEqual(access_closed_lines([self.eq("1", "R", "ASC", "Hors service")]), [])

    def test_interchange_closed_on_one_line_only(self):
        from custom_components.star_rennes_live.api.metro_api import build_metro_structure

        rt = {"G": {"id": "G", "name": "Gares", "states_by_line": {"A": "Ouverte", "B": "Ouverte"}, "state": "Ouverte", "lastupdate": None}}
        equipments = {
            "1": self.eq("1", "G", "ASC", "Hors service", "B"), "2": self.eq("2", "G", "ESC", "Hors service", "B"),
            "3": self.eq("3", "G", "ASC", "En fonctionnement", "A"), "4": self.eq("4", "G", "ESC", "Hors service", "A"),
        }
        data = build_metro_structure({}, {}, rt, equipments)
        p = data["perturbations"][0]
        self.assertEqual(p["lignes"], ["B"])
        self.assertIn("Ligne b", p["detail"])

    def test_station_missing_from_realtime_is_created(self):
        from custom_components.star_rennes_live.api.metro_api import apply_access_closures

        result = apply_access_closures({}, [self.eq("1", "X", "ASC", "Hors service", "B"),
                                            self.eq("2", "X", "ESC", "Hors service", "B")])
        self.assertEqual(result["X"]["states_by_line"], {"B": "Fermée"})
        self.assertEqual(result["X"]["name"], "République")


class TrafficAlertsTests(unittest.TestCase):
    """Alertes trafic réelles du 17/10/2026 : manifestation, tous les bus arrêtés."""

    def records(self):
        import json
        return json.load(open(HERE / "fixture_alertes.json", encoding="utf-8"))["results"]

    def test_grouped_and_active(self):
        from custom_components.star_rennes_live.api.alerts_api import active_alerts, alerts_for_line

        now = datetime(2026, 10, 17, 13, 43, tzinfo=timezone.utc)
        alerts = active_alerts(self.records(), now)
        # Suspensions d'abord
        self.assertEqual([a["titre"] for a in alerts], ["Manifestation", "Arrêt TNB"])
        manif = alerts[0]
        self.assertEqual(manif["type"], "suspension")
        self.assertTrue(manif["bloquante"])
        self.assertEqual(manif["lignes"][:4], ["C1", "C2", "C3", "C4"])
        self.assertIn("54", manif["lignes"])
        self.assertEqual(len(manif["lignes"]), 19)  # une seule alerte pour 19 fiches
        tnb = alerts[1]
        self.assertEqual(tnb["type"], "info")  # « Bloquante » aussi, mais simple report d'arrêt
        self.assertEqual(tnb["directions"], ["Roazhon Park/ZI Ouest"])
        self.assertEqual(tnb["description"], "Pendant plusieurs mois · Ligne 11 vers Roazhon Park/ZI Ouest · L'arrêt TNB est reporté à l'arrêt République.")
        self.assertEqual([a["titre"] for a in alerts_for_line(alerts, "11")], ["Manifestation", "Arrêt TNB"])
        self.assertEqual([a["titre"] for a in alerts_for_line(alerts, "C4")], ["Manifestation"])
        # Le soir, la manifestation est terminée (fin 21:59 UTC) ; TNB court jusqu'au 31/12
        later = active_alerts(self.records(), datetime(2026, 10, 17, 22, 30, tzinfo=timezone.utc))
        self.assertEqual([a["titre"] for a in later], ["Arrêt TNB"])
        # Pas encore commencée
        before = active_alerts(self.records(), datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(before, [])

    def test_bus_sensor_exposes_line_alerts(self):
        import homeassistant.util.dt as dt_util
        db = make_db()
        db["tco-busmetro-trafic-alertes-tr"] = self.records()
        dt_util._now[0] = datetime(2026, 10, 17, 13, 43, tzinfo=timezone.utc)
        try:
            buses = [
                {"line_id": "0004", "line_code": "C4", "stop_name": "République", "dest_name": "Grand Quartier", "sens": 0, "icon_url": ""},
                {"line_id": "0011", "line_code": "11", "stop_name": "République", "dest_name": "Roazhon Park", "sens": 0, "icon_url": ""},
            ]
            hass, entry, session, added = setup_entry({"bus": True, "bus_entries": buses}, FakeSession(db))
            by_line = {e.extra_state_attributes["ligne"]: e.extra_state_attributes["alertes"] for e in added}
        finally:
            dt_util._now[0] = None
        alert_calls = [c for c in session.calls if c[0] == "tco-busmetro-trafic-alertes-tr"]
        self.assertEqual(len(alert_calls), 1)  # une seule requête pour toutes les lignes
        self.assertEqual([a["titre"] for a in by_line["C4"]], ["Manifestation"])
        self.assertEqual(by_line["C4"][0]["lignes"], ["C4", "11"])  # seulement les lignes suivies
        self.assertTrue(by_line["C4"][0]["bloquante"])
        self.assertEqual([a["titre"] for a in by_line["11"]], ["Manifestation", "Arrêt TNB"])
        tnb = by_line["11"][1]
        self.assertEqual((tnb["type"], tnb["ce_sens"]), ("info", True))  # bus 11 vers Roazhon Park

    def test_suspension_and_direction_rules(self):
        from custom_components.star_rennes_live.api.alerts_api import is_suspension, concerns_direction

        self.assertTrue(is_suspension("Manifestation", "Arrêt de circulation de toutes les lignes de BUS jusqu'à nouvel ordre."))
        self.assertTrue(is_suspension("Incident", "La circulation est interrompue entre Gares et Clemenceau"))
        self.assertTrue(is_suspension("Grève", "Les bus de la ligne 9 ne circulent pas"))
        self.assertFalse(is_suspension("Arrêt TNB", "L'arrêt TNB est reporté à l'arrêt République."))
        self.assertFalse(is_suspension("Travaux", "Déviation de la ligne C4 rue de Paris"))
        tnb = {"directions": ["Roazhon Park/ZI Ouest"]}
        self.assertTrue(concerns_direction(tnb, "Roazhon Park"))
        self.assertTrue(concerns_direction(tnb, "ZI Ouest"))
        self.assertFalse(concerns_direction(tnb, "Saint-Laurent"))
        self.assertTrue(concerns_direction({"directions": []}, "Saint-Laurent"))  # pas de sens cité : les deux

    def test_alerts_failure_does_not_block_buses(self):
        db = make_db()
        db.pop("tco-busmetro-trafic-alertes-tr", None)
        bus = {"line_id": "0001", "line_code": "C1", "stop_name": "République", "dest_name": "Cesson", "sens": 0, "icon_url": ""}
        hass, entry, session, added = setup_entry({"bus": True, "bus_entries": [bus]}, FakeSession(db))
        self.assertEqual(added[0].extra_state_attributes["alertes"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests du GTFS métro (faux GTFS au format standard) et de la carte star-metro-card côté données."""
import asyncio
import io
import sys
import unittest
import zipfile
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "stubs"))  # faux Home Assistant minimal
sys.path.insert(0, str(HERE.parent))  # racine du dépôt (custom_components)
sys.path.insert(0, str(HERE))  # imports entre fichiers de test

from test_star import FakeHass, FakeSession, make_db, ConfigEntry, DOMAIN  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")

LINE_A = ["J.F. Kennedy", "Villejean-Université", "République", "Gares", "La Poterie"]
LINE_B = ["Saint-Jacques - Gaîté", "Gares", "Sainte-Anne", "Cesson - Viasilva"]


def build_gtfs() -> bytes:
    stops = ["stop_id,stop_name,parent_station,location_type"]
    sid = {}
    for code, names in (("A", LINE_A), ("B", LINE_B)):
        for n, name in enumerate(names):
            parent = sid.setdefault(name, f"ST{len(sid)}")
    for name, parent in sid.items():
        stops.append(f'{parent},"{name}",,1')
        for code in "AB":
            for d in (0, 1):
                stops.append(f'{parent}_{code}{d},"{name}",{parent},0')
    stops.append('BUS1,"Arrêt bus",,0')

    routes = [
        "route_id,route_short_name,route_long_name,route_type,route_color",
        "7-1001,a,Ligne a,1,E2001A",
        "7-1002,b,Ligne b,1,00A651",
        "4-0001,C1,Chronostar 1,3,000000",
    ]
    trips = ["route_id,service_id,trip_id,trip_headsign,direction_id"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]

    def add_trip(tid, route, code, service, names, start_s, direction, step=120, dwell=20):
        headsign = names[-1]
        trips.append(f'{route},{service},{tid},"{headsign}",{direction}')
        t = start_s
        for seq, name in enumerate(names):
            arr, dep = t, t + dwell
            fmt = lambda s: f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
            times.append(f"{tid},{fmt(arr)},{fmt(dep)},{sid[name]}_{code}{direction},{seq + 1}")
            t = dep + step - dwell

    # Ligne a : départs toutes les 4 min de 08:00 à 08:40, dans les deux sens (semaine)
    for k in range(11):
        add_trip(f"A0_{k}", "7-1001", "A", "SEM", LINE_A, 8 * 3600 + k * 240, 0)
        add_trip(f"A1_{k}", "7-1001", "A", "SEM", LINE_A[::-1], 8 * 3600 + 120 + k * 240, 1)
    # Ligne b : course courte (Gares -> Cesson) + complète, semaine
    add_trip("B0_full", "7-1002", "B", "SEM", LINE_B, 8 * 3600, 0)
    add_trip("B0_short", "7-1002", "B", "SEM", LINE_B[1:], 8 * 3600 + 300, 0)
    # Course après minuit (service du samedi, 24:30 = dimanche 00:30)
    add_trip("A0_night", "7-1001", "A", "SAM", LINE_A, 24 * 3600 + 30 * 60, 0)
    # Course du bus : doit être ignorée
    trips.append('4-0001,SEM,BUS_T,"Bus",0')
    times.append("BUS_T,08:00:00,08:00:00,BUS1,1")

    calendar = [
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
        "SEM,1,1,1,1,1,0,0,20261001,20261231",
        "SAM,0,0,0,0,0,1,0,20261001,20261231",
    ]
    calendar_dates = [
        "service_id,date,exception_type",
        "SEM,20261111,2",  # 11 novembre férié : pas de service semaine
        "SEM,20261018,1",  # dimanche exceptionnel
    ]
    agency = ["agency_id,agency_name,agency_url,agency_timezone", "STAR,STAR,https://star.fr,Europe/Paris"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, rows in (("agency.txt", agency), ("routes.txt", routes), ("trips.txt", trips),
                           ("stops.txt", stops), ("stop_times.txt", times), ("calendar.txt", calendar),
                           ("calendar_dates.txt", calendar_dates)):
            zf.writestr(name, "﻿" + "\n".join(rows) + "\n")  # BOM comme certains exports
    return buf.getvalue()


GTFS = build_gtfs()


def at(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=PARIS)


class ParseTests(unittest.TestCase):
    def test_lines_and_station_order(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro

        s = parse_gtfs_metro(GTFS)
        self.assertEqual(sorted(s.lines), ["A", "B"])
        self.assertEqual(s.lines["A"].stations, LINE_A)
        self.assertEqual(s.lines["B"].stations, LINE_B)  # la course courte ne fixe pas l'ordre
        self.assertEqual(s.lines["A"].color, "#e2001a")
        self.assertEqual(s.lines["A"].name, "a")
        self.assertEqual(len([t for t in s.trips if t.line == "A"]), 23)
        self.assertFalse(any(t.headsign == "Bus" for t in s.trips))
        back = next(t for t in s.trips if t.headsign == "J.F. Kennedy")
        self.assertEqual(back.direction, -1)
        self.assertEqual([i for i, _, _ in back.stops], [4, 3, 2, 1, 0])
        short = next(t for t in s.trips if t.service_id == "SEM" and t.line == "B" and len(t.stops) == 3)
        self.assertEqual([i for i, _, _ in short.stops], [1, 2, 3])

    def test_calendar(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro

        s = parse_gtfs_metro(GTFS)
        self.assertEqual(s.services_on(date(2026, 10, 14)), {"SEM"})  # mercredi
        self.assertEqual(s.services_on(date(2026, 10, 17)), {"SAM"})  # samedi
        self.assertEqual(s.services_on(date(2026, 11, 11)), set())  # férié retiré
        self.assertEqual(s.services_on(date(2026, 10, 18)), {"SEM"})  # dimanche ajouté


class WindowTests(unittest.TestCase):
    def setUp(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro
        self.s = parse_gtfs_metro(GTFS)

    def test_running_and_positions(self):
        from custom_components.star_rennes_live.api.gtfs_metro import trains_window, train_position

        # Courses de 8 min 20 (arrivées toutes les 120 s, arrêt de 20 s)
        now = at(2026, 10, 14, 8, 9, 0)
        t0, trains, running = trains_window(self.s, now)
        self.assertEqual(t0, int(now.timestamp()))
        # Sens +1 parties à 08:04 et 08:08 ; sens -1 parties à 08:02 et 08:06 ; b : course courte de 08:05
        # + 1 rame en attente à La Poterie (arrivée 08:08, repart à 08:10:20)
        self.assertEqual(running, {"A": 5, "B": 1})
        down = sorted(p for t in trains if t["l"] == "A" and t["d"] == 1
                      if (p := train_position(t["s"], 0)) is not None)
        # 08:08 + 60 s : entre Kennedy (dép +20 s) et Villejean (arr +120 s) -> 0.4
        # 08:04 + 300 s : entre République (dép +260 s) et Gares (arr +360 s) -> 2.4
        self.assertEqual([round(p, 3) for p in down], [0.4, 2.4, 4.0])  # + rame à quai à La Poterie
        up = sorted(round(p, 3) for t in trains if t["l"] == "A" and t["d"] == -1
                    if (p := train_position(t["s"], 0)) is not None)
        self.assertEqual(up, [0.6, 2.6])  # 08:02 + 420 s : Villejean -> Kennedy ; 08:06 + 180 s : Gares -> République
        # La rame de 08:00, arrivée à 08:08, attend à La Poterie jusqu'à 08:10:19
        waiting = [t for t in trains if t["l"] == "A" and t["d"] == 1 and t["s"][-1][0] == 4 and t["s"][-1][2] == 79]
        self.assertEqual(len(waiting), 1)
        # Heures relatives à t0, fenêtre rognée [-90 s ; +600 s]
        for t in trains:
            self.assertGreaterEqual(t["s"][-1][2], -90)
            self.assertLessEqual(t["s"][0][1], 600)

    def test_night_trip_on_next_calendar_day(self):
        from custom_components.star_rennes_live.api.gtfs_metro import trains_window

        # Dimanche 00:33 : la course de 24:30 du samedi doit être en circulation
        t0, trains, running = trains_window(self.s, at(2026, 10, 18, 0, 33))
        self.assertEqual(running["A"], 1)
        # Dimanche 18 octobre 08:05 : service SEM ajouté exceptionnellement
        _, _, running = trains_window(self.s, at(2026, 10, 18, 8, 5))
        self.assertGreater(running["A"], 0)
        # Férié : rien
        _, trains, running = trains_window(self.s, at(2026, 11, 11, 8, 5))
        self.assertEqual((trains, running["A"]), ([], 0))

    def test_dst_change_day(self):
        from custom_components.star_rennes_live.api.gtfs_metro import trains_window, train_position

        # 25 octobre 2026 : passage à l'heure d'hiver, dimanche (SEM inactif) -> pas d'erreur
        trains_window(self.s, at(2026, 10, 25, 8, 5))
        # 26 octobre, lundi 08:04 heure d'hiver : la rame de 08:00 est à quai à République
        # (un décalage d'une heure la ferait disparaître ou la placerait ailleurs)
        _, trains, _ = trains_window(self.s, at(2026, 10, 26, 8, 4))
        positions = [train_position(t["s"], 0) for t in trains if t["l"] == "A" and t["d"] == 1]
        self.assertIn(2.0, positions)


class FakeGtfsResp:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def read(self):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class GtfsSession(FakeSession):
    def __init__(self, status=200, data=GTFS):
        super().__init__(make_db())
        self.gtfs_status, self.gtfs_data, self.gtfs_calls = status, data, 0

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith(".zip"):
            self.gtfs_calls += 1
            return FakeGtfsResp(self.gtfs_status, self.gtfs_data)
        return super().get(url, params=params, headers=headers, timeout=timeout)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        import homeassistant.util.dt as dt_util
        self.dt = dt_util

    def tearDown(self):
        self.dt._now[0] = None

    def run_setup(self, config, session):
        from custom_components.star_rennes_live import async_setup_entry
        from custom_components.star_rennes_live.sensor import async_setup_entry as sensor_setup

        hass = FakeHass(session)

        async def executor(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = executor
        entry = ConfigEntry(data=config)
        added = []

        async def go():
            await async_setup_entry(hass, entry)
            await asyncio.gather(*getattr(entry, "bg", []))
            await sensor_setup(hass, entry, lambda ents, *a: added.extend(ents))

        asyncio.run(go())
        return hass, entry, added

    def test_sensor_with_option(self):
        self.dt._now[0] = at(2026, 10, 14, 8, 9).astimezone(timezone.utc)
        session = GtfsSession()
        hass, entry, added = self.run_setup({"metro": True, "metro_lines": ["A"], "metro_rames": True}, session)
        sensor = next(e for e in added if type(e).__name__ == "StarMetroTrainsSensor")
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 6)
        a = sensor.extra_state_attributes
        self.assertEqual(a["lignes"]["A"]["stations"], LINE_A)
        self.assertEqual(a["lignes"]["B"]["couleur"], "#00a651")
        self.assertEqual(a["en_service"], {"A": 5, "B": 1})
        self.assertTrue(a["rames"])
        self.assertEqual(session.gtfs_calls, 1)
        self.assertIn("rames", type(sensor)._unrecorded_attributes)

    def test_no_option_no_download(self):
        session = GtfsSession()
        hass, entry, added = self.run_setup({"metro": True, "metro_lines": ["A"]}, session)
        self.assertEqual(session.gtfs_calls, 0)
        self.assertFalse(any(type(e).__name__ == "StarMetroTrainsSensor" for e in added))

    def test_download_failure_backoff_then_success(self):
        from custom_components.star_rennes_live.coordinator_metro_trains import MetroTrainsCoordinator
        from datetime import timedelta

        session = GtfsSession(status=503)
        hass = FakeHass(session)

        async def executor(fn, *args):
            return fn(*args)

        hass.async_add_executor_job = executor
        coord = MetroTrainsCoordinator(hass, ConfigEntry(), session)
        t = at(2026, 10, 14, 8, 9).astimezone(timezone.utc)
        self.dt._now[0] = t
        asyncio.run(coord.async_refresh())
        self.assertFalse(coord.last_update_success)
        self.assertEqual(session.gtfs_calls, 1)
        # Une minute plus tard : pas de nouveau téléchargement (attente de 30 min)
        self.dt._now[0] = t + timedelta(minutes=1)
        asyncio.run(coord.async_refresh())
        self.assertEqual(session.gtfs_calls, 1)
        # 31 minutes plus tard, le serveur répond
        session.gtfs_status = 200
        self.dt._now[0] = t + timedelta(minutes=31)
        asyncio.run(coord.async_refresh())
        self.assertTrue(coord.last_update_success)
        self.assertEqual(session.gtfs_calls, 2)
        # Pas de rechargement avant 20 h, puis rechargement la nuit suivante (≥ 3 h)
        self.dt._now[0] = t + timedelta(hours=5)
        asyncio.run(coord.async_refresh())
        self.assertEqual(session.gtfs_calls, 2)
        self.dt._now[0] = at(2026, 10, 15, 6, 0).astimezone(timezone.utc)
        asyncio.run(coord.async_refresh())
        self.assertEqual(session.gtfs_calls, 3)
        # Échec de mise à jour : on garde les horaires de la veille
        session.gtfs_status = 500
        self.dt._now[0] = at(2026, 10, 16, 6, 0).astimezone(timezone.utc)
        asyncio.run(coord.async_refresh())
        self.assertTrue(coord.last_update_success)

    def test_options_flow_metro_rames(self):
        from custom_components.star_rennes_live.options_flow import StarRennesOptionsFlow

        flow = StarRennesOptionsFlow(ConfigEntry(data={"metro_rames": True}))
        flow.hass = FakeHass(FakeSession(make_db()))
        self.assertTrue(flow.selected_types["metro"])
        asyncio.run(flow.async_step_init({"metro": True}))
        r = asyncio.run(flow.async_step_configure({"metro_lines": [], "metro_stations": [], "metro_rames": True}))
        self.assertTrue(r["data"]["metro_rames"])
        flow2 = StarRennesOptionsFlow(ConfigEntry(data={"metro_rames": True}))
        flow2.hass = FakeHass(FakeSession(make_db()))
        asyncio.run(flow2.async_step_init({"metro": False}))
        r = asyncio.run(flow2.async_step_configure({}))
        self.assertFalse(r["data"]["metro_rames"])


class LayoverTests(unittest.TestCase):
    def test_train_waits_at_terminus_fifo(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro, trains_window, train_position

        s = parse_gtfs_metro(GTFS)
        # Rame partie de La Poterie à 08:02 : arrivée à Kennedy 08:10:00 ; le
        # premier départ de Kennedy ensuite est celui de 08:12:00.
        now = at(2026, 10, 14, 8, 11, 0)
        _, trains, _ = trains_window(s, now)
        at_jfk = [t for t in trains if t["l"] == "A" and train_position(t["s"], 0) == 0.0]
        self.assertEqual(len(at_jfk), 1)
        self.assertEqual(at_jfk[0]["d"], -1)  # c'est la rame arrivée, en attente
        # 08:12:00 : toujours à quai ; 08:12:20 : elle repart (course suivante), sans doublon
        _, trains, _ = trains_window(s, at(2026, 10, 14, 8, 12, 0))
        at_jfk = [t for t in trains if t["l"] == "A" and train_position(t["s"], 0) == 0.0]
        self.assertEqual([t["d"] for t in at_jfk], [-1])
        _, trains, _ = trains_window(s, at(2026, 10, 14, 8, 12, 20))
        at_jfk = [t for t in trains if t["l"] == "A" and train_position(t["s"], 0) == 0.0]
        self.assertEqual([t["d"] for t in at_jfk], [1])
        # La dernière arrivée du soir n'est pas reliée (pas de départ dans les 30 min)
        last = max((t for t in s.trips if t.line == "A" and t.direction == -1), key=lambda t: t.stops[-1][1])
        self.assertLess(last.stops[-1][2] - last.stops[-1][1], 60)

    def test_block_id_takes_precedence(self):
        from custom_components.star_rennes_live.api.gtfs_metro import MetroTrip, link_terminus_layovers

        arrive = MetroTrip("A", "S", "J.F. Kennedy", -1, [(2, 0, 20), (1, 120, 140), (0, 240, 260)], "V1")
        other = MetroTrip("A", "S", "La Poterie", 1, [(0, 300, 320), (1, 420, 440)], "V2")
        mine = MetroTrip("A", "S", "La Poterie", 1, [(0, 600, 620), (1, 720, 740)], "V1")
        link_terminus_layovers([arrive, other, mine], {"A": 3})
        self.assertEqual(arrive.stops[-1], (0, 240, 619))  # attend « sa » course (V1), pas la première
        self.assertEqual(other.stops[0], (0, 300, 320))


class PassagesTests(unittest.TestCase):
    def test_next_departures_by_direction(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro, next_departures

        s = parse_gtfs_metro(GTFS)
        now = at(2026, 10, 14, 8, 9, 0)
        p = next_departures(s, now)
        rep = {x["direction"]: x for x in p["republique"]}
        # République (index 2) : sens +1 parties de Kennedy toutes les 4 min, passage à +260 s
        # -> 08:12:20 (départ 08:08) et 08:16:20 ; sens -1 : départ La Poterie 08:06 -> +260 s = 08:10:20
        self.assertEqual(set(rep), {"La Poterie", "J.F. Kennedy"})
        fmt = lambda e: datetime.fromtimestamp(e, PARIS).strftime("%H:%M:%S")
        self.assertEqual([fmt(t) for t in rep["La Poterie"]["departs"]], ["08:12:20", "08:16:20"])
        self.assertEqual([fmt(t) for t in rep["J.F. Kennedy"]["departs"]], ["08:10:20", "08:14:20"])
        # Terminus : pas d'arrivées listées, seulement les départs vers l'autre bout
        self.assertEqual([x["direction"] for x in p["jfkennedy"]], ["La Poterie"])
        # Station commune aux deux lignes (07:58 : des rames b passent encore)
        early = next_departures(s, at(2026, 10, 14, 7, 58))
        self.assertEqual(sorted(x["ligne"] for x in early["gares"]), ["A", "A", "B"])

    def test_station_sensor_attribute(self):
        session = GtfsSession()
        import homeassistant.util.dt as dt_util
        dt_util._now[0] = at(2026, 10, 14, 8, 9).astimezone(timezone.utc)
        try:
            db = session.db
            db["tco-metro-topologie-stations-td"].append({"id": "15006", "nom": "République", "codetechniquestationlignea": "REP"})
            # Pas de temps réel dans ce faux réseau : passages GTFS (schéma activé)
            hass, entry, added = CoordinatorTests.run_setup(
                self, {"metro": True, "metro_stations": ["15006"], "metro_rames": True}, session)
        finally:
            dt_util._now[0] = None
        station = next(e for e in added if e._attr_name == "Station République")
        passages = station.extra_state_attributes["prochains_passages"]
        self.assertEqual({p["direction"] for p in passages}, {"La Poterie", "J.F. Kennedy"})
        self.assertTrue(passages[0]["departs"][0].startswith("2026-10-14T"))
        self.assertFalse(passages[0]["temps_reel"])
        self.assertEqual(session.gtfs_calls, 1)


class CodeTests(unittest.TestCase):
    def test_station_codes(self):
        from custom_components.star_rennes_live.api.gtfs_metro import station_code, codes_by_name

        topo = {"15010": {"name": "Clemenceau", "code": "GCL"}, "15012": {"name": "Italie", "code": "ITA"},
                "15064": {"name": "Saint-Jacques - Gaîté", "code": "SJG"}}
        known = codes_by_name(topo)
        self.assertEqual(station_code("Italie", known), "ITA")
        self.assertEqual(station_code("Clemenceau", known), "GCL")  # code officiel, pas « CLE »
        self.assertEqual(station_code("Saint-Jacques – Gaîté", known), "SJG")  # tiret différent
        self.assertEqual(station_code("République", known), "REP")  # repli : 3 lettres sans accent
        self.assertEqual(station_code("J.F. Kennedy", {}), "JFK")


if __name__ == "__main__":
    unittest.main(verbosity=2)

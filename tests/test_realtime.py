"""Temps réel métro (deux prochains passages) : parsing, rames reconstituées, secours GTFS."""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone

from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "stubs"))  # faux Home Assistant minimal
sys.path.insert(0, str(HERE.parent))  # racine du dépôt (custom_components)
sys.path.insert(0, str(HERE))  # imports entre fichiers de test

from test_gtfs import GTFS, GtfsSession, LINE_A, PARIS, at, CoordinatorTests  # noqa: E402
from test_star import FakeHass, ConfigEntry  # noqa: E402

NOW = at(2026, 10, 14, 20, 40, 0)
DATASET = "tco-metro-circulation-deux-prochains-passages-tr"


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def rec(station, destination, first, second, line="1001", extraction=NOW):
    """first/second : (arrivée, départ) en secondes après NOW."""
    r = {"idligne": line, "nomarret": station, "destination": destination,
         "heureextraction": extraction.strftime("%H:%M:%S")}
    for rank, (a, d) in (("first", first), ("second", second)):
        r[f"arrivee{rank}train"] = fmt(NOW + timedelta(seconds=a))
        r[f"depart{rank}train"] = fmt(NOW + timedelta(seconds=d))
    return r


# Ligne a du faux GTFS : Kennedy(0) Villejean(1) République(2) Gares(3) La Poterie(4)
# Sens vers La Poterie : rame X entre Villejean et République (arrive à République dans 40 s),
# rame Y attendue à Kennedy (départ +100 s), rame Z derrière.
RECORDS = [
    rec("J.F. Kennedy", "La Poterie", (80, 100), (320, 340)),
    rec("Villejean-Université", "La Poterie", (200, 220), (440, 460)),
    rec("République", "La Poterie", (40, 60), (320, 340)),
    rec("Gares", "La Poterie", (160, 180), (440, 460)),
    # Sens vers Kennedy : une rame à quai à Gares (arrivée -10 s, départ +10 s)
    rec("La Poterie", "J.F. Kennedy", (200, 220), (500, 520)),
    rec("Gares", "J.F. Kennedy", (-10, 10), (320, 340)),
    rec("République", "J.F. Kennedy", (110, 130), (430, 450)),
    rec("Villejean-Université", "J.F. Kennedy", (230, 250), (550, 570)),
]


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro
        self.schedule = parse_gtfs_metro(GTFS)

    def test_parse_sample_from_star(self):
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, passages_by_station

        sample = [{"idligne": "1001", "nomcourtligne": "a", "sens": 1, "destination": "J.F. Kennedy",
                   "nomarret": "Italie", "arriveefirsttrain": "2026-10-15 20:41:41", "departfirsttrain": "2026-10-15 20:41:55",
                   "arriveesecondtrain": "2026-10-15 20:46:47", "departsecondtrain": "2026-10-15 20:47:01",
                   "heureextraction": "20:38:02"}]
        p = parse_passages(sample)
        self.assertEqual(p[0].line, "A")
        self.assertEqual(datetime.fromtimestamp(p[0].times[0][1], PARIS).strftime("%H:%M:%S"), "20:41:55")
        by = passages_by_station(p, datetime(2026, 10, 15, 20, 38, 2, tzinfo=PARIS))
        self.assertEqual(by["italie"][0]["direction"], "J.F. Kennedy")
        self.assertEqual(len(by["italie"][0]["departs"]), 2)
        self.assertTrue(by["italie"][0]["temps_reel"])

    def test_freshness(self):
        from custom_components.star_rennes_live.api.metro_realtime import is_fresh

        self.assertTrue(is_fresh(RECORDS, NOW + timedelta(seconds=40)))
        self.assertFalse(is_fresh(RECORDS, NOW + timedelta(minutes=10)))
        # Extraction 23:59:50, lecture 00:00:20 : fraîche
        late = [rec("Gares", "La Poterie", (0, 1), (2, 3), extraction=at(2026, 10, 14, 23, 59, 50))]
        self.assertTrue(is_fresh(late, at(2026, 10, 15, 0, 0, 20)))

    def test_rebuilt_trains(self):
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, build_realtime_trains
        from custom_components.star_rennes_live.api.gtfs_metro import train_position

        trains = build_realtime_trains(parse_passages(RECORDS), self.schedule, NOW)["A"]
        t0 = NOW.timestamp()
        rel = lambda t: [[i, a - t0, d - t0] for i, a, d in t["s"]]
        now_pos = sorted((t["d"], round(p, 2)) for t in trains if (p := train_position(rel(t), 0)) is not None)
        # X : entre Villejean (dép. estimé -60 s) et République (arr. +40 s) -> 1.6 ; rame à quai à Gares (sens -1) -> 3.0
        self.assertEqual(now_pos, [(-1, 3.0), (1, 1.6)])
        x = next(t for t in trains if t["d"] == 1 and rel(t)[0][0] == 1)
        # X continue vers Gares puis arrive au terminus La Poterie (pas de fiche d'arrivée)
        self.assertEqual([s[0] for s in x["s"]], [1, 2, 3, 4])
        # Y (départ de Kennedy +100 s) passe ensuite à Villejean +200 s et République +320 s
        y = next(t for t in trains if t["d"] == 1 and rel(t)[0][0] == 0 and rel(t)[0][1] == 80)
        self.assertEqual([[s[0], round(s[1])] for s in rel(y)][:3], [[0, 80], [1, 200], [2, 320]])
        # Dans 50 s, X est à quai à République
        self.assertEqual(train_position(rel(x), 50), 2.0)


class SameSegmentTests(unittest.TestCase):
    """Deux rames entre Villejean (1) et République (2), une troisième à quai à Villejean."""

    def test_two_trains_in_segment(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro, train_position
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, build_realtime_trains

        schedule = parse_gtfs_metro(GTFS)
        records = [
            rec("J.F. Kennedy", "La Poterie", (150, 170), (400, 420)),
            # rame à quai à Villejean jusqu'à +60 s (au moins une minute derrière la suivante)
            rec("Villejean-Université", "La Poterie", (-10, 60), (270, 290)),
            rec("République", "La Poterie", (30, 50), (85, 105)),  # deux rames dans le tronçon
            rec("Gares", "La Poterie", (150, 170), (205, 225)),
        ]
        trains = build_realtime_trains(parse_passages(records), schedule, NOW)["A"]
        t0 = NOW.timestamp()
        rel = lambda t: [[i, a - t0, d - t0] for i, a, d in t["s"]]
        pos = sorted(round(p, 2) for t in trains if t["d"] == 1 if (p := train_position(rel(t), 0)) is not None)
        # Rame 1 : arrive à République dans 30 s -> 1.7 ; rame 2 : dans 85 s -> départ estimé
        # -15 s (85 - 100) -> 1.15 ; rame 3 : à quai à Villejean -> 1.0
        self.assertEqual(pos, [1.0, 1.15, 1.7])
        # La rame à quai à Villejean (sans créneau à République) continue sur une arrivée estimée
        dwelling = next(t for t in trains if t["d"] == 1 and any(s[0] == 1 and round(s[1] - t0) == -10 for s in t["s"]))
        self.assertEqual(rel(dwelling)[-1][0], 2)
        self.assertEqual(train_position(rel(dwelling), 110), 1.5)  # partie à +60 s, arrivée estimée +160 s

    def test_held_train_stays_visible(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro, train_position
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, build_realtime_trains

        schedule = parse_gtfs_metro(GTFS)
        # Rame retenue dans le tunnel : arrivée à République dans 400 s au lieu de ~100
        records = [
            rec("Villejean-Université", "La Poterie", (500, 520), (800, 820)),
            rec("République", "La Poterie", (400, 420), (700, 720)),
        ]
        trains = build_realtime_trains(parse_passages(records), schedule, NOW)["A"]
        t0 = NOW.timestamp()
        rel = lambda t: [[i, a - t0, d - t0] for i, a, d in t["s"]]
        visible = [p for t in trains if (p := train_position(rel(t), 0)) is not None]
        self.assertEqual(len(visible), 1)
        self.assertTrue(1.0 <= visible[0] < 2.0)


class GapTests(unittest.TestCase):
    def test_missing_station_sums_real_segments(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro
        from custom_components.star_rennes_live.api.metro_realtime import _segment_time

        schedule = parse_gtfs_metro(GTFS)
        schedule.segment_times[("A", 1, 2)] = 150.0
        self.assertEqual(_segment_time(schedule, "A", 1, 2), 150.0)
        # Kennedy (0) -> République (2) sans fiche à Villejean : 100 + 150
        self.assertEqual(_segment_time(schedule, "A", 0, 2), 250.0)
        self.assertEqual(_segment_time(schedule, "A", 2, 0), 250.0)


class RealtimeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        import homeassistant.util.dt as dt_util
        self.dt = dt_util
        dt_util._now[0] = NOW.astimezone(timezone.utc)

    def tearDown(self):
        self.dt._now[0] = None

    def session(self, records=RECORDS, status=200):
        s = GtfsSession()
        s.db[DATASET] = records
        s.db["tco-metro-topologie-stations-td"].append({"id": "15006", "nom": "République", "code": "REP"})
        return s

    def test_station_passages_realtime_without_gtfs(self):
        s = self.session()
        hass, entry, added = CoordinatorTests.run_setup(self, {"metro": True, "metro_stations": ["15006"]}, s)
        self.assertEqual(s.gtfs_calls, 0)  # pas de GTFS sans le schéma
        station = next(e for e in added if e._attr_name == "Station République")
        passages = {p["direction"]: p for p in station.extra_state_attributes["prochains_passages"]}
        self.assertTrue(passages["La Poterie"]["temps_reel"])
        self.assertEqual(passages["La Poterie"]["departs"][0], (NOW + timedelta(seconds=60)).astimezone(timezone.utc).isoformat())

    def test_schema_uses_realtime_then_falls_back(self):
        s = self.session()
        hass, entry, added = CoordinatorTests.run_setup(self, {"metro": True, "metro_rames": True}, s)
        sensor = next(e for e in added if type(e).__name__ == "StarMetroTrainsSensor")
        a = sensor.extra_state_attributes
        self.assertEqual(a["source"], "temps_reel")
        self.assertEqual(a["lignes"]["A"]["source"], "temps_reel")
        self.assertEqual(a["lignes"]["B"]["source"], "horaires_theoriques")  # pas de fiche b dans l'exemple
        self.assertEqual(a["lignes"]["A"]["codes"][2], "REP")
        self.assertEqual(a["en_service"]["A"], 2)
        # Temps réel périmé : retour aux horaires théoriques pour la ligne a
        coord = hass.data["star_rennes_live"][entry.entry_id]["metro_rames"]
        self.dt._now[0] = (NOW + timedelta(minutes=12)).astimezone(timezone.utc)
        asyncio.run(coord.async_refresh())
        self.assertEqual(sensor.extra_state_attributes["source"], "horaires_theoriques")
        self.assertTrue(coord.last_update_success)

    def test_realtime_down_and_no_gtfs_fails(self):
        s = self.session(records=[])
        hass, entry, added = CoordinatorTests.run_setup(self, {"metro": True, "metro_stations": ["15006"]}, s)
        coord = hass.data["star_rennes_live"][entry.entry_id]["metro_rames"]
        self.assertFalse(coord.last_update_success)
        station = next(e for e in added if e._attr_name == "Station République")
        self.assertEqual(station.extra_state_attributes["prochains_passages"], [])


class NoDuplicateTests(unittest.TestCase):
    """GTFS qui surestime les parcours (minute arrondie + arrêt) : pas de rames en double."""

    def simulate(self, run, dwell, headway, gtfs_seg, offset):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro, train_position
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, build_realtime_trains

        schedule = parse_gtfs_metro(GTFS)
        for i in range(4):
            schedule.segment_times[("A", i, i + 1)] = gtfs_seg
        names = ["J.F. Kennedy", "Villejean-Université", "République", "Gares", "La Poterie"]
        now = offset
        # Vérité : rames parties de Kennedy toutes les headway secondes
        truth = []
        for k in range(-10, 10):
            start = k * headway
            stops = [(i, start + i * (run + dwell) - (dwell if i else 0), start + i * (run + dwell)) for i in range(5)]
            truth.append(stops)
        records = []
        for i, name in enumerate(names[:-1]):
            nxt = [(a, d) for st in truth for (j, a, d) in st if j == i and d >= now][:2]
            r = {"idligne": "1001", "nomarret": name, "destination": "La Poterie", "heureextraction": NOW.strftime("%H:%M:%S")}
            for rank, (a, d) in zip(("first", "second"), nxt):
                r[f"arrivee{rank}train"] = fmt(NOW + timedelta(seconds=a - now))
                r[f"depart{rank}train"] = fmt(NOW + timedelta(seconds=d - now))
            records.append(r)
        expected = sum(1 for st in truth if st[0][2] <= now <= st[-1][1])
        trains = build_realtime_trains(parse_passages(records), schedule, NOW)["A"]
        t0 = NOW.timestamp()
        rel = lambda t: [[i, a - t0, d - t0] for i, a, d in t["s"]]
        visible = sorted(round(p, 2) for t in trains if (p := train_position(rel(t), 0)) is not None)
        return expected, visible

    def test_no_duplicates_when_gtfs_overestimates(self):
        for offset in range(0, 240, 15):
            expected, visible = self.simulate(run=55, dwell=20, headway=240, gtfs_seg=120, offset=offset)
            gaps = [b - a for a, b in zip(visible, visible[1:])]
            self.assertTrue(all(g > 0.5 for g in gaps), (offset, visible))
            self.assertLessEqual(len(visible), expected + 1, (offset, visible))


class RealLineBTests(unittest.TestCase):
    """Extraction réelle STAR, ligne b, jeudi 15/10/2026 23:17:48 (18 courses identifiées)."""

    def build(self):
        import json
        from zoneinfo import ZoneInfo
        from custom_components.star_rennes_live.api.gtfs_metro import MetroSchedule, MetroLine
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, build_realtime_trains

        recs = json.load(open(HERE / "fixture_ligne_b.json", encoding="utf-8"))["results"]
        st = ["Cesson - Viasilva", "Atalante", "Beaulieu - Université", "Joliot-Curie - Chateaubriand",
              "Les Gayeulles", "Gros-Chêne", "Jules Ferry", "Sainte-Anne", "Saint-Germain", "Gares",
              "Colombier", "Mabilais", "Cleunay", "La Courrouze", "Saint-Jacques - Gaîté"]
        sch = MetroSchedule("Europe/Paris", {"B": MetroLine("B", "b", None, st)}, [])
        for i in range(14):
            sch.segment_times[("B", i, i + 1)] = 120  # GTFS volontairement surestimé
        now = datetime(2026, 10, 15, 23, 17, 48, tzinfo=ZoneInfo("Europe/Paris"))
        return build_realtime_trains(parse_passages(recs), sch, now)["B"], now.timestamp()

    def visible(self, trains, t0, dt):
        from custom_components.star_rennes_live.api.gtfs_metro import train_position
        return [(t["d"], p) for t in trains
                if (p := train_position([[i, a - t0, d - t0] for i, a, d in t["s"]], dt)) is not None]

    def test_count_matches_star_train_ids(self):
        trains, t0 = self.build()
        self.assertEqual(len(self.visible(trains, t0, 0)), 18)

    def test_affluence_per_departure(self):
        import json
        from zoneinfo import ZoneInfo
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages, passages_by_station

        recs = json.load(open(HERE / "fixture_ligne_b.json", encoding="utf-8"))["results"]
        now = datetime(2026, 10, 15, 23, 17, 48, tzinfo=ZoneInfo("Europe/Paris"))
        by = passages_by_station(parse_passages(recs), now)
        sta = {p["direction"]: p for p in by["sainte anne"]} if "sainte anne" in by else {p["direction"]: p for p in next(v for k, v in by.items() if "anne" in k)}
        self.assertEqual(sta["Cesson - Viasilva"]["affluence"], ["Vert Foncé", "Vert Foncé"])
        self.assertEqual(sta["Saint-Jacques - Gaîté"]["affluence"], ["Vert Clair", "Vert Clair"])
        ferry = next(v for k, v in by.items() if "ferry" in k)
        self.assertEqual(ferry[0]["affluence"], [None, None])  # « NA » = non mesuré
        self.assertEqual(len(ferry[0]["affluence"]), len(ferry[0]["departs"]))

    def test_turnback_not_drawn_twice(self):
        trains, t0 = self.build()
        # Après l'arrivée publiée à Gaîté (23:19:18), la rame entrante a disparu
        for dt in (105, 120, 135):
            at_gaite = [v for v in self.visible(trains, t0, dt) if v[1] >= 13.5]
            self.assertLessEqual(len(at_gaite), 1, (dt, at_gaite))


class NightClosureTests(unittest.TestCase):
    """Fermetures de station : alerte seulement pendant l'exploitation."""

    def test_station_served_from_gtfs(self):
        from custom_components.star_rennes_live.api.gtfs_metro import parse_gtfs_metro, station_served, _service_day_start
        from zoneinfo import ZoneInfo

        schedule = parse_gtfs_metro(GTFS)
        day = NOW.astimezone(PARIS).date()
        services = schedule.services_on(day)
        trips = [t for t in schedule.trips if t.line == "A" and t.service_id in services]
        base = _service_day_start(day, ZoneInfo(schedule.timezone))
        last = max(base + t.stops[-1][2] for t in trips)
        first = min(base + t.stops[0][1] for t in trips)
        name = schedule.lines["A"].stations[0]
        mid = datetime.fromtimestamp((first + last) / 2, PARIS)
        # Bien après la dernière rame et bien avant la première : pas desservie
        self.assertFalse(station_served(schedule, datetime.fromtimestamp(last + 3600, PARIS), "A", name))
        self.assertFalse(station_served(schedule, datetime.fromtimestamp(first - 3600, PARIS), "A", name))
        # Juste avant un passage prévu : desservie
        dep = next(base + d for t in trips for (i, a, d) in t.stops if i == 0)
        self.assertTrue(station_served(schedule, datetime.fromtimestamp(dep - 300, PARIS), "A", name))
        self.assertIsNone(station_served(schedule, mid, "A", "Station inconnue"))

    def test_service_hours_fallback(self):
        from custom_components.star_rennes_live.api.metro_api import within_service_hours

        self.assertTrue(within_service_hours(datetime(2026, 10, 15, 12, 0, tzinfo=PARIS)))   # jeudi midi
        self.assertFalse(within_service_hours(datetime(2026, 10, 15, 3, 0, tzinfo=PARIS)))   # jeudi 3 h
        self.assertFalse(within_service_hours(datetime(2026, 10, 15, 1, 30, tzinfo=PARIS)))  # nuit mer.-jeu.
        self.assertTrue(within_service_hours(datetime(2026, 10, 16, 1, 30, tzinfo=PARIS)))   # nuit jeu.-ven.
        self.assertTrue(within_service_hours(datetime(2026, 10, 15, 5, 5, tzinfo=PARIS)))

    def test_drop_off_service_closures(self):
        from custom_components.star_rennes_live.api.metro_api import drop_off_service_closures

        items = [
            {"type": "ligne", "ligne": "B", "lignes": ["B"], "titre": "Ligne b · trafic interrompu"},
            {"type": "station", "station": "Colombier", "lignes": ["B"], "titre": "Station Colombier fermée"},
            {"type": "station", "station": "Gares", "lignes": ["A", "B"], "titre": "Station Gares fermée"},
        ]
        served = lambda line, station: line == "A"
        kept = drop_off_service_closures(items, served)
        self.assertEqual([k["titre"] for k in kept], ["Ligne b · trafic interrompu", "Station Gares fermée"])
        self.assertEqual(kept[1]["lignes"], ["A"])
        self.assertEqual(kept[1]["detail"], "Ligne a · les rames ne s'y arrêtent pas")
        self.assertEqual(drop_off_service_closures(items, lambda l, s: False), items[:1])


class RealLineATests(unittest.TestCase):
    """Extraction réelle STAR, ligne a, samedi 17/10/2026 15:19:54.

    République fermée (manifestation) : ses deux fiches restent figées à
    13:42. Rame toutes les ~1 min 40, tronçons courts (Clemenceau → Jacques
    Cartier en 44 s) et n° de rame qui change d'une station à l'autre.
    """

    STATIONS = ["La Poterie", "Le Blosne", "Triangle", "Italie", "Henri Fréville", "Clemenceau",
                "Jacques Cartier", "Gares", "Charles de Gaulle", "République", "Sainte-Anne",
                "Anatole France", "Pontchaillou", "Villejean-Université", "J.F. Kennedy"]

    def setUp(self):
        import json
        from zoneinfo import ZoneInfo
        from custom_components.star_rennes_live.api.gtfs_metro import MetroSchedule, MetroLine
        from custom_components.star_rennes_live.api.metro_realtime import parse_passages

        self.now = datetime(2026, 10, 17, 15, 19, 54, tzinfo=ZoneInfo("Europe/Paris"))
        recs = json.load(open(HERE / "fixture_ligne_a.json", encoding="utf-8"))["results"]
        self.passages = parse_passages(recs, now=self.now)
        self.schedule = MetroSchedule("Europe/Paris", {"A": MetroLine("A", "a", None, self.STATIONS)}, [])
        for i in range(14):
            self.schedule.segment_times[("A", i, i + 1)] = 120  # GTFS volontairement surestimé

    def test_frozen_station_ignored(self):
        from custom_components.star_rennes_live.api.metro_realtime import passages_by_station, is_fresh
        import json

        self.assertFalse(any(p.station == "République" for p in self.passages))
        self.assertNotIn("republique", passages_by_station(self.passages, self.now))
        recs = json.load(open(HERE / "fixture_ligne_a.json", encoding="utf-8"))["results"]
        self.assertTrue(is_fresh(recs, self.now))  # une fiche figée ne disqualifie pas tout le jeu

    def test_no_phantom_trains(self):
        from custom_components.star_rennes_live.api.gtfs_metro import train_position
        from custom_components.star_rennes_live.api.metro_realtime import build_realtime_trains

        trains = build_realtime_trains(self.passages, self.schedule, self.now)["A"]
        t0 = self.now.timestamp()
        counts = set()
        for dt in (0, 30, 60, 90):
            vis = sorted((t["d"], p) for t in trains
                         if (p := train_position([[i, a - t0, d - t0] for i, a, d in t["s"]], dt)) is not None)
            counts.add(len(vis))
            close = [(a, b) for a, b in zip(vis, vis[1:]) if a[0] == b[0] and b[1] - a[1] < 0.4]
            self.assertEqual(close, [], dt)
        self.assertEqual(counts, {19})  # stable, sans les 2 fantômes de République ni les doublons


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""COROS (apps/trainer/coros.py, fit.py): connect with OAuth (discovery, registration, PKCE); from then on new
activities come from COROS into the log with their speed analysis, and Strava stops adding new ones. Run against a
fake COROS, with FIT files written here to the FIT protocol."""

import base64
import hashlib
import json
import struct
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from apps.trainer import coros, fit, strava
from database import Database

MCP = "https://mcpus.coros.com/mcp"
AS = "https://mcpus.coros.com"
EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


def fit_file(start, seconds=1800, rep_every=None, sport=1, offset=-7 * 3600, burst=30, flat_speed=False):
    """A FIT file: one record a second (time, position, distance, speed, heart rate), then session and activity.
    Easy running at 3 m/s; with rep_every, 30 seconds at 6 m/s every rep_every seconds."""
    t0 = int((start - EPOCH).total_seconds())
    body = bytearray()

    def define(local, number, fields):
        body.extend(struct.pack("<BBBHB", 0x40 | local, 0, 0, number, len(fields)))
        for num, size, base in fields:
            body.extend(struct.pack("<BBB", num, size, base))

    define(0, 20, [(253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85), (5, 4, 0x86), (73, 4, 0x86), (3, 1, 0x02)])
    lat, lon, d = int(33.35 / fit.SEMICIRCLE), int(-111.79 / fit.SEMICIRCLE), 0.0
    for s in range(seconds):
        v = 6.0 if rep_every and s % rep_every < burst and s > 60 else 3.0
        d += v
        shown = 3.0 if flat_speed else v   # flat_speed: the watch's own speed field smoothed flat
        body.extend(struct.pack("<BIiiIIB", 0, t0 + s, lat + s * 300, lon, int(d * 100), int(shown * 1000), 140))
    define(1, 18, [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00), (7, 4, 0x86), (8, 4, 0x86), (9, 4, 0x86),
                   (16, 1, 0x02), (17, 1, 0x02)])
    body.extend(struct.pack("<BIIBIIIBB", 1, t0 + seconds, t0, sport, seconds * 1000, seconds * 1000,
                            int(d * 100), 140, 165))
    define(2, 34, [(253, 4, 0x86), (5, 4, 0x86)])
    body.extend(struct.pack("<BII", 2, t0 + seconds, t0 + seconds + offset))
    header = struct.pack("<BBHI4s", 12, 0x20, 2100, len(body), b".FIT")
    return header + bytes(body) + b"\x00\x00"


class FakeCoros:
    def __init__(self):
        self.activities, self.files, self.registered, self.codes, self.expired = [], {}, [], {}, False

    def __call__(self, url, method="GET", form=None, body=None, headers=None, raw=False):
        if url.endswith("/.well-known/oauth-protected-resource"):
            return 200, {}, json.dumps({"resource": AS, "authorization_servers": [AS]})
        if url == AS + "/.well-known/oauth-authorization-server":
            return 200, {}, json.dumps({"authorization_endpoint": AS + "/oauth2/authorize",
                                        "token_endpoint": AS + "/oauth2/token",
                                        "registration_endpoint": AS + "/connect/register"})
        if url == AS + "/connect/register":
            self.registered.append(body)
            return 201, {}, json.dumps({"client_id": "dieter-client"})
        if url == AS + "/oauth2/token":
            if form["grant_type"] == "authorization_code":
                challenge = self.codes.pop(form["code"])
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
                assert digest.decode() == challenge, "PKCE verifier doesn't match"
                return 200, {}, json.dumps({"access_token": "a1", "refresh_token": "r1", "expires_in": 3600})
            self.expired = False
            return 200, {}, json.dumps({"access_token": "a2", "refresh_token": "r1", "expires_in": 3600})
        assert url == MCP, url
        if self.expired:
            return 401, {}, "expired"
        if body["method"] == "initialize":
            return 200, {"Mcp-Session-Id": "s1"}, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body["method"] == "notifications/initialized":
            return 202, {}, ""
        assert headers.get("Mcp-Session-Id") == "s1"
        name, args = body["params"]["name"], body["params"]["arguments"]
        if name == "querySportRecords":
            rows = [a for a in self.activities if args["startDate"] <= str(a["date"]) <= args["endDate"]]
            return 200, {}, "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": json.dumps({"records": rows})}]}}) + "\n\n"
        if name == "downloadActivityFitFiles":
            return 200, {}, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"content": [
                {"type": "resource", "resource": {"uri": f"file://{args['labelId']}.fit",
                                                  "blob": base64.b64encode(self.files[args["labelId"]]).decode()}}]}})
        raise AssertionError(name)


class MemoryBlobs:
    def __init__(self):
        self.files = {}

    def put(self, key, data):
        self.files[key] = data


class FitReaderTests(unittest.TestCase):
    def test_reads_records_session_and_local_time(self):
        start = datetime(2026, 11, 28, 15, 10, tzinfo=timezone.utc)
        run = fit.read(fit_file(start, seconds=600))
        self.assertEqual(run["start"], start)
        self.assertEqual(run["local_offset"], -7 * 3600)
        self.assertEqual((run["sport"], run["avg_hr"], run["max_hr"], run["moving_s"]), (1, 140, 165, 600))
        self.assertEqual(len(run["records"]), 600)
        self.assertAlmostEqual(run["records"][0]["lat"], 33.35, places=4)
        self.assertAlmostEqual(run["distance_m"], 1800, delta=1)
        s = fit.streams(run)
        self.assertEqual(s["time"]["data"][:3], [0, 1, 2])
        self.assertEqual(len(s["latlng"]["data"]), 600)

    def test_strides_come_from_distance_not_the_smoothed_speed(self):
        # 10-second strides the watch's speed field smooths away still show in the distance it records.
        from apps.trainer import speed
        run = fit.read(fit_file(datetime(2026, 11, 28, 15, 10, tzinfo=timezone.utc), seconds=900, rep_every=150,
                                burst=10, flat_speed=True))
        series = speed.unpack(speed.pack(fit.streams(run)))
        self.assertAlmostEqual(max(v for v in series["v"] if v), 6.0, delta=0.3)
        self.assertGreaterEqual(speed.summarize(series)["strides"], 4)

    def test_not_a_fit_file(self):
        with self.assertRaises(fit.FitError):
            fit.read(b"hello, this is not a fit file")


class CorosTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(str(Path(tempfile.mkdtemp()) / "t.db"))
        self.db.init()
        self.user = self.db.create_user("runner@example.com", "Runner", "x", "member")
        self.fake, self.blobs = FakeCoros(), MemoryBlobs()
        self.c = coros.Coros(self.db, self.blobs, call=self.fake, mcp_url=MCP)

    def connect(self):
        address = self.c.start(self.user, "https://askdieter.ai/apps/trainer/coros/callback")
        query = parse_qs(urlsplit(address).query)
        self.assertTrue(address.startswith(AS + "/oauth2/authorize?"))
        self.assertEqual((query["code_challenge_method"], query["scope"]), (["S256"], [coros.SCOPES]))
        self.fake.codes["code-1"] = query["code_challenge"][0]
        self.c.finish(self.user, query["state"][0], "code-1")
        self.assertEqual(self.fake.registered[0]["token_endpoint_auth_method"], "none")

    def add(self, label, start, sport_type=100, **kw):
        local = start + timedelta(hours=-7)
        self.fake.activities.append({"labelId": label, "sportType": sport_type, "date": int(local.strftime("%Y%m%d"))})
        self.fake.files[label] = fit_file(start, **kw)

    def test_new_runs_come_from_coros_into_the_log(self):
        self.connect()
        since = datetime.fromisoformat(self.c.since(self.user)).replace(tzinfo=timezone.utc)
        self.add("OLD", since - timedelta(hours=3))                       # before the switch: Strava has it
        self.add("NEW", since + timedelta(minutes=5), rep_every=120)      # after: comes from COROS
        tomorrow = (since + timedelta(days=1)).date()
        self.assertEqual(self.c.bring_in(self.user, today=tomorrow), 1)
        rows = self.db.connect().execute("SELECT * FROM trainer_imported_workouts WHERE source = 'coros'").fetchall()
        self.assertEqual([(r["external_id"], r["activity_type"], r["workout_category"]) for r in rows],
                         [("NEW", "Run", "run")])
        self.assertTrue(rows[0]["started_at_local"])
        kept = self.db.connect().execute("SELECT * FROM tr_run_speed WHERE external_id = 'NEW'").fetchone()
        self.assertTrue(kept["series"])
        self.assertGreater(len(json.loads(kept["efforts_json"])), 3)       # the 6 m/s bursts were found
        self.assertIn(f"coros/{self.user}/NEW.fit", self.blobs.files)
        self.assertEqual(self.c.bring_in(self.user, today=tomorrow), 0)    # each is read once
        self.assertEqual(self.c.status(self.user)["added"], 1)

    def test_strava_stops_adding_new_activities(self):
        sync = strava.Sync(self.db)
        sync._conn()
        self.c._conn()
        self.assertFalse(sync._coros_took_over(self.user, "12345"))
        self.connect()
        self.assertTrue(sync._coros_took_over(self.user, "12345"))
        self.c.disconnect(self.user)
        self.assertFalse(sync._coros_took_over(self.user, "12345"))
        self.assertEqual(self.c.since(self.user), "")

    def test_latest_run_compared_with_strava(self):
        self.connect()
        start = datetime(2026, 11, 28, 15, 10, tzinfo=timezone.utc)
        self.add("TODAY", start, seconds=1200, rep_every=200)
        local = (start - timedelta(hours=7)).replace(tzinfo=None)
        self.db.add_trainer_imported_workout(external_id="999", activity_type="Run", workout_category="run",
                                             title="Morning Run", started_at=local.date().isoformat(),
                                             distance_meters=4400, moving_time_seconds=1200, average_heartrate=139,
                                             max_heartrate=166, user_id=self.user)
        strava.Sync(self.db)._conn().execute("UPDATE trainer_imported_workouts SET started_at_local = ? WHERE "
                                             "external_id = '999'", (local.isoformat(),))
        result = self.c.test_latest(self.user, today=date(2026, 11, 28))
        self.assertEqual(result["strava_id"], "999")
        rows = {label: (c, s) for label, c, s in result["rows"]}
        self.assertEqual(rows["Distance"][1], "2.73 mi")
        self.assertIn("140 avg", rows["Heart rate"][0])
        self.assertNotEqual(rows["What was fast"][0], "nothing")
        self.assertEqual(self.c.last_test(self.user)["label_id"], "TODAY")
        self.assertEqual(self.c.added(self.user), 0)                       # a test doesn't add it to the log

    def test_an_expired_sign_in_is_refreshed(self):
        self.connect()
        self.fake.expired = True
        self.c.list_window(self.user, date(2026, 11, 1), date(2026, 11, 28))
        self.assertEqual(self.c.connected(self.user)["access_token"], "a2")

    def test_a_stale_state_is_refused(self):
        self.c.start(self.user, "https://askdieter.ai/cb")
        with self.assertRaises(coros.CorosError):
            self.c.finish(self.user, "not-the-state", "code")

    def test_text_answers_are_read_too(self):
        text = ("1. Morning Run - 2026-11-28 08:10, 7.0 mi (labelId: 474723165319233737, sportType: 100)\n"
                "2. Strength - 2026-11-27 (labelId: 474723165319233001, sportType: 402)")
        found = coros.records(text)
        self.assertEqual([(r["label_id"], r["sport_type"], r["start"]) for r in found],
                         [("474723165319233737", 100, "20261128"), ("474723165319233001", 402, "20261127")])

    def test_reading_coros_answers(self):
        self.assertEqual(coros.sse_messages('event: message\ndata: {"id": 2}\n\n'), [{"id": 2}])
        self.assertEqual(coros._start_text(20261126), ("2026-11-26", ""))


if __name__ == "__main__":
    unittest.main()

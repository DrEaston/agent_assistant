"""Coach mode (apps/trainer/coach.py): invites, prescriptions in plain terms, the athlete's own target times, and
the coach never seeing the athlete's runs or times."""

import tempfile
import unittest
import warnings
from pathlib import Path

from database import Database, reset_current_user_id, set_current_user_id

from apps.trainer import coach, coach_reader, plan, speed


MODEL = {"cs": 4.5, "d_prime": 200, "vmax": 8.0, "easy": 3.4}   # threshold 5:58/mi, top speed 8 m/s


TEST_PASSCODE_HASH = coach._hash("test-passcode")   # tests use their own passcode, never the real one


def setUpModule():
    global _real_hash
    _real_hash, coach.PASSCODE_HASH = coach.PASSCODE_HASH, TEST_PASSCODE_HASH


def tearDownModule():
    coach.PASSCODE_HASH = _real_hash


class FakeAI:
    """Stands in for the AI model: returns one fixed reply."""

    def __init__(self, reply):
        self.reply = reply

    def chat(self, messages, system_prompt, max_completion_tokens=1024):
        return self.reply


class ReaderTests(unittest.TestCase):
    """coach_reader: the AI's reply is checked like any coach's input; it never sets paces."""

    def test_reads_sets_and_keeps_the_rest_as_notes(self):
        out = coach_reader.read("2 mi wu. 6x mile @ T, 90s jog; 8x200 fast", provider=FakeAI(
            'Here you go: {"title": "Miles and 200s", "sets": [{"reps": 6, "metres": 1609, "intensity": "threshold", '
            '"rest": "90 s jog"}, {"reps": "8", "metres": "200", "intensity": "sprint", "rest": ""}], '
            '"notes": "2 mile warm-up"}'))
        self.assertEqual(out["title"], "Miles and 200s")
        self.assertEqual([coach.describe_set(x) for x in out["sets"]][0], "6 × 1 mile at threshold, 90 s jog")
        self.assertEqual(out["sets"][1]["reps"], 8)
        self.assertEqual(out["notes"], "2 mile warm-up")

    def test_unknown_efforts_become_by_feel_and_nonsense_is_refused(self):
        out = coach_reader.read("4x400 at goal pace", provider=FakeAI(
            '{"sets": [{"reps": 4, "metres": 400, "intensity": "goal pace"}]}'))
        self.assertEqual(out["sets"][0]["intensity"], "feel")
        for reply in ("no json here", '{"sets": []}', '{"sets": [{"reps": 4, "metres": 5}]}'):
            with self.assertRaises(coach.Invalid):
                coach_reader.read("whatever", provider=FakeAI(reply))
        with self.assertRaises(coach.Invalid):
            coach_reader.read("   ", provider=FakeAI("{}"))


class As:
    """Sign in as someone for a block."""

    def __init__(self, user):
        self.user = user

    def __enter__(self):
        self.token = set_current_user_id(self.user)

    def __exit__(self, *exc):
        reset_current_user_id(self.token)


class CoachStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "coach.db"))
        self.db.init()
        self.athlete = self.db.create_user("runner@example.com", "Curtis", "x", "member")
        self.coach_user = self.db.create_user("coach@example.com", "Coach Kim", "x", "member")
        self.other = self.db.create_user("other@example.com", "Someone Else", "x", "member")
        self.store = coach.CoachStore(self.db)

    def tearDown(self):
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def link(self):
        with As(self.athlete):
            token = self.store.invite()
            self.assertEqual(self.store.invite(), token)            # one open link at a time
        with As(self.coach_user):
            self.store.accept(token)
        return token

    def test_the_targets_come_from_the_athletes_own_model(self):
        sets = coach.clean_sets([{"reps": "6", "metres": "1609", "intensity": "threshold", "rest": "90 s jog"},
                                 {"reps": "8", "metres": "200", "intensity": "sprint", "rest": "200 m jog"},
                                 {"reps": "", "metres": ""}])
        self.assertEqual(len(sets), 2)
        self.assertEqual(coach.describe_set(sets[0]), "6 × 1 mile at threshold, 90 s jog")
        rows = coach.targets(sets, MODEL)
        mile = round(1609 / 4.5)
        self.assertTrue(rows[0]["target"].startswith(speed.clock(mile) + " each ("))
        self.assertEqual(coach.describe_set(sets[1]), "8 × 200 m sprint, 200 m jog")
        k = coach.targets(coach.clean_sets([{"reps": "3", "metres": "2000", "intensity": "threshold"}]), MODEL)[0]
        self.assertIn(speed.pace(4.5) + "/mi", k["target"])       # pace per mile for longer reps that aren't a mile
        self.assertEqual(rows[1]["target"], f"about {speed.clock(200 / (0.9 * 8.0))} each")
        # 5K pace from d = CS·t + D': faster than threshold, slower than mile pace
        self.assertGreater(coach.target_speed("5k", MODEL), 4.5)
        self.assertGreater(coach.target_speed("mile", MODEL), coach.target_speed("5k", MODEL))
        # Without a model, Dieter says so instead of guessing
        self.assertIn("needs", coach.targets(sets, {"cs": None, "d_prime": None, "vmax": None})[0]["target"])
        with self.assertRaises(coach.Invalid):
            coach.clean_sets([{"reps": "6", "metres": "1609", "intensity": "warp speed"}])
        with self.assertRaises(coach.Invalid):
            coach.clean_sets([])

    def test_invite_prescribe_take_and_report_back(self):
        token = self.link()
        with As(self.coach_user):
            with self.assertRaises(coach.Invalid):
                self.store.accept(token)                             # used once
            self.assertEqual([a["name"] for a in self.store.my_athletes()], ["Curtis"])
            pid = self.store.prescribe(self.athlete, "2026-12-01", "Threshold miles",
                                       [{"reps": 6, "metres": 1609, "intensity": "threshold", "rest": "90 s jog"},
                                        {"reps": 8, "metres": 200, "intensity": "sprint", "rest": ""}],
                                       "Warm up 2 miles")
        with As(self.athlete):
            self.assertEqual([c["name"] for c in self.store.my_coaches()], ["Coach Kim"])
            mine = self.store.for_me()
            self.assertEqual((mine[0]["status"], mine[0]["coach"]), ("sent", "Coach Kim"))
            session = self.store.take(pid, MODEL)
            self.assertEqual((session["day"], session["kind"], session["title"]),
                             ("2026-12-01", "workout", "Threshold miles"))
            self.assertIn(speed.clock(round(1609 / 4.5)), session["details"])   # his times on his plan
            self.assertIn("Coach: Warm up 2 miles", session["details"])
            self.store.done(pid, "Felt smooth, last two hard")
        with As(self.coach_user):
            link, sent = self.store.sent_to(self.athlete)
            self.assertEqual((sent[0]["status"], sent[0]["athlete_note"]), ("done", "Felt smooth, last two hard"))
            # The coach's view holds what the coach wrote and the athlete's note: no times anywhere
            text = repr(sent)
            for secret in (speed.clock(round(1609 / 4.5)), speed.pace(4.5), "target"):
                self.assertNotIn(secret, text)

    def test_only_a_linked_coach_can_send_and_either_side_can_stop(self):
        with As(self.other):
            with self.assertRaises(PermissionError):
                self.store.prescribe(self.athlete, "2026-12-01", "", [{"reps": 1, "metres": 400}])
            with self.assertRaises(PermissionError):
                self.store.sent_to(self.athlete)
        self.link()
        with As(self.athlete):
            link_id = self.store.my_coaches()[0]["link_id"]
            self.store.end(link_id)
            self.assertEqual(self.store.my_coaches(), [])
        with As(self.coach_user):
            with self.assertRaises(PermissionError):
                self.store.prescribe(self.athlete, "2026-12-01", "", [{"reps": 1, "metres": 400}])
            with self.assertRaises(coach.Invalid):
                self.store.end(link_id)                              # already ended

    def test_an_athlete_cannot_coach_themselves_or_answer_someone_elses_workout(self):
        with As(self.athlete):
            token = self.store.invite()
            with self.assertRaises(coach.Invalid):
                self.store.accept(token)
        with As(self.coach_user):
            self.store.accept(token)
            pid = self.store.prescribe(self.athlete, "2026-12-02", "", [{"reps": 4, "metres": 800, "intensity": "5k"}])
        with As(self.other):
            with self.assertRaises(coach.Invalid):
                self.store.take(pid, MODEL)
        with As(self.athlete):
            self.store.decline(pid, "Racing that weekend")
            with self.assertRaises(coach.Invalid):
                self.store.take(pid, MODEL)
        with As(self.coach_user):
            with self.assertRaises(coach.Invalid):
                self.store.withdraw(pid)                             # answered: can't take it back
            pid2 = self.store.prescribe(self.athlete, "2026-12-03", "", [{"reps": 4, "metres": 800}])
            self.store.withdraw(pid2)
            self.assertEqual([w["id"] for w in self.store.sent_to(self.athlete)[1]], [pid])


class FullViewTests(CoachStoreTests):
    """Temporary full view: only Dieter's owner can give it, it ends by itself, and stopping coaching ends it."""

    def test_only_the_owner_can_turn_it_on_and_it_ends_by_itself(self):
        from datetime import date, timedelta
        self.link()
        with As(self.athlete):
            link_id = self.store.my_coaches()[0]["link_id"]
            with self.assertRaises(PermissionError):
                self.store.set_full_view(link_id, True)              # not the owner
        conn = self.db.connect()
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (self.athlete,))
        conn.commit()
        with As(self.athlete):
            until = self.store.set_full_view(link_id, True)
            self.assertEqual(until, (date.today() + timedelta(days=coach.FULL_VIEW_DAYS)).isoformat())
        with As(self.coach_user):
            self.assertTrue(self.store.full_view(self.athlete))
            self.assertFalse(self.store.full_view(self.athlete, today=date.today() + timedelta(days=31)))
        with As(self.other):
            self.assertFalse(self.store.full_view(self.athlete))     # not their coach
        with As(self.athlete):
            self.store.set_full_view(link_id, False)
        with As(self.coach_user):
            self.assertFalse(self.store.full_view(self.athlete))
        with As(self.athlete):
            self.store.set_full_view(link_id, True)
            self.store.end(link_id)
        with As(self.coach_user):
            self.assertFalse(self.store.full_view(self.athlete))     # stopping coaching ends it at once


class CoachPasscodeTests(CoachStoreTests):
    """/coach: one shared passcode opens a coach account linked to Dieter's owner, with full view."""

    def test_passcode_links_the_coach_to_the_owner_with_full_view(self):
        conn = self.db.connect()
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (self.athlete,))
        conn.commit()
        self.assertIsNone(self.store.passcode_sign_in("test-passcodex"))
        self.assertIsNone(self.store.passcode_sign_in(""))
        coach_id = self.store.passcode_sign_in("test-passcode")
        self.assertEqual(self.db.get_user_by_id(coach_id)["role"], coach.PASSCODE_ROLE)
        self.assertEqual(self.store.passcode_sign_in(" test-passcode "), coach_id)      # same account every time
        with As(coach_id):
            athletes = self.store.my_athletes()
            self.assertEqual([a["athlete_id"] for a in athletes], [self.athlete])
            self.assertTrue(self.store.full_view(self.athlete))
            self.assertEqual(self.store.send_to_all("2026-12-01", "Miles", [{"reps": 6, "metres": 1609,
                                                                             "intensity": "threshold"}]), 1)
            self.assertEqual(self.store.sent_by_me()[0]["athlete"], "Curtis")
        with As(self.athlete):
            self.assertEqual(self.store.new_for_me()[0]["title"], "Miles")


class CoachPageTests(unittest.TestCase):
    """The pages: the athlete sees their times, the coach doesn't."""

    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api = api

    def client_for(self, user):
        import secrets
        from datetime import datetime, timedelta
        from fastapi.testclient import TestClient
        session = secrets.token_urlsafe(16)
        self.api.db.create_session(user, self.api.hash_session_token(session),
                                   (datetime.utcnow() + timedelta(days=1)).isoformat())
        client = TestClient(self.api.app)
        client.cookies.set(self.api.SESSION_COOKIE_NAME, session)
        return client

    def make_user(self, name):
        import uuid
        db = self.api.db
        user = db.create_user(f"{name}-{uuid.uuid4().hex[:8]}@example.com", name, "x", "member")
        conn = db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (user,))
        conn.commit()
        return user

    def test_the_coach_pages_never_show_the_athletes_times(self):
        from unittest import mock
        athlete, coach_user = self.make_user("Runner"), self.make_user("Coach")
        runner, coaching = self.client_for(athlete), self.client_for(coach_user)
        with mock.patch.object(coach, "athlete_model", return_value=dict(MODEL)):
            self.assertEqual(runner.get("/apps/trainer/my-coach").status_code, 200)
            runner.post("/apps/trainer/my-coach/invite")
            page = runner.get("/apps/trainer/my-coach").text
            token = page.split("/apps/trainer/coach-invite/")[1].split('"')[0]
            self.assertIn("invited you to be their coach", coaching.get(f"/apps/trainer/coach-invite/{token}").text)
            coaching.post(f"/apps/trainer/coach-invite/{token}")
            self.assertIn("Runner", coaching.get("/apps/trainer/athletes").text)
            sent = coaching.post(f"/apps/trainer/athletes/{athlete}/prescribe",
                                 data={"day": "2026-12-01", "title": "Miles and 200s", "reps_0": "6", "metres_0": "1609",
                                       "intensity_0": "threshold", "rest_0": "90 s jog", "reps_1": "8",
                                       "metres_1": "200", "intensity_1": "sprint"})
            self.assertEqual(sent.status_code, 200)
            mine = runner.get("/apps/trainer/my-coach").text
            self.assertIn("6 × 1 mile at threshold, 90 s jog", mine)
            self.assertIn(speed.clock(round(1609 / 4.5)), mine)
            theirs = coaching.get(f"/apps/trainer/athletes/{athlete}").text
            self.assertIn("6 × 1 mile at threshold, 90 s jog", theirs)
            self.assertNotIn(speed.clock(round(1609 / 4.5)), theirs)
            self.assertNotIn(speed.pace(4.5), theirs)
        stranger = self.client_for(self.make_user("Stranger"))
        self.assertEqual(stranger.get(f"/apps/trainer/athletes/{athlete}").status_code, 403)
        # Full view is the owner's only, and not on by default
        self.assertEqual(coaching.post(f"/apps/trainer/athletes/{athlete}/view").status_code, 403)

    def test_temporary_full_view_is_read_only_and_owner_only(self):
        athlete, coach_user = self.make_user("Owner"), self.make_user("Daniel")
        conn = self.api.db.connect()
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (athlete,))
        conn.commit()
        owner, daniel = self.client_for(athlete), self.client_for(coach_user)
        owner.post("/apps/trainer/my-coach/invite")
        token = owner.get("/apps/trainer/my-coach").text.split("/apps/trainer/coach-invite/")[1].split('"')[0]
        daniel.post(f"/apps/trainer/coach-invite/{token}")
        page = owner.get("/apps/trainer/my-coach").text
        self.assertIn("Turn on full view", page)
        link_id = page.split("/apps/trainer/coaching-links/")[1].split("/")[0]
        owner.post(f"/apps/trainer/coaching-links/{link_id}/full-view", data={"on": "1"})
        self.assertIn("View Owner", daniel.get(f"/apps/trainer/athletes/{athlete}").text)
        # while full view is on (temporary), the coach's athlete page shows the paces Dieter picked
        from unittest import mock
        with mock.patch.object(coach, "athlete_model", return_value=dict(MODEL)):
            daniel.post(f"/apps/trainer/athletes/{athlete}/prescribe",
                        data={"day": "2026-12-01", "title": "Miles", "reps_0": "6", "metres_0": "1609",
                              "intensity_0": "threshold"})
            profile = daniel.get(f"/apps/trainer/athletes/{athlete}").text
        self.assertIn(f"{speed.pace(4.5)}/mi", profile)                     # threshold in the pace table
        self.assertIn(speed.clock(round(1609 / 4.5)), profile)              # the rep time on the sent workout
        daniel.post(f"/apps/trainer/athletes/{athlete}/view")
        week = daniel.get("/apps/trainer/week")
        self.assertEqual(week.status_code, 200)
        self.assertIn("Viewing Owner's Trainer, read-only", week.text)
        # Read-only: forms are refused while viewing
        self.assertEqual(daniel.post("/apps/trainer/sessions/add", data={"day": "2026-12-01", "kind": "easy"},
                                     follow_redirects=False).status_code, 403)
        # His own coach pages stay his
        self.assertIn("Owner", daniel.get("/apps/trainer/athletes").text)
        daniel.post("/apps/trainer/view-as/stop")
        self.assertNotIn("read-only", daniel.get("/apps/trainer/week").text)
        # Turned off: the cookie alone does nothing
        owner.post(f"/apps/trainer/coaching-links/{link_id}/full-view")
        self.assertNotIn(speed.clock(round(1609 / 4.5)), daniel.get(f"/apps/trainer/athletes/{athlete}").text)
        daniel.cookies.set("trainer_view_as", str(athlete))
        self.assertNotIn("Viewing Owner", daniel.get("/apps/trainer/week").text)


    def test_coach_page_behind_the_passcode(self):
        from unittest import mock
        from fastapi.testclient import TestClient
        athlete = self.make_user("Curtis")
        conn = self.api.db.connect()
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (athlete,))
        conn.commit()
        runner = self.client_for(athlete)
        self.assertIn("switches this browser to the coach", runner.get("/coach").text)   # signed in as himself
        self.api._coach_tries.clear()
        visitor = TestClient(self.api.app)
        with mock.patch.object(coach.CoachStore, "_owner", return_value=athlete), \
                mock.patch.object(coach, "athlete_model", return_value=dict(MODEL)):
            self.assertIn("Passcode", visitor.get("/coach").text)
            self.assertIn("passcode wasn&#39;t recognized", visitor.post("/coach", data={"passcode": "nope-nope"}).text)
            signed_in = visitor.post("/coach", data={"passcode": "test-passcode"}, follow_redirects=False)
            self.assertEqual((signed_in.status_code, signed_in.headers["location"]), (303, "/coach"))
            page = visitor.get("/coach").text                      # the coach page, at its own address
            self.assertIn("View athlete page", page)
            self.assertIn(f'<option value="{athlete}">Curtis</option>', page)
            self.assertIn("Send to all athletes (1)", page)
            self.assertIn('id="talk"', page)                                   # voice for the workout box
            with mock.patch.object(self.api, "transcribe_upload", return_value={"text": "6 x mile at threshold"}):
                said = visitor.post("/coach/transcribe", files={"audio": ("w.webm", b"audio", "audio/webm")})
            self.assertEqual(said.json()["text"], "6 x mile at threshold")
            # only the coach page: everything else sends it back, and other forms are refused
            for path in ("/", "/apps/trainer/week", "/apps/recipes", "/apps/trainer/my-coach"):
                moved = visitor.get(path, follow_redirects=False)
                self.assertEqual((moved.status_code, moved.headers.get("location")), (303, "/coach"), path)
            self.assertEqual(visitor.post("/apps/trainer/sessions/add", data={"day": "2026-12-01", "kind": "easy"},
                                          follow_redirects=False).status_code, 403)
            # write a workout for all athletes
            day = (__import__("datetime").date.today() + __import__("datetime").timedelta(days=1)).isoformat()
            reply = ('{"title": "Threshold miles", "sets": [{"reps": 6, "metres": 1609, "intensity": "threshold", '
                     '"rest": "90 s jog"}], "notes": "2 mile warm-up"}')
            with mock.patch.object(coach_reader, "get_llm_provider", return_value=FakeAI(reply)):
                sent = visitor.post("/coach/send", follow_redirects=False,
                                    data={"day": day, "workout": "2 mi wu, 6x mile @ T w/ 90s jog"})
            self.assertEqual(sent.headers["location"], "/coach?sent=1#sent")
            sent_page = visitor.get("/coach?sent=1").text
            self.assertIn("Sent to 1 athlete", sent_page)
            self.assertIn("Dieter read it as: 6 × 1 mile at threshold, 90 s jog", sent_page)
            self.assertNotIn(speed.clock(round(1609 / 4.5)), sent_page)          # no paces on the coach page
            with mock.patch.object(coach_reader, "get_llm_provider", return_value=FakeAI("sorry, no idea")):
                failed = visitor.post("/coach/send", data={"day": day, "workout": "something odd"})
            self.assertIn("read that workout just now", failed.text)
            self.assertIn("something odd", failed.text)                           # what they typed is kept
            # the athlete gets it with his own times
            home = runner.get("/apps/trainer").text
            self.assertIn("New from your coach", home)
            self.assertIn(speed.clock(round(1609 / 4.5)), home)
            # the coach opens his athlete page (read-only) and sees the pace Dieter picked
            visitor.post("/coach/view", data={"athlete_id": str(athlete)})
            viewing = visitor.get("/apps/trainer")
            self.assertEqual(viewing.status_code, 200)
            self.assertIn("Viewing Curtis's Trainer, read-only", viewing.text)
            self.assertIn("Coming up from the coach", viewing.text)
            self.assertIn(speed.clock(round(1609 / 4.5)), viewing.text)
            self.assertNotIn("Put it on my plan", viewing.text)
            # while still viewing, the coach page's forms keep working (they're the coach's own)
            with mock.patch.object(coach_reader, "get_llm_provider", return_value=FakeAI(reply)):
                again = visitor.post("/coach/send", follow_redirects=False,
                                     data={"day": day, "workout": "6x mile @ T w/ 90s jog"})
            self.assertEqual(again.status_code, 303, again.text[:200])
            self.assertEqual(visitor.post("/coach/view", data={"athlete_id": str(athlete)},
                                          follow_redirects=False).status_code, 303)
            stopped = visitor.post("/apps/trainer/view-as/stop", follow_redirects=False)
            self.assertEqual(stopped.headers["location"], "/coach")
        self.assertIn("New from Coach", runner.get("/").text)

    def test_passcode_tries_are_limited(self):
        from fastapi.testclient import TestClient
        self.api._coach_tries.clear()
        visitor = TestClient(self.api.app)
        for _ in range(self.api.COACH_TRIES):
            visitor.post("/coach", data={"passcode": "guess-guess"})
        self.assertIn("Too many tries", visitor.post("/coach", data={"passcode": "guess-guess"}).text)
        self.api._coach_tries.clear()


if __name__ == "__main__":
    unittest.main()

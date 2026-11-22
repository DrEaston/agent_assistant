"""The kitchen-PC helper: sleep/wake, motion wakes the screen, events for the page, and who may control it."""

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "helper"))
import dieter_helper as dh  # noqa: E402


class FakeMonitor:
    def __init__(self):
        self.calls = []

    def keep_pc_awake(self):
        self.calls.append("awake")

    def off(self):
        self.calls.append("off")

    def on(self):
        self.calls.append("on")


class FakeWatcher:
    """Stands in for the webcam: tests call .see(side) to simulate someone walking by."""

    def __init__(self, on_motion, settings):
        self.on_motion, self.watching, self.camera_ok = on_motion, False, True

    def start(self):
        self.watching = True

    def stop(self):
        self.watching = False

    def see(self, side):
        if self.watching:
            self.on_motion(side)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.object(dh, "load_settings", return_value={"sensitivity": "medium", "camera": 0})
        self.settings.start()
        self.monitor = FakeMonitor()
        self.helper = dh.Helper(monitor=self.monitor, watcher_cls=FakeWatcher)

    def tearDown(self):
        self.settings.stop()

    def wait_for(self, condition, seconds=2.0):
        end = time.time() + seconds
        while time.time() < end and not condition():
            time.sleep(0.02)
        return condition()

    def test_sleep_turns_the_monitor_off_and_watches(self):
        self.helper.sleep()
        self.assertTrue(self.helper.asleep)
        self.assertTrue(self.wait_for(lambda: "off" in self.monitor.calls))
        self.assertTrue(self.helper.watcher.watching)

    def test_motion_wakes_the_monitor_and_tells_the_page_which_side(self):
        self.helper.sleep()
        self.wait_for(lambda: self.helper.watcher.watching)
        start = self.helper.latest_id()
        self.helper.watcher.see("left")
        self.assertFalse(self.helper.asleep)
        self.assertEqual(self.monitor.calls[-1], "on")
        events = self.helper.events_after(start, timeout=0.1)
        self.assertEqual([(e["type"], e["reason"], e["side"]) for e in events], [("wake", "motion", "left")])
        self.assertFalse(self.helper.watcher.watching)  # camera off again once awake

    def test_waking_before_the_monitor_went_off_cancels_the_sleep(self):
        self.helper.sleep()
        self.helper.wake("asked")
        time.sleep(0.8)
        self.assertNotIn("off", self.monitor.calls)

    def test_events_wait_for_the_next_one(self):
        start = self.helper.latest_id()
        threading.Timer(0.1, lambda: self.helper.wake("asked")).start()
        events = self.helper.events_after(start, timeout=2)
        self.assertEqual(events[0]["type"], "wake")
        self.assertEqual(self.helper.events_after(self.helper.latest_id(), timeout=0.1), [])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = patch.object(dh, "load_settings", return_value={"sensitivity": "medium", "camera": 0})
        cls.settings.start()
        cls.monitor = FakeMonitor()
        cls.helper = dh.Helper(monitor=cls.monitor, watcher_cls=FakeWatcher)
        cls.server = dh.serve(cls.helper, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.settings.stop()

    def request(self, method, path, origin="https://askdieter.ai"):
        req = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def test_askdieter_pages_can_sleep_and_wake_it(self):
        status, headers, body = self.request("GET", "/status")
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://askdieter.ai")
        self.assertEqual(self.request("POST", "/sleep")[0], 200)
        self.assertTrue(self.helper.asleep)
        status, _, body = self.request("POST", "/wake")
        self.assertEqual((status, body["event"]["type"]), (200, "wake"))

    def test_other_sites_and_programs_cannot_control_it(self):
        self.assertEqual(self.request("POST", "/sleep", origin="https://evil.example")[0], 403)
        self.assertEqual(self.request("POST", "/sleep", origin=None)[0], 403)
        self.assertEqual(self.request("GET", "/status", origin="https://evil.example")[0], 403)
        self.assertFalse(self.helper.asleep)

    def test_preflight_allows_private_network_access(self):
        status, headers, _ = self.request("OPTIONS", "/sleep")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Private-Network"), "true")


if __name__ == "__main__":
    unittest.main()

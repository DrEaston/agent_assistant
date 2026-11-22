"""Dieter helper for the kitchen PC: turns the monitor fully off on request, watches the webcam for someone
walking by, and turns the monitor back on (the Dieter page then shows the eyes and fades back in).

It runs quietly in the tray and talks to the Dieter page through a tiny web server that only listens on this
computer (127.0.0.1). The camera is only opened while the screen is asleep, frames stay in memory, and nothing
is saved or sent anywhere.

Endpoints (called by askdieter.ai pages in this PC's browser):
  GET  /status                -> {"ok", "version", "asleep", "camera"}
  POST /sleep                 -> monitor off, start watching
  POST /wake                  -> monitor on (e.g. a step alert or timer went off)
  GET  /events?after=N        -> waits up to 25 s for the next event: {"id", "type": "wake", "reason", "side"}
Later the "hey Dieter" listener joins this same helper.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERSION = "0.1.0"
PORT = 47613
ALLOWED_ORIGINS = {"https://askdieter.ai", "https://www.askdieter.ai", "http://localhost:8000", "http://127.0.0.1:8000"}
# The page "Open Dieter" shows (override with "dieter_page" in helper.json).
DIETER_PAGE = "https://askdieter.ai/thanksgiving/cook"
SETTINGS_FILE =Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Dieter" / "helper.json"
# Share of the picture that has to change to count as someone there. Higher = needs more movement.
SENSITIVITY = {"high": 0.004, "medium": 0.012, "low": 0.03}


# ---------------------------------------------------------------- settings

def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("sensitivity", "medium")
    data.setdefault("camera", 0)
    return data


def save_settings(settings):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- the monitor (Windows)

def _powershell(command, timeout=10):
    """Run a short PowerShell command without a window; returns its output ('' on failure)."""
    import subprocess
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Monitor:
    """Darken the screen and bring it back. On other systems these do nothing (so the helper can be tested anywhere).

    Built-in screens (all-in-ones, laptops): the backlight goes to its lowest level while the page shows black, and
    Windows is told the display is still needed. Telling Windows "monitor off" instead would, on PCs with Modern
    Standby (most new ones), put the whole PC into standby a second later: the webcam freezes, nothing can wake it
    but a key, and Windows asks to sign in again.
    Separate monitors (no backlight control): the old way, monitor off, then a mouse nudge to wake it.
    """

    def __init__(self, settings=None):
        self.win = sys.platform == "win32"
        self.settings = settings if settings is not None else {}
        self._display_request = None
        if self.win:
            import ctypes
            self.ctypes = ctypes
            self.user32 = ctypes.windll.user32
            self.kernel32 = ctypes.windll.kernel32
            self.can_dim = self._get_brightness() is not None
            # If the helper stopped while the screen was dimmed, put the brightness back.
            if self.can_dim and self.settings.get("dimmed_from") is not None:
                self._set_brightness(self.settings.pop("dimmed_from"))
                save_settings(self.settings)

    def keep_pc_awake(self):
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: the PC itself never sleeps while the helper runs (the screen may).
        if self.win:
            self.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)

    # built-in screen brightness (WMI), 0-100
    def _get_brightness(self):
        out = _powershell("(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
                          "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness")
        return int(out) if out.isdigit() else None

    def _set_brightness(self, level):
        _powershell("Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods | "
                    f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=0; Brightness={int(level)}}}"
                    " | Out-Null")

    # a process-wide "the display is in use" request, so Windows doesn't switch the screen off or go to standby
    def _hold_display(self, hold):
        k = self.kernel32
        if hold and self._display_request is None:
            ct = self.ctypes

            class REASON_CONTEXT(ct.Structure):
                _fields_ = [("Version", ct.c_ulong), ("Flags", ct.c_ulong), ("SimpleReasonString", ct.c_wchar_p)]

            k.PowerCreateRequest.restype = ct.c_void_p
            k.PowerCreateRequest.argtypes = [ct.POINTER(REASON_CONTEXT)]
            k.PowerSetRequest.argtypes = k.PowerClearRequest.argtypes = [ct.c_void_p, ct.c_int]
            k.CloseHandle.argtypes = [ct.c_void_p]
            self._reason = REASON_CONTEXT(0, 1, "Dieter kitchen screen is resting, watching for someone")
            handle = k.PowerCreateRequest(ct.byref(self._reason))
            if handle and handle != ct.c_void_p(-1).value:
                k.PowerSetRequest(handle, 0)  # PowerRequestDisplayRequired
                self._display_request = handle
        elif not hold and self._display_request is not None:
            k.PowerClearRequest(self._display_request, 0)
            k.CloseHandle(self._display_request)
            self._display_request = None

    def off(self):
        if not self.win:
            return
        if self.can_dim:
            self._hold_display(True)
            if self.settings.get("dimmed_from") is None:
                current = self._get_brightness()
                self.settings["dimmed_from"] = current if current else 100
                save_settings(self.settings)
            self._set_brightness(0)
        else:
            # WM_SYSCOMMAND, SC_MONITORPOWER, 2 = off. PostMessage so a busy window can't block us.
            self.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)

    def on(self):
        if not self.win:
            return
        if self.can_dim:
            level = self.settings.pop("dimmed_from", None)
            if level is not None:
                self._set_brightness(level)
                save_settings(self.settings)
            self._hold_display(False)
        # ES_DISPLAY_REQUIRED without ES_CONTINUOUS: counts as activity once, doesn't pin the screen on.
        self.kernel32.SetThreadExecutionState(0x00000002)
        # A one-pixel mouse nudge (and back) is what reliably turns a sleeping monitor on in Windows 10/11.
        MOUSEEVENTF_MOVE = 0x0001
        self.user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, 0)
        time.sleep(0.05)
        self.user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, 0)


# ---------------------------------------------------------------- motion

class MotionWatcher:
    """Opens the webcam while asleep and calls on_motion(side) when enough of the picture changes."""

    def __init__(self, on_motion, settings):
        self.on_motion = on_motion
        self.settings = settings
        self._stop = threading.Event()
        self._thread = None
        self.camera_ok = None  # None = not tried yet

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="motion", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            import cv2
        except ImportError:
            self.camera_ok = False
            print("Motion: OpenCV isn't installed; only a mouse, key or alert will wake the screen.")
            return
        cap = cv2.VideoCapture(int(self.settings.get("camera", 0)), cv2.CAP_DSHOW if sys.platform == "win32" else 0)
        if not cap or not cap.isOpened():
            self.camera_ok = False
            print("Motion: couldn't open the webcam.")
            return
        self.camera_ok = True
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        background = None
        started = time.time()
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.2)
                    continue
                gray = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY), (9, 9), 0)
                if background is None:
                    background = gray.astype("float")
                    continue
                cv2.accumulateWeighted(gray, background, 0.08)  # slow changes (light fading) blend in
                diff = cv2.absdiff(gray, cv2.convertScaleAbs(background))
                mask = cv2.threshold(diff, 28, 255, cv2.THRESH_BINARY)[1]
                changed = cv2.countNonZero(mask) / mask.size
                need = SENSITIVITY.get(self.settings.get("sensitivity", "medium"), SENSITIVITY["medium"])
                # Ignore the first second: the camera adjusts its exposure when it opens.
                if time.time() - started > 1.5 and changed >= need:
                    moments = cv2.moments(mask)
                    side = "center"
                    if moments["m00"]:
                        x = moments["m10"] / moments["m00"] / mask.shape[1]
                        # The webcam faces the room, so its left is the viewer's right.
                        side = "right" if x < 0.4 else ("left" if x > 0.6 else "center")
                    self.on_motion(side)
                    return
                time.sleep(0.15)  # about 6 looks a second is plenty for someone walking by
        finally:
            cap.release()


# ---------------------------------------------------------------- Dieter's face

class Face:
    """Dieter living on the screen: a small pair of cartoon eyes in the lower-right corner (just above where the
    page's "Talk to Dieter" button sits), always on top. They blink and glance around now and then, close their
    orange lids while the screen rests, and open and look around when someone walks in.

    While resting (and no Dieter page is showing its own sleep screen) a full-screen black window covers the rest
    of the screen; touching it wakes Dieter. All drawing happens on one Tk thread; other threads queue requests.
    """

    OUTLINE, WHITE, PUPIL, SHINE, LID = "#1c140e", "#ffffff", "#111111", "#ffffff", "#ea580c"
    KEY = "#1d150f"  # see-through colour around the eyes (next to the outline colour, so edges blend cleanly)
    BOX = (124, 70)  # the eyes' size at 100% display scaling, in pixels
    SS = 3           # drawn this many times larger, then shrunk, for smooth edges

    def __init__(self, on_touch=None, settings=None):
        import queue
        self.on_touch = on_touch
        self.settings = settings if settings is not None else {}
        self._q = queue.Queue()
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="face", daemon=True).start()
        self._ready.wait(5)

    # called from any thread
    def rest(self):
        self._q.put(("rest", None))

    def eyes(self, side="center"):
        self._q.put(("wake", side))

    def hide(self):
        self._q.put(("unrest", None))

    # ------------------------------------------------ the Tk thread
    def _run(self):
        try:
            import tkinter as tk
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            self._ready.set()
            return
        self.Image, self.ImageDraw, self.ImageTk = Image, ImageDraw, ImageTk
        if sys.platform == "win32":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # sharp, real-size pixels on scaled displays
            except (AttributeError, OSError):
                pass
            self.scale = ctypes.windll.user32.GetDpiForSystem() / 96 if hasattr(ctypes.windll.user32, "GetDpiForSystem") else 1.0
        else:
            self.scale = 1.0
        root = self.root = tk.Tk()
        root.withdraw()
        root.title("Dieter")

        # the black screen while resting
        self.rest_win = tk.Toplevel(root, bg="black", cursor="none")
        self.rest_win.withdraw()
        self.rest_win.overrideredirect(True)
        for event in ("<Motion>", "<KeyPress>", "<ButtonPress>"):
            self.rest_win.bind(event, self._touched)

        # the eyes
        w, h = int(self.BOX[0] * self.scale), int(self.BOX[1] * self.scale)
        self.size = (w, h)
        win = self.eyes_win = tk.Toplevel(root, bg=self.KEY)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", self.KEY)
        try:
            win.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.label = tk.Label(win, bg=self.KEY, bd=0, highlightthickness=0)
        self.label.pack()
        self.label.bind("<ButtonPress>", lambda e: self._double_blink())
        self._place()

        # what the eyes are doing: lid 0 = open, 1 = shut; look x, y from -1 to 1
        self.lid, self.lid_to = 1.0, 1.0
        self.look, self.look_to = [0.0, 0.0], [0.0, 0.0]
        self.resting = False
        self._rested_at = 0.0
        self._drawn = None
        self._script = []  # pending [(time, action)] for the current little performance
        self._next_blink = time.time() + 3
        self._next_glance = time.time() + 8
        if self.settings.get("eyes", True):
            win.deiconify()
        else:
            win.withdraw()
        root.after(16, self._tick)
        root.after(2000, self._keep_on_top)
        self._ready.set()
        root.mainloop()

    def _place(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = self.size
        right = int(self.settings.get("eyes_right", 28) * self.scale)
        bottom = int(self.settings.get("eyes_bottom", 112) * self.scale)  # sits above the "Talk to Dieter" button
        self.eyes_win.geometry(f"{w}x{h}+{sw - w - right}+{sh - h - bottom}")

    def _keep_on_top(self):
        # A full-screen browser can end up above us; quietly come back on top.
        if self.eyes_win.winfo_viewable():
            self.eyes_win.attributes("-topmost", True)
            self.eyes_win.lift()
        self.root.after(2000, self._keep_on_top)

    def _touched(self, _event):
        if time.time() - self._rested_at > 1.2 and self.resting and self.on_touch:
            self.on_touch()

    # ------------------------------------------------ behaviour
    def _do(self, what, arg):
        now = time.time()
        if what == "rest":
            self.resting = True
            self._rested_at = now
            self._script = []
            self.lid_to, self.look_to = 1.0, [0.0, 0.25]  # eyes drift down and close
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.rest_win.geometry(f"{sw}x{sh}+0+0")
            self.rest_win.deiconify()
            self.rest_win.attributes("-topmost", True)
            self.rest_win.focus_force()
            self.eyes_win.lift()
        elif what == "unrest":
            self._end_rest()
        elif what == "wake":
            self._end_rest()
            first = {"left": -1.0, "right": 1.0}.get(arg, 0.0)
            other = -(first or -0.8)
            # (seconds from now, lid, look) - open, glance toward whoever's there, look around, blink, settle
            self._script = [(now + t, lid, look) for t, lid, look in (
                (0.0, 0.0, (0.0, 0.0)), (0.45, None, (first or -0.8, 0.1)), (1.1, None, (other, 0.05)),
                (1.75, 1.0, None), (1.88, 0.0, (0.5, -0.5)), (2.5, None, (0.0, 0.0)))]
            self._next_blink = now + 4
            self._next_glance = now + 9

    def _end_rest(self):
        self.resting = False
        self.rest_win.withdraw()
        self.lid_to = 0.0

    def _double_blink(self):
        now = time.time()
        self._script = [(now, 1.0, (0.0, 0.0)), (now + 0.12, 0.0, None), (now + 0.3, 1.0, None), (now + 0.42, 0.0, None)]

    def _tick(self):
        import random
        while not self._q.empty():
            self._do(*self._q.get_nowait())
        now = time.time()
        while self._script and self._script[0][0] <= now:
            _, lid, look = self._script.pop(0)
            if lid is not None:
                self.lid_to = lid
            if look is not None:
                self.look_to = list(look)
        if not self.resting and not self._script:
            if now >= self._next_blink:
                self._script = [(now, 1.0, None), (now + 0.12, 0.0, None)]
                self._next_blink = now + random.uniform(3, 7)
            elif now >= self._next_glance:
                spot = (random.choice((-0.8, -0.5, 0.5, 0.8)), random.uniform(-0.4, 0.3))
                self._script = [(now, None, spot), (now + random.uniform(0.9, 1.6), None, (0.0, 0.0))]
                self._next_glance = now + random.uniform(8, 16)
        # ease toward the targets (lids shut fast for blinks, close slowly for sleep)
        lid_speed = 0.08 if self.resting else 0.45
        self.lid += (self.lid_to - self.lid) * lid_speed
        for i in (0, 1):
            self.look[i] += (self.look_to[i] - self.look[i]) * 0.22
        state = (round(self.lid, 2), round(self.look[0], 2), round(self.look[1], 2))
        if state != self._drawn:
            self._drawn = state
            self._draw(*state)
        self.root.after(33, self._tick)

    # ------------------------------------------------ drawing
    def _draw(self, lid, lx, ly):
        Image, ImageDraw = self.Image, self.ImageDraw
        s = self.SS * self.scale
        W, H = int(self.BOX[0] * s), int(self.BOX[1] * s)
        img = Image.new("RGB", (W, H), self.KEY)
        d = ImageDraw.Draw(img)
        line = max(2, int(3 * s))
        for cx in (34, 90):
            cy, rx, ry = 35, 26, 31
            box = [(cx - rx) * s, (cy - ry) * s, (cx + rx) * s, (cy + ry) * s]
            # white of the eye
            d.ellipse(box, fill=self.WHITE)
            # pupil and its shine dots, moved by where Dieter is looking
            px, py = cx + lx * 9, cy + 3 + ly * 11
            d.ellipse([(px - 14) * s, (py - 14) * s, (px + 14) * s, (py + 14) * s], fill=self.PUPIL)
            d.ellipse([(px + 1) * s, (py - 10) * s, (px + 10) * s, (py - 1) * s], fill=self.SHINE)
            d.ellipse([(px - 8) * s, (py + 4) * s, (px - 4) * s, (py + 8) * s], fill=self.SHINE)
            # the orange lid, coming down from the top with a curved edge, kept inside the eye
            if lid > 0.02:
                from PIL import ImageChops
                edge = (cy - ry) + lid * (2 * ry + 6)
                curve = [(cx - rx * 1.7) * s, (edge - 2.6 * ry) * s, (cx + rx * 1.7) * s, edge * s]
                eye = Image.new("L", (W, H), 0)
                ImageDraw.Draw(eye).ellipse(box, fill=255)
                lid_mask = Image.new("L", (W, H), 0)
                ImageDraw.Draw(lid_mask).ellipse(curve, fill=255)
                edge_mask = Image.new("L", (W, H), 0)
                ImageDraw.Draw(edge_mask).arc(curve, 0, 180, fill=255, width=line)
                img.paste(self.LID, (0, 0, W, H), ImageChops.multiply(lid_mask, eye))
                img.paste(self.OUTLINE, (0, 0, W, H), ImageChops.multiply(edge_mask, eye))
                if lid > 0.9:  # shut: a sleepy curved line across the lid
                    d.arc([(cx - rx * 0.7) * s, (cy - ry * 0.45) * s, (cx + rx * 0.7) * s, (cy + ry * 0.4) * s],
                          25, 155, fill=self.OUTLINE, width=line)
            d.ellipse(box, outline=self.OUTLINE, width=line)
        img = img.reduce(self.SS)
        self._photo = self.ImageTk.PhotoImage(img)
        self.label.configure(image=self._photo)


# ---------------------------------------------------------------- the helper

class Helper:
    def __init__(self, monitor=None, watcher_cls=MotionWatcher, face=None):
        self.settings = load_settings()
        self.monitor = monitor or Monitor(self.settings)
        self.watcher = watcher_cls(self._motion, self.settings)
        self.face = face
        self.asleep = False
        self._events = []
        self._next_id = 1
        self._listeners = 0  # Dieter pages waiting on /events right now
        self._last_listened = 0.0
        self._cond = threading.Condition()
        self.monitor.keep_pc_awake()

    def page_listening(self):
        # A page asks again right after each answer, so a gap of a moment between asks still counts as listening.
        with self._cond:
            return self._listeners > 0 or time.time() - self._last_listened < 3.0

    # events the page waits for
    def _emit(self, **event):
        with self._cond:
            event["id"] = self._next_id
            self._next_id += 1
            self._events = (self._events + [event])[-20:]
            self._cond.notify_all()
        return event

    def events_after(self, after, timeout=25.0):
        deadline = time.time() + timeout
        with self._cond:
            self._listeners += 1
            try:
                while True:
                    found = [e for e in self._events if e["id"] > after]
                    if found:
                        return found
                    left = deadline - time.time()
                    if left <= 0:
                        return []
                    self._cond.wait(left)
            finally:
                self._listeners -= 1
                self._last_listened = time.time()

    def latest_id(self):
        with self._cond:
            return self._events[-1]["id"] if self._events else 0

    # actions
    def sleep(self):
        self.asleep = True
        self._emit(type="sleep")
        # Give the page a moment to go dark first, then switch the monitor off and start watching.
        threading.Timer(0.6, self._sleep_now).start()

    def _sleep_now(self):
        if not self.asleep:
            return
        if self.face and not self.page_listening():
            self.face.rest()  # no Dieter page is showing its sleep screen, so Dieter's own window goes black
        self.monitor.off()
        self.watcher.start()

    def wake(self, reason="asked", side="center"):
        was_asleep = self.asleep
        self.asleep = False
        self.watcher.stop()
        self.monitor.on()
        if self.face:
            self.face.eyes(side)  # Dieter's eyes in the corner open and look toward whoever's there
        return self._emit(type="wake", reason=reason, side=side, was_asleep=was_asleep)

    def _motion(self, side):
        if self.asleep:
            self.wake("motion", side)

    def status(self):
        return {"ok": True, "version": VERSION, "asleep": self.asleep, "camera": self.watcher.camera_ok,
                "sensitivity": self.settings.get("sensitivity"), "last_event": self.latest_id()}

    def set_sensitivity(self, level):
        if level in SENSITIVITY:
            self.settings["sensitivity"] = level
            save_settings(self.settings)


def make_handler(helper):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DieterHelper/" + VERSION

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _cors(self):
            origin = self.headers.get("Origin", "")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                # Chrome asks before a public site talks to this computer (Private/Local Network Access).
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self):
            # Only pages from askdieter.ai (or a local Dieter) may control the screen.
            origin = self.headers.get("Origin")
            return origin is None or origin in ALLOWED_ORIGINS

        def do_OPTIONS(self):
            self.send_response(204 if self._allowed() else 403)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if not self._allowed():
                return self._json({"detail": "Not allowed."}, 403)
            url = urlsplit(self.path)
            if url.path == "/status":
                return self._json(helper.status())
            if url.path == "/events":
                after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
                return self._json({"events": helper.events_after(after)})
            return self._json({"detail": "Not found."}, 404)

        def do_POST(self):
            if not self._allowed() or self.headers.get("Origin") is None:
                # Changes need a browser page from an allowed site (blocks other programs' blind requests).
                return self._json({"detail": "Not allowed."}, 403)
            path = urlsplit(self.path).path
            if path == "/sleep":
                helper.sleep()
                return self._json({"ok": True})
            if path == "/wake":
                return self._json({"ok": True, "event": helper.wake("asked")})
            return self._json({"detail": "Not found."}, 404)

    return Handler


def serve(helper, port=PORT):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(helper))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------- tray icon

def tray(helper, server):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print(f"Dieter helper {VERSION} running on http://127.0.0.1:{PORT} (no tray icon). Ctrl+C to quit.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(194, 65, 12, 255))
    for x in (18, 38):  # two little eyes
        draw.ellipse((x, 22, x + 10, 36), fill=(255, 255, 255, 255))
        draw.ellipse((x + 3, 27, x + 8, 33), fill=(30, 20, 10, 255))

    def level(name):
        return pystray.MenuItem(name.capitalize(), lambda: helper.set_sensitivity(name),
                                checked=lambda item: helper.settings.get("sensitivity") == name, radio=True)

    def quit_app(icon):
        server.shutdown()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(f"Dieter helper {VERSION}", None, enabled=False),
        pystray.MenuItem("Open Dieter", lambda: open_dieter(helper.settings), default=True),
        pystray.MenuItem("Sleep the screen now", lambda: helper.sleep()),
        pystray.MenuItem("Test the eyes", lambda: helper.wake("test")),
        pystray.MenuItem("Motion sensitivity", pystray.Menu(level("high"), level("medium"), level("low"))),
        pystray.MenuItem("Quit", quit_app),
    )
    pystray.Icon("dieter-helper", image, "Dieter helper", menu).run()


def find_browser():
    """Chrome if it's installed, otherwise Edge (always on Windows)."""
    for path in (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                 r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                 r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
                 r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                 r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"):
        full = os.path.expandvars(path)
        if os.path.exists(full):
            return full
    return None


def open_dieter(settings):
    """Open the Dieter page full screen, as its own app window (F11 leaves full screen)."""
    import subprocess
    import webbrowser
    page = settings.get("dieter_page", DIETER_PAGE)
    browser = find_browser()
    if browser:
        subprocess.Popen([browser, f"--app={page}", "--start-fullscreen"])
    else:
        webbrowser.open(page)


def main():
    if "--open-only" in sys.argv:  # the sign-in shortcut: just open the Dieter page (the helper runs separately)
        open_dieter(load_settings())
        return
    helper = Helper()
    helper.face = Face(on_touch=lambda: helper.wake("touch"), settings=helper.settings)
    face = helper.face
    try:
        server = serve(helper)
    except OSError:
        print("Another Dieter helper is already running.")
        return
    if "--quiet" not in sys.argv:
        face.eyes()  # hello
    tray(helper, server)


if __name__ == "__main__":
    main()

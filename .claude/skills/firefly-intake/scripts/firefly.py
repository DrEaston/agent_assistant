"""Firefly intake: take a mascot video apart into transparent stills and animations, and upload them to Dieter Media.

Run with the skill's own Python (see SKILL.md, "Setup"):

    firefly.py analyze  VIDEO WORK            frames, contact sheet, motion profile, last-second sheet
    firefly.py peek     WORK N1,N2,...  [--box x0,y0,x1,y1]   close-up sheet of chosen frames
    firefly.py cutout   WORK [--lo 14 --hi 60]  remove the black background around him (not inside him) on every frame (+ a checkerboard check)
    firefly.py build    WORK OUT --still N:name ... --anim name:start-end[:step][:fps][:once|loop|pingpong][:WxH]
    firefly.py upload   FILE ... --source TEXT [--site https://askdieter.ai]

WORK is a scratch folder (frames live there); OUT is where the finished files go (e.g. Downloads/<name>).
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

FPS = 24


# ---------------------------------------------------------------- analyze

def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def frames_of(work):
    return sorted((Path(work) / "frames").glob("f*.png"))


def contact(paths, out, cols=6, size=(320, 180), box=None):
    w, h = size
    sheet = Image.new("RGB", (cols * w, ((len(paths) + cols - 1) // cols) * (h + 18)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        if box:
            im = im.crop(box)
        x, y = (i % cols) * w, (i // cols) * (h + 18)
        sheet.paste(im.resize((w, h)), (x, y + 18))
        n = int(p.stem[1:])
        draw.text((x + 4, y + 3), f"frame {n}  ({(n - 1) / FPS:.2f}s)", fill="black")
    sheet.save(out, quality=85)
    return out


def analyze(video, work):
    work = Path(work)
    (work / "frames").mkdir(parents=True, exist_ok=True)
    info = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(video)], capture_output=True, text=True).stderr
    print("\n".join(line.strip() for line in info.splitlines() if "Duration" in line or "Video:" in line))
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                    str(work / "frames" / "f%04d.png")], check=True)
    paths = frames_of(work)
    motion, prev = [], None
    for p in paths:
        a = np.asarray(Image.open(p).convert("L").resize((320, 180)), dtype=np.float32)
        motion.append(0.0 if prev is None else float(np.abs(a - prev).mean()))
        prev = a
    img = np.asarray(Image.open(paths[0]).convert("RGB"), dtype=np.float32)
    corners = np.concatenate([c.reshape(-1, 3) for c in (img[:40, :40], img[:40, -40:], img[-40:, :40], img[-40:, -40:])])
    print(f"{len(paths)} frames. Background (corners) brightest channel: mean {corners.max(1).mean():.1f}, "
          f"max {corners.max(1).max():.0f}  (under ~12 = black, the cutout works)")
    print("motion per second:", [round(sum(motion[i:i + FPS]), 1) for i in range(0, len(motion), FPS)])
    quiet = [min(range(max(1, i), min(i + FPS, len(motion))), key=lambda j: motion[j]) + 1
             for i in range(0, len(motion), FPS)]
    print("stillest frame in each second (good key poses):", quiet)
    contact(paths[::6], work / "sheet.jpg")
    contact(paths[-FPS:][::2], work / "last_second.jpg")
    json.dump({"video": str(video), "motion": motion}, open(work / "analysis.json", "w"))
    print(f"Look at: {work / 'sheet.jpg'} and {work / 'last_second.jpg'}")


def peek(work, numbers, box=None):
    paths = [Path(work) / "frames" / f"f{int(n):04d}.png" for n in numbers]
    out = contact(paths, Path(work) / "peek.jpg", cols=4, size=(400, 240) if box else (480, 270), box=box)
    print(f"Look at: {out}")


# ---------------------------------------------------------------- cutout

def cut(path, lo=14.0, hi=60.0, bg=20.0, edge=3):
    """Black background -> transparent, outside the figure only.

    1. The background is the near-black (brightest channel under `bg`) that connects to the edge of the frame.
       Dark parts inside Dieter (claws, shadow under the chin, dark scales, pupils) don't connect to it, so they
       stay solid even when they're as dark as the background.
    2. Only the background and a thin band around it (`edge` px) get see-through, on a soft ramp from `lo` to `hi`
       so his outline stays smooth; everything else is fully solid.
    3. A mild un-premultiply on that band (more than ~1.15x leaves a red fringe on orange edges)."""
    from scipy import ndimage
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    m = rgb.max(axis=2)
    labels, _ = ndimage.label(m < bg)
    border = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    outside = np.isin(labels, border[border > 0])
    band = ndimage.binary_dilation(outside, iterations=edge)
    a = np.where(band, np.clip((m - lo) / (hi - lo), 0, 1), 1.0)
    a[outside] = np.minimum(a[outside], np.clip((m[outside] - lo) / (hi - lo), 0, 1))
    out = np.where(a[..., None] > 0, np.clip(rgb / np.maximum(a[..., None], 1e-3), 0, 255), 0)
    out = np.where(a[..., None] >= 1, rgb, np.minimum(out, rgb * 1.15))
    return Image.fromarray(np.dstack([out, a * 255]).astype(np.uint8), "RGBA")


def checker(img, cell=16):
    w, h = img.size
    y, x = np.mgrid[0:h, 0:w]
    bg = np.where(((x // cell + y // cell) % 2)[..., None] == 0, 235, 190).astype(np.uint8).repeat(3, 2)
    base = Image.fromarray(bg, "RGB").convert("RGBA")
    base.alpha_composite(img)
    return base.convert("RGB")


def cutout(work, lo, hi):
    work = Path(work)
    (work / "cut").mkdir(exist_ok=True)
    paths = frames_of(work)
    for p in paths:
        cut(p, lo, hi).save(work / "cut" / p.name, compress_level=3)
    picks = [paths[0], paths[len(paths) // 2], paths[-1]]
    ims = [checker(Image.open(work / "cut" / p.name)).resize((640, 360)) for p in picks]
    sheet = Image.new("RGB", (640, 360 * 3))
    for i, im in enumerate(ims):
        sheet.paste(im, (0, i * 360))
    sheet.save(work / "cutout_check.jpg", quality=85)
    print(f"Cut {len(paths)} frames. Look at: {work / 'cutout_check.jpg'}")


# ---------------------------------------------------------------- build

def build(work, out, stills, anims):
    work, out = Path(work), Path(out)
    cut_dir = work / "cut"
    (out / "stills").mkdir(parents=True, exist_ok=True)
    (out / "animations").mkdir(parents=True, exist_ok=True)
    made = []
    for spec in stills:
        n, name = spec.split(":", 1)
        dest = out / "stills" / f"{name}.png"
        Image.open(cut_dir / f"f{int(n):04d}.png").save(dest, optimize=True)
        made.append(dest)
        print(f"still {dest.name}: frame {n}, {dest.stat().st_size / 1e6:.1f} MB")
    for spec in anims:
        parts = spec.split(":")
        name, (start, end) = parts[0], map(int, parts[1].split("-"))
        step = int(parts[2]) if len(parts) > 2 and parts[2] else 2
        fps = float(parts[3]) if len(parts) > 3 and parts[3] else FPS / step
        mode = parts[4] if len(parts) > 4 and parts[4] else "once"
        size = tuple(map(int, parts[5].split("x"))) if len(parts) > 5 else (480, 270)
        nums = list(range(start, end + 1, step))
        if mode == "pingpong":
            nums = nums + nums[-2:0:-1]
        frames = [Image.open(cut_dir / f"f{n:04d}.png").resize(size, Image.LANCZOS) for n in nums]
        durations = [round(1000 / fps)] * len(frames)
        if mode == "once":
            durations[-1] = 1500  # rest on the last pose
        dest = out / "animations" / f"{name}.webp"
        frames[0].save(dest, save_all=True, append_images=frames[1:], duration=durations,
                       loop=1 if mode == "once" else 0, quality=72, method=4, alpha_quality=90)
        made.append(dest)
        print(f"animation {dest.name}: frames {start}-{end} every {step}, {len(frames)} frames, "
              f"{sum(durations) / 1000:.1f}s, {mode}, {size[0]}x{size[1]}, {dest.stat().st_size / 1e6:.1f} MB")
        if dest.stat().st_size > 8e6:
            print("  over 8 MB: too heavy for a web page; use a bigger step or a smaller size")
    if stills:
        ims = [checker(Image.open(p)).resize((480, 270)) for p in made if p.suffix == ".png"]
        sheet = Image.new("RGB", (480 * 4, 270 * ((len(ims) + 3) // 4)), "white")
        for i, im in enumerate(ims):
            sheet.paste(im, ((i % 4) * 480, (i // 4) * 270))
        sheet.save(work / "stills_check.jpg", quality=85)
        print(f"Look at: {work / 'stills_check.jpg'}")


# ---------------------------------------------------------------- upload (Dieter's own sign-in, once per computer)

TOKEN_FILE = Path.home() / ".dieter" / "firefly_upload.json"


def _post(url, data, files=None, token=None):
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if files is None and isinstance(data, dict) and data.get("_json"):
        return httpx.post(url, json=data["_json"], headers=headers, timeout=60)
    return httpx.post(url, data=data, files=files, headers=headers, timeout=120)


def _save(state):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(state))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def _login(site):
    """Register this computer as a Dieter app, open the browser for his Allow, catch the code on 127.0.0.1."""
    codes = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            codes.update(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<p>Done. You can close this tab and go back to Claude Code.</p>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    reg = _post(f"{site}/oauth/register", {"_json": {"client_name": "Claude Code (Firefly intake)",
                                                      "redirect_uris": [redirect]}})
    reg.raise_for_status()
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = f"{site}/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": "dieter",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    print("Opening Dieter in the browser: sign in if asked, then tap Allow.")
    webbrowser.open(url)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()
    if codes.get("state", [""])[0] != state or "code" not in codes:
        sys.exit("No OK from Dieter (closed, refused, or took over 5 minutes). Run upload again to retry.")
    tok = _post(f"{site}/oauth/token", {"grant_type": "authorization_code", "code": codes["code"][0],
                                        "redirect_uri": redirect, "client_id": client_id, "code_verifier": verifier})
    tok.raise_for_status()
    body = tok.json()
    return {"site": site, "client_id": client_id, "refresh_token": body["refresh_token"],
            "access_token": body["access_token"], "expires_at": time.time() + body.get("expires_in", 3600) - 60}


def access_token(site):
    state = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else {}
    if state.get("site") == site and state.get("access_token") and state.get("expires_at", 0) > time.time():
        return state["access_token"]
    if state.get("site") == site and state.get("refresh_token"):
        r = _post(f"{site}/oauth/token", {"grant_type": "refresh_token", "refresh_token": state["refresh_token"],
                                          "client_id": state["client_id"]})
        if r.status_code == 200:
            body = r.json()
            state.update(access_token=body["access_token"], refresh_token=body["refresh_token"],
                         expires_at=time.time() + body.get("expires_in", 3600) - 60)
            _save(state)
            return state["access_token"]
    state = _login(site)
    _save(state)
    return state["access_token"]


def upload(files, source, site):
    token = access_token(site)
    for f in map(Path, files):
        title = f.stem.split("_", 1)[-1].replace("_", " ").strip().capitalize() if f.stem[:2].isdigit() \
            else f.stem.replace("_", " ").strip().capitalize()
        mime = {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(f.suffix[1:].lower(), "image/jpeg")
        with open(f, "rb") as fh:
            r = _post(f"{site}/media/api/upload", {"title": title, "source": source}, files={"file": (f.name, fh, mime)},
                      token=token)
        if r.status_code != 200:
            print(f"FAILED {f.name}: {r.status_code} {r.text[:200]}")
            continue
        body = r.json()
        print(f"{'already there' if body['duplicate'] else body['status']}: {title} -> {body['url']}")
    print(f"Keep them at {site}/media (Keep all).")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze"); a.add_argument("video"); a.add_argument("work")
    p = sub.add_parser("peek"); p.add_argument("work"); p.add_argument("frames"); p.add_argument("--box")
    c = sub.add_parser("cutout"); c.add_argument("work"); c.add_argument("--lo", type=float, default=14)
    c.add_argument("--hi", type=float, default=60)
    b = sub.add_parser("build"); b.add_argument("work"); b.add_argument("out")
    b.add_argument("--still", action="append", default=[]); b.add_argument("--anim", action="append", default=[])
    u = sub.add_parser("upload"); u.add_argument("files", nargs="+"); u.add_argument("--source", required=True)
    u.add_argument("--site", default="https://askdieter.ai")
    args = ap.parse_args()
    if args.cmd == "analyze":
        analyze(args.video, args.work)
    elif args.cmd == "peek":
        peek(args.work, args.frames.split(","), tuple(map(int, args.box.split(","))) if args.box else None)
    elif args.cmd == "cutout":
        cutout(args.work, args.lo, args.hi)
    elif args.cmd == "build":
        build(args.work, args.out, args.still, args.anim)
    elif args.cmd == "upload":
        upload(args.files, args.source, args.site.rstrip("/"))


if __name__ == "__main__":
    main()

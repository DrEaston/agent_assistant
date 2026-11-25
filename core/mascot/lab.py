"""The clips, lined up and checked: what /media/lab shows and what the player draws (docs/mascot_plan.md).

Built from his kept Media pictures the first time the lab asks, then kept in memory for this server (keyed by the
pictures' ids, so a replaced picture is rebuilt).
"""

import json
import threading
from pathlib import Path

from . import frames as fr

MANIFEST = Path(__file__).parent / "clips.json"
_cache, _lock = {}, threading.Lock()


def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _moved(box, scale, dx, dy):
    return (box[0] * scale + dx, box[1] * scale + dy, box[2] * scale + dx, box[3] * scale + dy)


def _fit_all(spec, ends_by_name):
    """How to place each clip on the shared canvas, starting from the reference clip and spreading out by pose.
    ends_by_name: name -> (first frame box, last frame box) in the clip's own pixels.
    Returns name -> ((scale, dx, dy), how it was fitted)."""
    by_name = {c["name"]: c for c in spec["clips"] if c["name"] in ends_by_name}
    fits, poses = {}, {}  # poses: pose name -> Dieter's box in that pose, on the canvas
    ref = spec["reference"]
    if ref in by_name and ends_by_name[ref][0]:
        fits[ref] = (fr.first_fit(ends_by_name[ref][0]), "reference")
    progress = True
    while progress:
        for name, (fit, _) in fits.items():
            c, (first, last) = by_name[name], ends_by_name[name]
            poses.setdefault(c["starts"], _moved(first, *fit))
            if last:
                poses.setdefault(c["ends"], _moved(last, *fit))
        progress = False
        for name, c in by_name.items():
            if name in fits:
                continue
            for end, index, how in (("starts", 0, "start"), ("ends", 1, "end")):
                source = ends_by_name[name][index]
                if c[end] in poses and source:
                    fits[name] = (fr.fit(source, poses[c[end]]), f"{how} matched to {c[end]}")
                    progress = True
                    break
    return fits


def _build_clip(c, data, fit):
    """One clip, a frame at a time: its sprite sheet, its checks, and its first and last frames (for joins)."""
    count = sum(1 for _ in fr.each(data))
    sheet = fr.Sheet(count)
    first = previous = None
    steps, anchors, ms = [], [], []
    for image, duration in fr.each(data):
        placed = fr.place_one(image, *fit)
        sheet.add(placed)
        anchors.append(fr.anchor(placed))
        ms.append(duration)
        if previous is not None:
            steps.append(fr.seam(previous, placed))
        else:
            first = placed
        previous = placed
    last = previous
    drift = max((abs(x - anchors[0][0]) + abs(y - anchors[0][1]) for x, y in anchors), default=0)
    out = {"frames": count, "columns": sheet.columns, "per_page": fr.Sheet.PAGE, "ms": round(sum(ms) / len(ms)) if ms else 100,
           "seconds": round(sum(ms) / 1000, 1), "biggest_step": max(steps, default=0.0),
           "biggest_step_at": steps.index(max(steps)) + 1 if steps else 0, "drift_px": round(drift),
           "loop_seam": None, "problems": []}
    if c["play"] == "loop" and count > 1:
        out["loop_seam"] = fr.seam(last, first)
        if fr.verdict(out["loop_seam"]) == "jump":
            out["problems"].append(f"jumps when it loops (last frame to first: {out['loop_seam']})")
    if fr.verdict(out["biggest_step"]) == "jump":
        out["problems"].append(f"jumps between frames {out['biggest_step_at']} and {out['biggest_step_at'] + 1} "
                               f"({out['biggest_step']})")
    if c["starts"] == c["ends"] == "alert" and c["play"] == "loop" and drift > 6:
        out["problems"].append(f"his paws move {round(drift)} px; a resting loop should keep them still")
    return out, sheet.webp(), first, last


_building = threading.Lock()  # one build at a time: a second visitor waits for the first build, not a second one


def build(store):
    """Everything the lab and the player need: clips (with checks), joins between them, and sprite sheets.
    store: the Media store, as the signed-in user. Works through one clip at a time to keep memory small."""
    spec = manifest()
    key = (store_user(store), tuple(c["media_id"] for c in spec["clips"]))
    with _building:
        with _lock:
            if key in _cache:
                return _cache[key]
        missing, ends_by_name = [], {}
        for c in spec["clips"]:
            try:
                if store.item(c["media_id"])["status"] != "kept":
                    missing.append({"name": c["name"], "why": "not kept yet (waiting for your OK in Media)"})
                    continue
                first, last, _ = fr.ends(store.read(c["media_id"])[0])
                ends_by_name[c["name"]] = (fr.box(first) if first else None, fr.box(last) if last else None)
            except Exception as exc:  # a missing or unreadable picture shouldn't hide the rest
                missing.append({"name": c["name"], "why": str(exc) or "couldn't read it"})
        fits = _fit_all(spec, ends_by_name)
        clips, sheets, edges = [], {}, {}
        for c in spec["clips"]:
            if c["name"] not in fits:
                if c["name"] in ends_by_name:
                    missing.append({"name": c["name"], "why": "couldn't be lined up with the others (no matching pose)"})
                continue
            fit, how = fits[c["name"]]
            try:
                report, sheets[c["name"]], first, last = _build_clip(c, store.read(c["media_id"])[0], fit)
            except Exception as exc:
                print(f"Warning: animation lab couldn't build {c['name']}: {exc!r}")
                missing.append({"name": c["name"], "why": "couldn't be built"})
                continue
            edges[c["name"]] = (fr.small_frame(first), fr.small_frame(last))
            clips.append({**{k: c[k] for k in ("name", "title", "media_id", "play", "starts", "ends", "role")},
                          "fitted": how, **report})
        joins = []
        for a in clips:
            for b in clips:
                if a["ends"] == b["starts"] and a["name"] != b["name"]:
                    score = fr.seam(edges[a["name"]][1], edges[b["name"]][0])
                    joins.append({"from": a["name"], "to": b["name"], "pose": a["ends"], "seam": score,
                                  "verdict": fr.verdict(score)})
        joins.sort(key=lambda j: -j["seam"])
        result = {"clips": clips, "joins": joins, "missing": missing, "canvas": fr.CANVAS,
                  "limits": {"ok": fr.OK, "look": fr.LOOK}, "sheets": sheets}
        with _lock:
            _cache.clear()  # only the latest build is kept
            _cache[key] = result
        return result


def store_user(store):
    try:
        return store._user()
    except Exception:
        return None


def forget():
    with _lock:
        _cache.clear()

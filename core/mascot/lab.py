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


def _fit_all(spec, raw):
    """Place every clip on the shared canvas, starting from the reference clip and spreading out by pose.
    raw: name -> decoded frames. Returns name -> (placed frames, how it was fitted)."""
    by_name = {c["name"]: c for c in spec["clips"] if c["name"] in raw}
    placed, poses = {}, {}  # poses: pose name -> Dieter's box in that pose, on the canvas
    ref = spec["reference"]
    if ref in by_name and fr.box(raw[ref][0][0]):
        placed[ref] = (fr.place(raw[ref], *fr.first_fit(fr.box(raw[ref][0][0]))), "reference")
    progress = True
    while progress:
        for name, (frames, _) in placed.items():
            c = by_name[name]
            poses.setdefault(c["starts"], fr.box(frames[0][0]))
            poses.setdefault(c["ends"], fr.box(frames[-1][0]))
        progress = False
        for name, c in by_name.items():
            if name in placed:
                continue
            for end, index, how in (("starts", 0, "start"), ("ends", -1, "end")):
                source = fr.box(raw[name][index][0])
                if c[end] in poses and source and poses[c[end]]:
                    placed[name] = (fr.place(raw[name], *fr.fit(source, poses[c[end]])),
                                    f"{how} matched to {c[end]}")
                    progress = True
                    break
    return placed


def _clip_report(c, frames):
    images = [f[0] for f in frames]
    steps = [fr.seam(a, b) for a, b in zip(images, images[1:])]
    anchors = [fr.anchor(i) for i in images]
    drift = max((abs(x - anchors[0][0]) + abs(y - anchors[0][1]) for x, y in anchors), default=0)
    ms = [f[1] for f in frames]
    out = {"frames": len(frames), "ms": round(sum(ms) / len(ms)) if ms else 100, "seconds": round(sum(ms) / 1000, 1),
           "biggest_step": max(steps, default=0.0), "biggest_step_at": steps.index(max(steps)) + 1 if steps else 0,
           "drift_px": round(drift), "loop_seam": None, "problems": []}
    if c["play"] == "loop" and len(images) > 1:
        out["loop_seam"] = fr.seam(images[-1], images[0])
        if fr.verdict(out["loop_seam"]) == "jump":
            out["problems"].append(f"jumps when it loops (last frame to first: {out['loop_seam']})")
    if fr.verdict(out["biggest_step"]) == "jump":
        out["problems"].append(f"jumps between frames {out['biggest_step_at']} and {out['biggest_step_at'] + 1} "
                               f"({out['biggest_step']})")
    if c["starts"] == c["ends"] == "alert" and c["play"] == "loop" and drift > 6:
        out["problems"].append(f"his paws move {round(drift)} px; a resting loop should keep them still")
    return out


def build(store):
    """Everything the lab and the player need: clips (with checks), joins between them, and sprite sheets.
    store: the Media store, as the signed-in user."""
    spec = manifest()
    key = tuple(c["media_id"] for c in spec["clips"])
    with _lock:
        hit = _cache.get((store_user(store), key))
        if hit:
            return hit
    raw, missing = {}, []
    for c in spec["clips"]:
        try:
            data, _ = store.read(c["media_id"])
            item = store.item(c["media_id"])
            if item["status"] != "kept":
                missing.append({"name": c["name"], "why": "not kept yet (waiting for your OK in Media)"})
                continue
            raw[c["name"]] = fr.decode(data)
        except Exception as exc:  # a missing or unreadable picture shouldn't hide the rest
            missing.append({"name": c["name"], "why": str(exc) or "couldn't read it"})
    placed = _fit_all(spec, raw)
    clips, sheets = [], {}
    for c in spec["clips"]:
        if c["name"] not in placed:
            if c["name"] in raw:
                missing.append({"name": c["name"], "why": "couldn't be lined up with the others (no matching pose)"})
            continue
        frames, how = placed[c["name"]]
        report = _clip_report(c, frames)
        clips.append({**{k: c[k] for k in ("name", "title", "media_id", "play", "starts", "ends", "role")},
                      "fitted": how, "columns": min(8, len(frames)), **report})
        sheets[c["name"]] = (frames, None)
    joins = []
    for a in clips:
        for b in clips:
            if a["ends"] == b["starts"] and a["name"] != b["name"]:
                score = fr.seam(sheets[a["name"]][0][-1][0], sheets[b["name"]][0][0][0])
                joins.append({"from": a["name"], "to": b["name"], "pose": a["ends"], "seam": score,
                              "verdict": fr.verdict(score)})
    joins.sort(key=lambda j: -j["seam"])
    result = {"clips": clips, "joins": joins, "missing": missing, "canvas": fr.CANVAS,
              "limits": {"ok": fr.OK, "look": fr.LOOK},
              "sheets": {name: fr.sheet(frames) for name, (frames, _) in sheets.items()}}
    with _lock:
        _cache[(store_user(store), key)] = result
    return result


def store_user(store):
    try:
        return store._user()
    except Exception:
        return None


def forget():
    with _lock:
        _cache.clear()

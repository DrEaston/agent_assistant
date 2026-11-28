"""Reading FIT files (Garmin's binary activity format, which COROS watches write) without a library.

Only what Trainer needs: the second-by-second records (time, position, distance, speed, heart rate), the session
summary and the local time offset. Follows the FIT protocol: a file header, then definition messages (which say how
the following data messages of a local type are laid out) and data messages, including compressed-timestamp
headers and developer fields (skipped).
"""

import struct
from datetime import datetime, timedelta, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SEMICIRCLE = 180 / 2 ** 31

# base type number (low 5 bits) -> (struct code, size, invalid value)
BASE = {0: ("B", 1, 0xFF), 1: ("b", 1, 0x7F), 2: ("B", 1, 0xFF), 3: ("h", 2, 0x7FFF), 4: ("H", 2, 0xFFFF),
        5: ("i", 4, 0x7FFFFFFF), 6: ("I", 4, 0xFFFFFFFF), 8: ("f", 4, None), 9: ("d", 8, None),
        10: ("B", 1, 0), 11: ("H", 2, 0), 12: ("I", 4, 0), 13: ("B", 1, None), 14: ("q", 8, 0x7FFFFFFFFFFFFFFF),
        15: ("Q", 8, 0xFFFFFFFFFFFFFFFF), 16: ("Q", 8, 0)}

RECORD, SESSION, ACTIVITY, LAP = 20, 18, 34, 19
WANTED = {RECORD, SESSION, ACTIVITY, LAP}


class FitError(Exception):
    pass


def _value(raw, base_type, size, little):
    code, width, invalid = BASE.get(base_type & 0x1F, (None, None, None))
    if code is None or size != width:
        return None  # strings, byte arrays and arrays aren't needed here
    value = struct.unpack(("<" if little else ">") + code, raw)[0]
    return None if invalid is not None and value == invalid else value


def messages(data):
    """[(global message number, {field number: value})] for the messages Trainer reads."""
    if len(data) < 12 or data[8:12] != b".FIT":
        raise FitError("That isn't a FIT file.")
    header = data[0]
    size = struct.unpack("<I", data[4:8])[0]
    end, i = min(len(data), header + size), header
    defs, out, last_time = {}, [], 0
    while i < end:
        h = data[i]
        i += 1
        if h & 0x80:   # compressed timestamp header: a data message with a 5-bit time offset
            local, offset = (h >> 5) & 0x03, h & 0x1F
            last_time = (last_time & ~0x1F) + offset + (0x20 if offset < (last_time & 0x1F) else 0)
            stamp = last_time
        elif h & 0x40:  # definition message
            local = h & 0x0F
            little = data[i + 1] == 0
            number = struct.unpack("<H" if little else ">H", data[i + 2:i + 4])[0]
            count = data[i + 4]
            i += 5
            fields = [(data[i + 3 * k], data[i + 3 * k + 1], data[i + 3 * k + 2]) for k in range(count)]
            i += 3 * count
            dev = 0
            if h & 0x20:
                dev_count = data[i]
                i += 1
                dev = sum(data[i + 3 * k + 1] for k in range(dev_count))
                i += 3 * dev_count
            defs[local] = (number, little, fields, dev)
            continue
        else:
            local, stamp = h & 0x0F, None
        if local not in defs:
            raise FitError("A data message came before its definition.")
        number, little, fields, dev = defs[local]
        values = {}
        for field, fsize, base_type in fields:
            if number in WANTED:
                values[field] = _value(data[i:i + fsize], base_type, fsize, little)
            i += fsize
        i += dev
        if values.get(253) is not None:
            last_time = values[253]
        elif stamp is not None:
            values[253] = stamp
        if number in WANTED:
            out.append((number, values))
    return out


def _when(value):
    return FIT_EPOCH + timedelta(seconds=value) if value is not None else None


def read(data):
    """{start (UTC datetime), local_offset (s), sport, distance_m, elapsed_s, moving_s, avg_hr, max_hr, ascent_m,
    records: [{t, lat, lon, d, v, hr}]} from a FIT file."""
    records, session, offset = [], {}, None
    for number, v in messages(data):
        if number == RECORD and v.get(253) is not None:
            speed = v.get(73) if v.get(73) is not None else v.get(6)
            records.append({"t": v[253], "d": v[5] / 100 if v.get(5) is not None else None,
                            "v": speed / 1000 if speed is not None else None, "hr": v.get(3),
                            "lat": v[0] * SEMICIRCLE if v.get(0) is not None else None,
                            "lon": v[1] * SEMICIRCLE if v.get(1) is not None else None})
        elif number == SESSION and not session:
            session = v
        elif number == ACTIVITY and v.get(5) is not None and v.get(253) is not None:
            offset = v[5] - v[253]
    if not records and not session:
        raise FitError("The FIT file has no activity in it.")
    start = session.get(2) if session.get(2) is not None else (records[0]["t"] if records else None)
    scale = lambda key, by: session[key] / by if session.get(key) is not None else None
    return {"start": _when(start), "local_offset": offset or 0, "sport": session.get(5),
            "distance_m": scale(9, 100) or (records[-1]["d"] if records and records[-1]["d"] else None),
            "elapsed_s": scale(7, 1000), "moving_s": scale(8, 1000), "avg_hr": session.get(16),
            "max_hr": session.get(17), "ascent_m": session.get(22), "records": records}


def streams(run):
    """A read FIT file as Strava-style streams ({key: {"data": [...]}}), so speed.pack() keeps it the same way."""
    records = run["records"]
    if not records:
        return {}
    t0 = records[0]["t"]
    speeds, last_d, last_t = [], None, None
    for r in records:
        v = r["v"]
        if v is None and r["d"] is not None and last_d is not None and r["t"] > last_t:
            v = (r["d"] - last_d) / (r["t"] - last_t)
        speeds.append(v)
        if r["d"] is not None:
            last_d, last_t = r["d"], r["t"]
    return {"time": {"data": [r["t"] - t0 for r in records]},
            "velocity_smooth": {"data": _smooth(speeds)},
            "distance": {"data": [r["d"] for r in records]},
            "heartrate": {"data": [r["hr"] for r in records]} if any(r["hr"] for r in records) else {},
            "latlng": {"data": [[r["lat"], r["lon"]] if r["lat"] is not None else None for r in records]}
            if any(r["lat"] is not None for r in records) else {}}


def _smooth(speeds, width=3):
    """A light centred average (Strava's velocity_smooth is smoothed too), ignoring gaps."""
    out = []
    for i in range(len(speeds)):
        window = [s for s in speeds[max(0, i - width // 2):i + width // 2 + 1] if s is not None]
        out.append(sum(window) / len(window) if window else None)
    return out

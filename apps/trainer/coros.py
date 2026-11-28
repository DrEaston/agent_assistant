"""COROS: new runs come straight from the watch's maker instead of Strava (docs/coros_plan.md).

COROS runs an official MCP server (https://mcp.coros.com/mcp, by region) that any app may connect to with the
athlete's own COROS sign-in: "self-service, no application required", single-user, OAuth 2.0. Dieter is an ordinary
OAuth client of it:

- Discovery: the server's /.well-known/oauth-protected-resource names its authorization server, whose metadata
  gives the authorize, token and registration endpoints (OAuth 2.0 + PKCE S256, scopes openid mcp.tools
  offline_access).
- Dieter registers itself once per callback address (dynamic client registration, RFC 7591; no client secret).
- Connect COROS: PKCE sign-in at COROS, back to /apps/trainer/coros/callback, tokens kept per person and refreshed.
- MCP over streamable HTTP: initialize, then tools/call. querySportRecords lists activities by date;
  downloadActivityFitFiles / queryActivityFitFileDownloadUrls give each activity's FIT file (every second of the
  run). COROS allows 50 FIT files per person per 24 hours, so the history comes in over several days.

From the moment he connects, COROS is where new activities come from: each pass lists the last two weeks, and
every activity that started after he connected is fetched as a FIT file, read (fit.py) and put in his log
(source 'coros') with its speed analysis, the same as a Strava run. Strava stops adding new activities then
(strava.Sync.handle); everything already in the log stays. FIT files are kept as files (core/media's Blobs: a local
folder plus Cloud Storage), not in the database. "Test with my latest run" reads his newest COROS run without
adding it and compares it with the same run from Strava.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from . import fit

MCP_URL = os.getenv("COROS_MCP_URL", "https://mcpus.coros.com/mcp")
SCOPES = "openid mcp.tools offline_access"
PROTOCOL = "2025-06-18"
FIT_PER_DAY = 50          # COROS: 50 FIT files per person per 24 hours (shared by both FIT tools)
FIT_PER_PASS = 5          # fetched per pass, so a visit stays quick
RECENT_DAYS = 14          # each pass lists this far back (querySportRecords has no paging)
PASS_EVERY = 10 * 60      # seconds between passes when Trainer or Today opens (COROS has no push)
FIT_SPORTS = {1: "Run", 2: "Ride", 5: "Swim", 10: "WeightTraining", 11: "Walk", 17: "Hike"}  # FIT sport -> type

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tr_coros_sync (key TEXT PRIMARY KEY, value TEXT DEFAULT '')",
    """CREATE TABLE IF NOT EXISTS tr_coros_tokens (
        user_id INTEGER PRIMARY KEY, client_id TEXT, redirect_uri TEXT, access_token TEXT, refresh_token TEXT,
        expires_at REAL DEFAULT 0, connected_at TEXT DEFAULT '', error TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS tr_coros_pending (
        state TEXT PRIMARY KEY, user_id INTEGER, verifier TEXT, redirect_uri TEXT, client_id TEXT,
        created_at REAL)""",
    """CREATE TABLE IF NOT EXISTS tr_coros_activities (
        user_id INTEGER, label_id TEXT, sport_type INTEGER, name TEXT DEFAULT '', day TEXT DEFAULT '',
        started_at TEXT DEFAULT '', distance_m REAL, seconds REAL, raw_json TEXT DEFAULT '{}',
        fit_key TEXT DEFAULT '', fit_bytes INTEGER DEFAULT 0, fit_at TEXT DEFAULT '', fit_error TEXT DEFAULT '',
        found_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, label_id))""",
    "CREATE INDEX IF NOT EXISTS tr_coros_activities_fit ON tr_coros_activities (user_id, fit_at, day)",
)


class CorosError(Exception):
    pass


def _origin(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def http(url, method="GET", form=None, body=None, headers=None, raw=False):
    """One request. form: urlencoded fields; body: a JSON-able object. Returns (status, headers, text or bytes)."""
    data, sent = None, {"Accept": "application/json"}
    sent.update(headers or {})
    if form is not None:
        data = urlencode(form).encode()
        sent["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=sent, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read()
            return response.status, dict(response.headers), payload if raw else payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise CorosError(f"Couldn't reach COROS: {exc.reason}") from None


def _json(text, what):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        raise CorosError(f"COROS sent something that isn't JSON for {what}: {str(text)[:200]}") from None


def sse_messages(text):
    """JSON-RPC messages from a streamable-HTTP reply: plain JSON, or server-sent events ("data: {...}")."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    out, chunk = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            chunk.append(line[5:].strip())
        elif not line.strip() and chunk:
            try:
                out.append(json.loads("\n".join(chunk)))
            except ValueError:
                pass
            chunk = []
    return out


def tool_payload(result):
    """A tools/call result as data: structuredContent, else each text item parsed as JSON where it is JSON."""
    if not isinstance(result, dict):
        return result
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    out = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                out.append(json.loads(item.get("text") or ""))
            except ValueError:
                out.append(item.get("text"))
        else:
            out.append(item)
    return out[0] if len(out) == 1 else out


def walk(value):
    """Every dict inside a nested JSON value."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def _pick(d, *names):
    for name in names:
        if d.get(name) not in (None, ""):
            return d[name]
    return None


LABEL_TEXT = re.compile(r"label_?id[\"']?\s*[:=]\s*[\"']?(\d{6,})", re.I)
SPORT_TEXT = re.compile(r"sport_?type[\"']?\s*[:=]\s*[\"']?(\d+)", re.I)
DATE_TEXT = re.compile(r"(20\d\d-?\d\d-?\d\d)")


def records(payload):
    """Activities in a querySportRecords answer: any object with a labelId (COROS's activity id). When COROS answers
    in text rather than JSON, labelIds (with the sportType and date near each) are read out of the text."""
    if isinstance(payload, str):
        out = []
        found = list(LABEL_TEXT.finditer(payload))
        for k, m in enumerate(found):
            line = payload[payload.rfind("\n", 0, m.start()) + 1:(payload.find("\n", m.end()) + 1 or len(payload) + 1) - 1]
            window = payload[found[k - 1].end() if k else 0:found[k + 1].start() if k + 1 < len(found) else None]
            sport = SPORT_TEXT.search(line) or SPORT_TEXT.search(window)
            day = DATE_TEXT.search(line) or DATE_TEXT.search(window)
            chunk = line if SPORT_TEXT.search(line) or DATE_TEXT.search(line) else window
            out.append({"label_id": m.group(1), "sport_type": int(sport.group(1)) if sport else None, "name": "",
                        "start": day.group(1).replace("-", "") if day else None, "distance": None, "seconds": None,
                        "raw": {"text": chunk[:2000]}})
        return out
    out, seen = [], set()
    for d in walk(payload):
        label = _pick(d, "labelId", "label_id", "labelID")
        if label is None or str(label) in seen:
            continue
        seen.add(str(label))
        sport = _pick(d, "sportType", "sport_type", "mode")
        start = _pick(d, "startTime", "start_time", "startTimestamp", "date", "startDate")
        out.append({"label_id": str(label), "sport_type": int(sport) if str(sport or "").isdigit() else None,
                    "name": str(_pick(d, "name", "title", "activityName") or ""), "start": start,
                    "distance": _pick(d, "distance", "distanceM", "totalDistance"),
                    "seconds": _pick(d, "totalTime", "duration", "workoutTime", "durationSeconds"), "raw": d})
    return out


def _start_text(start):
    """COROS dates arrive as yyyyMMdd, unix seconds or ISO text: (day 'YYYY-MM-DD', started_at text)."""
    if start is None:
        return "", ""
    text = str(start)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}", ""
    if re.fullmatch(r"\d{9,13}", text):
        seconds = int(text) / (1000 if len(text) > 10 else 1)
        stamp = datetime.utcfromtimestamp(seconds).isoformat(timespec="seconds")
        return stamp[:10], stamp
    return text[:10], text[:19]


def fit_files(result):
    """FIT files in a download answer: [(name, bytes or None, url or None)]. Embedded resources first; links as
    the fallback."""
    out = []
    for item in (result or {}).get("content") or []:
        resource = item.get("resource") if item.get("type") == "resource" else None
        if resource and resource.get("blob"):
            out.append((resource.get("uri") or "", base64.b64decode(resource["blob"]), None))
        elif item.get("type") == "resource_link" and item.get("uri"):
            out.append((item.get("name") or item["uri"], None, item["uri"]))
    if out:
        return out
    for d in walk(tool_payload(result)):
        for key, value in d.items():
            if isinstance(value, str) and value.startswith("http") and ("fit" in key.lower() or "url" in key.lower()
                                                                        or ".fit" in value.lower()):
                out.append((str(d.get("fileName") or d.get("name") or value), None, value))
    return out


class Coros:
    """`call(url, method, form, body, headers, raw)` does HTTP (swapped in tests). `blobs` keeps FIT files."""

    def __init__(self, db, blobs=None, call=None, mcp_url=MCP_URL):
        self.db, self.blobs, self.mcp_url = db, blobs, mcp_url
        self.call = call or http
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            for statement in SCHEMA:
                conn.execute(statement)
            conn.commit()
            self._ready = True
        return conn

    def get(self, key, default=""):
        row = self._conn().execute("SELECT value FROM tr_coros_sync WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def put(self, key, value):
        conn = self._conn()
        conn.execute("INSERT INTO tr_coros_sync (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (key, str(value)))
        conn.commit()

    # ------------------------------------------------------------ OAuth

    def server(self):
        """The authorization server's metadata (cached for a day)."""
        cached = self.get("server_json")
        if cached and time.time() - float(self.get("server_at") or 0) < 86400:
            return json.loads(cached)
        status, _, text = self.call(_origin(self.mcp_url) + "/.well-known/oauth-protected-resource")
        resource = _json(text, "its sign-in details") if status == 200 else {}
        issuer = (resource.get("authorization_servers") or [_origin(self.mcp_url)])[0]
        status, _, text = self.call(issuer.rstrip("/") + "/.well-known/oauth-authorization-server")
        if status != 200:
            raise CorosError(f"COROS sign-in details aren't available ({status}).")
        meta = _json(text, "its sign-in server")
        meta["resource"] = resource.get("resource") or self.mcp_url
        self.put("server_json", json.dumps(meta))
        self.put("server_at", time.time())
        return meta

    def client_id(self, redirect_uri):
        """Dieter's client id at COROS for this callback address, registered once (RFC 7591)."""
        key = "client_" + hashlib.sha256(redirect_uri.encode()).hexdigest()[:16]
        if self.get(key):
            return self.get(key)
        meta = self.server()
        if not meta.get("registration_endpoint"):
            raise CorosError("COROS doesn't offer app registration.")
        status, _, text = self.call(meta["registration_endpoint"], "POST", body={
            "client_name": "Dieter Trainer (askdieter.ai)", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": SCOPES})
        if status not in (200, 201):
            raise CorosError(f"COROS didn't register Dieter ({status}): {text[:300]}")
        client = _json(text, "registration").get("client_id")
        if not client:
            raise CorosError("COROS registered Dieter but sent no client id.")
        self.put(key, client)
        return client

    def start(self, user_id, redirect_uri):
        """The COROS sign-in address to send him to (PKCE; the state ties the reply to him)."""
        meta, client = self.server(), self.client_id(redirect_uri)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        conn = self._conn()
        conn.execute("DELETE FROM tr_coros_pending WHERE created_at < ?", (time.time() - 3600,))
        conn.execute("INSERT INTO tr_coros_pending (state, user_id, verifier, redirect_uri, client_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (state, user_id, verifier, redirect_uri, client, time.time()))
        conn.commit()
        return meta["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": client, "redirect_uri": redirect_uri, "scope": SCOPES,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": meta.get("resource") or self.mcp_url})

    def finish(self, user_id, state, code):
        conn = self._conn()
        row = conn.execute("SELECT * FROM tr_coros_pending WHERE state = ?", (state or "",)).fetchone()
        if not row or row["user_id"] != user_id or time.time() - row["created_at"] > 3600:
            raise CorosError("That COROS sign-in expired. Try Connect COROS again.")
        conn.execute("DELETE FROM tr_coros_pending WHERE state = ?", (state,))
        meta = self.server()
        tokens = self._token_call(meta, {"grant_type": "authorization_code", "code": code,
                                         "redirect_uri": row["redirect_uri"], "client_id": row["client_id"],
                                         "code_verifier": row["verifier"],
                                         "resource": meta.get("resource") or self.mcp_url})
        conn.execute(
            "INSERT INTO tr_coros_tokens (user_id, client_id, redirect_uri, access_token, refresh_token, expires_at, "
            "connected_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, '') ON CONFLICT(user_id) DO UPDATE SET "
            "client_id = excluded.client_id, redirect_uri = excluded.redirect_uri, access_token = excluded.access_token, "
            "refresh_token = excluded.refresh_token, expires_at = excluded.expires_at, "
            "connected_at = excluded.connected_at, error = ''",
            (user_id, row["client_id"], row["redirect_uri"], tokens["access_token"], tokens.get("refresh_token", ""),
             time.time() + float(tokens.get("expires_in") or 3600), datetime.utcnow().isoformat(timespec="seconds")))
        conn.commit()
        if not self.get(f"since_{user_id}"):   # from now on, new activities come from COROS
            self.put(f"since_{user_id}", datetime.utcnow().isoformat(timespec="seconds"))

    def since(self, user_id):
        """When COROS took over from Strava for him (UTC), or "" when it hasn't."""
        return self.get(f"since_{user_id}") if self.connected(user_id) else ""

    def _token_call(self, meta, form):
        status, _, text = self.call(meta["token_endpoint"], "POST", form=form)
        tokens = _json(text, "the sign-in") if text else {}
        if status != 200 or not tokens.get("access_token"):
            raise CorosError(f"COROS didn't accept the sign-in ({status}): {text[:300]}")
        return tokens

    def connected(self, user_id):
        row = self._conn().execute("SELECT * FROM tr_coros_tokens WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row and (row["refresh_token"] or row["access_token"]) else None

    def disconnect(self, user_id):
        conn = self._conn()
        conn.execute("DELETE FROM tr_coros_tokens WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM tr_coros_sync WHERE key = ?", (f"since_{user_id}",))  # Strava brings them in again
        conn.commit()

    def _access(self, user_id, force=False):
        row = self.connected(user_id)
        if not row:
            raise CorosError("COROS isn't connected.")
        if not force and row["access_token"] and row["expires_at"] > time.time() + 60:
            return row["access_token"]
        if not row["refresh_token"]:
            raise CorosError("The COROS sign-in ran out. Connect COROS again.")
        meta = self.server()
        tokens = self._token_call(meta, {"grant_type": "refresh_token", "refresh_token": row["refresh_token"],
                                         "client_id": row["client_id"]})
        conn = self._conn()
        conn.execute("UPDATE tr_coros_tokens SET access_token = ?, refresh_token = ?, expires_at = ?, error = '' "
                     "WHERE user_id = ?", (tokens["access_token"], tokens.get("refresh_token") or row["refresh_token"],
                                            time.time() + float(tokens.get("expires_in") or 3600), user_id))
        conn.commit()
        return tokens["access_token"]

    # ------------------------------------------------------------ MCP

    def _post(self, token, message, session=""):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if session:
            headers["Mcp-Session-Id"] = session
        return self.call(self.mcp_url, "POST", body=message, headers=headers)

    def tool(self, user_id, name, arguments):
        """tools/call on COROS's MCP server, as this person. Returns the raw result."""
        for attempt in (0, 1):
            token = self._access(user_id, force=attempt == 1)
            status, headers, text = self._post(token, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "dieter-trainer", "version": "1"}}})
            if status == 401 and attempt == 0:
                continue
            if status != 200:
                raise CorosError(f"COROS's server didn't start a session ({status}): {text[:300]}")
            session = next((v for k, v in headers.items() if k.lower() == "mcp-session-id"), "")
            self._post(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
            status, _, text = self._post(token, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": name, "arguments": arguments}}, session)
            if status == 401 and attempt == 0:
                continue
            if status != 200:
                raise CorosError(f"COROS's {name} failed ({status}): {text[:300]}")
            for message in sse_messages(text):
                if message.get("id") == 2:
                    if message.get("error"):
                        raise CorosError(f"COROS's {name}: {message['error'].get('message', message['error'])}")
                    result = message.get("result") or {}
                    if result.get("isError"):
                        raise CorosError(f"COROS's {name}: {json.dumps(tool_payload(result))[:300]}")
                    return result
            raise CorosError(f"COROS's {name} sent no answer.")
        raise CorosError("COROS didn't accept Dieter's sign-in.")

    # ------------------------------------------------------------ activities and FIT files

    def list_window(self, user_id, start, end):
        """Store the activities between two dates. Returns how many COROS listed."""
        result = self.tool(user_id, "querySportRecords", {
            "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "sportTypeCodes": [65535],
            "limit": 200})
        # What COROS sent, kept so the Log page can show it (its exact shape isn't documented)
        self.put(f"last_list_{user_id}", json.dumps(result, default=str)[:6000])
        conn, found = self._conn(), records(tool_payload(result))
        for r in found:
            day, started = _start_text(r["start"])
            conn.execute(
                "INSERT INTO tr_coros_activities (user_id, label_id, sport_type, name, day, started_at, distance_m, "
                "seconds, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, label_id) DO UPDATE SET "
                "raw_json = excluded.raw_json, name = excluded.name",
                (user_id, r["label_id"], r["sport_type"], r["name"], day, started,
                 float(r["distance"]) if str(r["distance"] or "").replace(".", "", 1).isdigit() else None,
                 float(r["seconds"]) if str(r["seconds"] or "").replace(".", "", 1).isdigit() else None,
                 json.dumps(r["raw"], default=str)[:8000]))
        conn.commit()
        return len(found)

    def used_today(self, user_id):
        since = (datetime.utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
        return self._conn().execute("SELECT COUNT(*) FROM tr_coros_activities WHERE user_id = ? AND fit_at >= ?",
                                    (user_id, since)).fetchone()[0]

    def _download(self, user_id, row):
        """Fetch, keep and read one activity's FIT file. Returns the read run (fit.read)."""
        data = self._fit(user_id, row["label_id"], row["sport_type"])
        key = f"coros/{user_id}/{row['label_id']}.fit"
        if self.blobs:
            self.blobs.put(key, data)
        conn = self._conn()
        conn.execute("UPDATE tr_coros_activities SET fit_key = ?, fit_bytes = ?, fit_at = ?, fit_error = '' "
                     "WHERE user_id = ? AND label_id = ?", (key, len(data), datetime.utcnow().isoformat(timespec="seconds"),
                                                           user_id, row["label_id"]))
        conn.commit()
        try:
            return fit.read(data)
        except fit.FitError as exc:
            raise CorosError(f"Couldn't read the FIT file: {exc}") from None

    def bring_in(self, user_id, limit=FIT_PER_PASS, today=None):
        """List the last two weeks and add every activity since the switch to his log. Returns how many were added."""
        cutoff = self.since(user_id)
        if not cutoff:
            return 0
        today = today or date.today()
        self.list_window(user_id, today - timedelta(days=RECENT_DAYS), today)
        room = min(limit, FIT_PER_DAY - self.used_today(user_id))
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM tr_coros_activities WHERE user_id = ? AND fit_at = '' AND fit_error = '' AND day >= ? "
            "ORDER BY day", (user_id, cutoff[:10])).fetchall()
        added = 0
        for row in rows[:max(0, room)]:
            try:
                run = self._download(user_id, row)
            except CorosError as exc:
                conn.execute("UPDATE tr_coros_activities SET fit_error = ?, fit_at = ? WHERE user_id = ? AND "
                             "label_id = ?", (str(exc)[:300], datetime.utcnow().isoformat(timespec="seconds"),
                                              user_id, row["label_id"]))
                conn.commit()
                continue
            if run["start"] and run["start"].replace(tzinfo=None).isoformat() < cutoff:
                continue  # recorded before the switch: that one came from Strava
            add_to_log(self.db, user_id, row["label_id"], row["name"], run)
            added += 1
        return added

    def test_latest(self, user_id, today=None):
        """Read his newest COROS run (without adding it) and compare it with the same run from Strava."""
        today = today or date.today()
        self.list_window(user_id, today - timedelta(days=RECENT_DAYS), today)
        row = self._conn().execute(   # a run if COROS's sport codes say so, else his newest activity
            "SELECT * FROM tr_coros_activities WHERE user_id = ? ORDER BY sport_type BETWEEN 100 AND 199 DESC, "
            "day DESC, started_at DESC, label_id DESC LIMIT 1", (user_id,)).fetchone()
        if not row:
            raise CorosError("No COROS runs in the last two weeks.")
        result = compare(self.db, user_id, row["label_id"], self._download(user_id, row))
        self.put(f"test_{user_id}", json.dumps(result))
        return result

    def last_test(self, user_id):
        text = self.get(f"test_{user_id}")
        return json.loads(text) if text else None

    def _fit(self, user_id, label_id, sport_type):
        args = {"labelId": label_id, "sportType": sport_type, "limit": 1}
        files = fit_files(self.tool(user_id, "downloadActivityFitFiles", args))
        for _, data, url in files:
            if data:
                return data
            if url:
                status, _, body = self.call(url, raw=True)
                if status == 200 and body:
                    return body
        raise CorosError("COROS sent no FIT file for it.")

    def status(self, user_id):
        conn = self._conn()
        row = self.connected(user_id)
        counts = conn.execute(
            "SELECT COUNT(*) AS found, SUM(fit_key != '') AS saved, SUM(fit_error != '') AS failed, "
            "SUM(sport_type BETWEEN 100 AND 199) AS runs, MIN(day) AS oldest, MAX(day) AS newest "
            "FROM tr_coros_activities WHERE user_id = ?", (user_id,)).fetchone()
        last_error = conn.execute("SELECT fit_error FROM tr_coros_activities WHERE user_id = ? AND fit_error != '' "
                                  "ORDER BY fit_at DESC LIMIT 1", (user_id,)).fetchone()
        return {"connected": bool(row), "connected_at": (row or {}).get("connected_at", ""),
                "error": (row or {}).get("error", ""), "found": counts["found"] or 0, "saved": counts["saved"] or 0,
                "failed": counts["failed"] or 0, "runs": counts["runs"] or 0, "oldest": counts["oldest"] or "",
                "newest": counts["newest"] or "", "used_today": self.used_today(user_id), "per_day": FIT_PER_DAY,
                "since": self.since(user_id), "added": self.added(user_id), "test": self.last_test(user_id),
                "last_list": self.get(f"last_list_{user_id}"),
                "last_error": last_error["fit_error"] if last_error else ""}

    def added(self, user_id):
        return self.db.connect().execute("SELECT COUNT(*) FROM trainer_imported_workouts WHERE user_id = ? AND "
                                         "source = 'coros'", (user_id,)).fetchone()[0]

    def note_error(self, user_id, message):
        conn = self._conn()
        conn.execute("UPDATE tr_coros_tokens SET error = ? WHERE user_id = ?", (message[:300], user_id))
        conn.commit()

    def sync(self, user_id, force=False):
        """One pass (at most every PASS_EVERY unless forced): bring in new activities. Errors are kept, not raised."""
        if not self.connected(user_id):
            return 0
        if not force and time.time() - float(self.get(f"pass_at_{user_id}") or 0) < PASS_EVERY:
            return 0
        self.put(f"pass_at_{user_id}", time.time())
        try:
            added = self.bring_in(user_id)
            self.note_error(user_id, "")
            return added
        except CorosError as exc:
            self.note_error(user_id, str(exc))
            return 0


def _local(run):
    start = run["start"].replace(tzinfo=None) if run["start"] else datetime.utcnow()
    return start + timedelta(seconds=run["local_offset"] or 0)


def add_to_log(db, user_id, label_id, name, run):
    """A COROS activity in his log (source 'coros'), with its speed analysis for runs."""
    from . import speed, strava

    kind = FIT_SPORTS.get(run["sport"], "Workout")
    local = _local(run)
    part = "Morning" if local.hour < 12 else "Afternoon" if local.hour < 17 else "Evening"
    title = name or f"{part} {kind if kind in ('Run', 'Ride', 'Walk') else 'Workout'}"
    moving = run["moving_s"] or run["elapsed_s"]
    db.add_trainer_imported_workout(
        source="coros", external_id=str(label_id), activity_type=kind,
        workout_category=strava.classify_strava_workout(kind, title), title=title,
        started_at=local.date().isoformat(), distance_meters=run["distance_m"], moving_time_seconds=moving,
        elapsed_time_seconds=run["elapsed_s"], elevation_gain_meters=run["ascent_m"],
        average_speed_mps=(run["distance_m"] / moving) if run["distance_m"] and moving else None,
        average_heartrate=run["avg_hr"], max_heartrate=run["max_hr"], raw={"coros_label_id": str(label_id)},
        user_id=user_id)
    sync = strava.Sync(db)
    sync._conn().execute("UPDATE trainer_imported_workouts SET started_at_local = ? WHERE source = 'coros' AND "
                         "external_id = ? AND user_id = ?", (local.isoformat(timespec="seconds"), str(label_id), user_id))
    db._commit()
    if kind == "Run" and run["records"]:
        speed._keep(db, user_id, str(label_id), local.date().isoformat(), fit.streams(run),
                    speed.easy_speed(db, user_id))


def compare(db, user_id, label_id, run):
    """His newest COROS run next to the same run from Strava (same start within 10 minutes)."""
    from . import speed

    speed.ensure(db)
    local = _local(run)
    coros_side = speed.summarize(speed.unpack(speed.pack(fit.streams(run))))
    out = {"label_id": str(label_id), "when": local.strftime("%a %b %d, %H:%M"), "rows": []}
    strava_row = db.connect().execute(
        "SELECT w.external_id, w.distance_meters, w.moving_time_seconds, w.average_heartrate, w.max_heartrate, "
        "s.series FROM trainer_imported_workouts w LEFT JOIN tr_run_speed s ON s.user_id = w.user_id AND "
        "s.external_id = w.external_id WHERE w.user_id = ? AND w.source = 'strava' AND "
        "abs(strftime('%s', w.started_at_local) - strftime('%s', ?)) < 600", (user_id, local.isoformat())).fetchone()
    strava_side = speed.summarize(speed.unpack(strava_row["series"])) if strava_row and strava_row["series"] else {}

    def efforts(side):
        return speed.summary(json.loads(side.get("efforts_json") or "[]")) if side else ""

    def miles(m):
        return f"{m / speed.METERS_PER_MILE:.2f} mi" if m else "–"

    out["strava_id"] = strava_row["external_id"] if strava_row else ""
    out["rows"] = [
        ("Distance", miles(run["distance_m"]), miles(strava_row["distance_meters"]) if strava_row else "–"),
        ("Moving time", speed.clock(run["moving_s"] or run["elapsed_s"] or 0),
         speed.clock(strava_row["moving_time_seconds"] or 0) if strava_row else "–"),
        ("Heart rate", f"{run['avg_hr'] or '–'} avg · {run['max_hr'] or '–'} max",
         f"{round(strava_row['average_heartrate'] or 0) or '–'} avg · {round(strava_row['max_heartrate'] or 0) or '–'} max"
         if strava_row else "–"),
        ("Samples", f"{len(run['records']):,}", "kept" if strava_side else "–"),
        ("What was fast", efforts(coros_side) or "nothing", efforts(strava_side) or ("nothing" if strava_side else "–")),
        ("Top speed (best 5 s)", speed.pace(coros_side.get("best5_mps")) + "/mi" if coros_side.get("best5_mps") else "–",
         speed.pace(strava_side.get("best5_mps")) + "/mi" if strava_side.get("best5_mps") else "–"),
    ]
    return out

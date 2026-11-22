"""Calls between Dieter services: who the user is, signed so it can't be forged.

The platform (the main app) signs in the user, then forwards an app's requests to that app's own service with an
`X-Dieter-User` header. Apps call back into the platform (the Kitchen recipe library, Studio problem reports) the
same way. Both sides share DIETER_INTERNAL_KEY; a header is good for five minutes.

Header value: base64url(JSON {"id", "email", "name", "ts"}) + "." + hex HMAC-SHA256 of that base64 text.
"""

import base64
import contextvars
import hashlib
import hmac
import json
import os
import time

HEADER = "X-Dieter-User"
MAX_AGE = 300
# The user a service is handling a request for (set by the service's middleware), so calls back to the
# platform can be made as that user.
current_user = contextvars.ContextVar("dieter_current_user", default=None)


def key():
    return os.getenv("DIETER_INTERNAL_KEY", "")


def sign(user, secret=None, now=None):
    secret = secret if secret is not None else key()
    if not secret:
        raise RuntimeError("DIETER_INTERNAL_KEY is not set")
    body = {"id": int(user.get("id") or 0), "email": str(user.get("email") or ""),
            "name": str(user.get("name") or ""), "ts": int(now if now is not None else time.time())}
    if user.get("signed_out") is True and not body["id"]:
        body["signed_out"] = True
    payload = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def verify(value, secret=None, now=None, allow_signed_out=False):
    """The user dict from a signed header, or None if it's missing, forged, or stale.

    allow_signed_out: also accept the main app's marker for a visitor who isn't signed in ({"signed_out": true});
    that comes back as {"id": None, ...}. Only a service's public pages should accept it."""
    secret = secret if secret is not None else key()
    if not secret or not value or "." not in value:
        return None
    payload, mac = value.rsplit(".", 1)
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    current = now if now is not None else time.time()
    if not isinstance(body, dict) or abs(current - int(body.get("ts") or 0)) > MAX_AGE:
        return None
    if not body.get("id"):
        return {"id": None, "email": "", "name": ""} if allow_signed_out and body.get("signed_out") is True else None
    return {"id": int(body["id"]), "email": body.get("email") or "", "name": body.get("name") or ""}


def call(base_url, method, path, user, payload=None, timeout=20):
    """Call another Dieter service as `user`. Returns parsed JSON; raises on a non-2xx answer."""
    import httpx

    response = httpx.request(method, base_url.rstrip("/") + path, json=payload, timeout=timeout,
                             headers={HEADER: sign(user)})
    response.raise_for_status()
    return response.json() if response.content else None

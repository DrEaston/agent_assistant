"""Forward a signed-in request to an app that runs as its own service (Thanksgiving first).

The main app keeps the address (askdieter.ai/thanksgiving/...) and the sign-in; the app's service gets the same
method, path, query and body, plus a signed X-Dieter-User header saying who it's for. Responses stream back, so
long answers (Tell Dieter thinking) and audio (/api/speak) aren't buffered.
"""

import httpx
from starlette.background import BackgroundTask
from starlette.responses import HTMLResponse, StreamingResponse

from . import internal

# Headers that describe one hop, not the request, and the user's cookie (the app never needs it).
_SKIP_REQUEST = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
                 "proxy-authorization", "content-length", "cookie", "accept-encoding", internal.HEADER.lower()}
_SKIP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
                  "content-length", "content-encoding", "set-cookie"}
_client = None


def _http():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(150, connect=10), follow_redirects=False)
    return _client


def signer_user(user):
    """The fields an app needs from the main app's user row."""
    if not user:
        return {"id": None, "signed_out": True}  # only a service's public pages accept this
    return {"id": user.get("id"), "email": user.get("email") or "",
            "name": user.get("name") or user.get("display_name") or ""}


async def forward(request, base_url, user, label="That page"):
    url = base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST}
    headers["accept-encoding"] = "identity"  # the main app gzips the answer itself
    headers[internal.HEADER] = internal.sign(signer_user(user))
    upstream = _http().build_request(request.method, url, headers=headers, content=await request.body())
    try:
        response = await _http().send(upstream, stream=True)
    except httpx.HTTPError as exc:
        print(f"Warning: {label} service unreachable: {exc}")
        return HTMLResponse(f"<p>{label} is restarting. Try again in a moment.</p>", status_code=502)
    out = {k: v for k, v in response.headers.items() if k.lower() not in _SKIP_RESPONSE}
    return StreamingResponse(response.aiter_raw(), status_code=response.status_code, headers=out,
                             background=BackgroundTask(response.aclose))

"""Getting an image's bytes from a link ChatGPT hands over (its `download_url` for a file parameter).

Only https to a public address, so a crafted link can't reach Dieter's own network; a size cap and a timeout. The
link is short-lived and signed: it's used once here and never stored or logged.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 20


def public_https(url):
    """(ok, why)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False, "not an https link"
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except OSError as exc:
        return False, f"couldn't look up the host ({exc.__class__.__name__})"
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            return False, "the link points at a private address"
    return True, ""


def download(url, max_bytes=MAX_BYTES):
    """(bytes, problem)."""
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            for _ in range(4):  # redirects are followed by hand so every hop is checked
                ok, why = public_https(url)
                if not ok:
                    return None, why
                with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = str(response.url.join(response.headers["location"]))
                        continue
                    if response.status_code != 200:
                        return None, f"the download answered {response.status_code}"
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            return None, f"the image is over {max_bytes // (1024 * 1024)} MB"
                    return bytes(data), None
            return None, "the download redirected too many times"
    except httpx.HTTPError as exc:
        return None, f"the download failed ({exc.__class__.__name__})"

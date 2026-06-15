"""Reusable approve/edit/cancel helpers for Dieter write workflows."""

import hashlib
import hmac
import json


def confirmation_token(content, page_url, action_kind, user_id=0, secret=""):
    """Create a tamper-resistant token for one proposed write."""
    payload = json.dumps(
        {
            "user_id": user_id or 0,
            "content": content or "",
            "page_url": page_url or "",
            "action_kind": action_kind or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    secret_bytes = (secret or "dieter-local-secret").encode("utf-8")
    signature = hmac.new(secret_bytes, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{signature}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def is_confirmed(content, page_url, action_kind, provided_token, user_id=0, secret=""):
    """Return true when the provided token confirms this exact proposed write."""
    expected = confirmation_token(
        content,
        page_url,
        action_kind,
        user_id=user_id,
        secret=secret,
    )
    return bool(provided_token and hmac.compare_digest(provided_token, expected))


def preview_response(content, page_url, action_kind, action_plan, user_id=0, secret=""):
    """Return the common Ask Dieter preview response shape."""
    token = confirmation_token(
        content,
        page_url,
        action_kind,
        user_id=user_id,
        secret=secret,
    )
    return {
        "assistant_message": action_plan,
        "action_plan": action_plan,
        "changed_fields": [],
        "needs_confirmation": True,
        "confirmation_token": token,
        "confirmation_action": action_kind,
    }

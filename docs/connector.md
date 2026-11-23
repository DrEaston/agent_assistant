# Dieter in Claude (the connector)

Claude (the Claude app on a phone, claude.ai, Claude Desktop, Claude Code) can use Dieter as a tool: read and
change shopping lists, recipes and the Thanksgiving plan, as the signed-in person. The data stays in Dieter;
nothing goes through git.

## Connecting (once, on claude.ai or Claude Desktop)

1. Settings → Connectors → Add custom connector. Name: Dieter. URL: `https://askdieter.ai/mcp`.
2. Claude opens Dieter's sign-in, then "Let Claude use your Dieter?". Tap Allow.
3. It now works in every Claude app on that account, including the phone (connectors can only be *added* on
   the web or desktop).

Optional: make a Claude Project per job ("Shopping", "Thanksgiving") with instructions such as "You're my
shopping helper. Use Dieter's lists; when I can't find something, suggest a swap and offer to move it to the
next trip." Each conversation in that project starts with that focus.

## How it's built

- `core/connector/oauth.py`: sign-in (OAuth 2.1 + PKCE, dynamic client registration, rotating refresh tokens).
  Tables: `oauth_clients`, `oauth_codes`, `oauth_tokens` (hashes only).
- `core/connector/server.py`: `/mcp`, MCP over HTTP answered as plain JSON (no MCP SDK: it needs newer
  FastAPI/uvicorn than Dieter runs).
- `core/connector/tools.py`: `Tool`, `ToolSet`, `RemoteToolSet`, `serve_tools()`.
- `apps/kitchen/connector.py`: shopping lists and recipes.
- `apps/kitchen/thanksgiving/connector.py`: plan, dishes, Thanksgiving list, plan changes (same checks and
  Undo as Tell Dieter). Served by the Thanksgiving service at `/thanksgiving/api/connector/tools`.

## Adding an app's tools (Trainer, Music, Planner...)

1. Write `apps/<app>/connector.py` with `def tools(): return ToolSet("<app>", [Tool(...), ...])`.
   - Names are `<app>_<verb>_<thing>`; descriptions say what comes back and which ids/keys later tools need.
   - Handlers take the arguments dict and run as the signed-in user (`get_current_user_id()` works).
   - Raise `ToolError("...")` for anything Claude should hear (not found, bad input); return JSON-able data.
   - Mark read-only tools `read_only=True` and deleting ones `destructive=True`.
2. Add it to `CONNECTOR_TOOLSETS` in `api.py`. If the app runs as its own service, call
   `serve_tools(router, "/<app>/api/connector/tools", connector.tools())` there and add a `RemoteToolSet` instead.
3. Test through `/mcp` like `tests/test_connector.py` does.

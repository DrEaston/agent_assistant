"""Dieter's connector: lets Claude (the Claude app, claude.ai, Claude Code) use Dieter as a tool.

Claude talks to https://askdieter.ai/mcp using the Model Context Protocol (MCP). The pieces:

- oauth.py   Sign-in. Claude registers itself, sends the person to Dieter's own login and an "Allow Claude?"
             page, and gets a token that acts as that person. Tokens are stored hashed.
- server.py  The /mcp endpoint: JSON-RPC over HTTP (initialize, tools/list, tools/call), answered as plain JSON.
- tools.py   Tool and ToolSet. Each app describes its tools in its own `connector.py` and the main app hands
             the sets to the server. An app that runs as its own service (Thanksgiving) serves its ToolSet with
             serve_tools() and the main app reaches it through RemoteToolSet, signed as the user.

Adding an app (Trainer, Music, ...): write apps/<app>/connector.py with `def tools(): return ToolSet(...)`,
name every tool "<app>_<verb>_<thing>", and add it to CONNECTOR_TOOLSETS in api.py. See docs/connector.md.
"""

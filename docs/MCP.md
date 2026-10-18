# MCP server: use Antarctica from Claude, Cursor, or any MCP client

`mcp_server/server.py` exposes the deployed API as four tools:

| Tool | What it does | Cost |
|---|---|---|
| `run_audit(url)` | Starts a Lighthouse scan, waits (30–90 s), returns score, metrics in seconds, top 5 findings | Free (Cloud Run time) |
| `get_report(scan_id)` | Full report for a finished scan (up to 20 findings) | Free |
| `suggest_fixes(scan_id, limit=3)` | RAG-grounded Gemini fixes for the top `limit` findings (1–5) | ~$0.0012 per fix |
| `site_history(url)` | Past scans of a URL with scores, newest first | Free |

## Install (once, on your laptop)
```bash
cd ~/antarctica && source .venv/bin/activate
pip install -e ".[dev,ai]"          # pulls in the mcp SDK and the antarctica-mcp command
which antarctica-mcp                 # copy this full path for the config below
```

## Claude Desktop
Settings → Developer → Edit Config, add:
```json
{
  "mcpServers": {
    "antarctica": {
      "command": "/Users/<you>/antarctica/.venv/bin/antarctica-mcp",
      "env": { "ANTARCTICA_API_URL": "https://<your-api>.run.app" }
    }
  }
}
```
Restart Claude Desktop. Try: *"Audit shopnovus.us and tell me the single change that would help most, with code."*

## Claude Code
```bash
claude mcp add antarctica --env ANTARCTICA_API_URL=https://<your-api>.run.app \
  -- /Users/<you>/antarctica/.venv/bin/antarctica-mcp
```

## How it works (interview version)
- **Transport: stdio.** The assistant launches `antarctica-mcp` as a subprocess and speaks JSON-RPC over stdin/stdout. No port, no auth to configure for local use.
- **Thin wrapper over the HTTP API**, not the database: same behavior as every other client, and no DB credentials on the laptop.
- **Compact tool results**: trimmed to what the model needs (seconds, top findings, deduped sources), because tool output costs context tokens.
- **Errors as tool results**: expected failures (bad URL, failed scan, AI not configured, unknown id) come back as `isError` results with a readable message, so the model can explain or recover; crashes stay in the server log.
- **Tool annotations**: `readOnlyHint` tells clients which tools are safe to auto-approve; `run_audit` creates a scan, so it isn't read-only.

## Testing
`tests/test_mcp_server.py` runs a real MCP client against the server in-process (list tools, call tools, error paths) with a fake API. No network.

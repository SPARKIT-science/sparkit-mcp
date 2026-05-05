# sparkit-mcp

MCP server for [SPARKIT](https://sparkit.science) — call the scientific
research agent from Claude Desktop, Cursor, Claude Code, or any other
MCP-compatible client.

Two tools are exposed:

- **`research`** — submit a scientific question. SPARKIT searches the
  literature, reads the relevant papers, and returns a cited Markdown
  report. Blocks until the job finishes (default 4 min) and returns the
  full report inline.
- **`get_job_status`** — fetch a previously-submitted job by id. Useful
  when `research` returned before the job finished, or to revisit a
  past report.

## Install

```bash
uv tool install sparkit-mcp
```

Or with pip:

```bash
pip install sparkit-mcp
```

Either installs a `sparkit-mcp` console script. (Pre-release: install
straight from GitHub with
``uv tool install "git+https://github.com/SPARKIT-science/sparkit-mcp.git"``
until the first PyPI release lands.)

## Get an API key

1. Sign up at https://app.sparkit.science/signup (Try-it costs $10 for
   5 queries; subscriptions start at $50/mo).
2. Visit https://app.sparkit.science/keys and create a key.
3. Copy the key — it's shown only once.

## Configure your MCP client

### Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add:

```json
{
  "mcpServers": {
    "sparkit": {
      "command": "sparkit-mcp",
      "env": {
        "SPARKIT_API_KEY": "sk_sparkit_..."
      }
    }
  }
}
```

Restart Claude Desktop. You should see `sparkit` appear in the tools
icon next to the chat input.

If `sparkit-mcp` isn't on Claude Desktop's PATH (common with `uv tool`),
use the absolute path:

```json
"command": "/Users/you/.local/bin/sparkit-mcp"
```

(Find the path with `which sparkit-mcp` after `uv tool install`.)

### Cursor

Edit `~/.cursor/mcp.json` (or `.cursor/mcp.json` in your project):

```json
{
  "mcpServers": {
    "sparkit": {
      "command": "sparkit-mcp",
      "env": {
        "SPARKIT_API_KEY": "sk_sparkit_..."
      }
    }
  }
}
```

Reload Cursor (`Cmd+Shift+P` → "Reload Window").

### Claude Code

```bash
claude mcp add sparkit -e SPARKIT_API_KEY=sk_sparkit_... -- sparkit-mcp
```

## Try it

Once configured, ask the LLM:

> Use SPARKIT to look up the most recent literature on the role of
> WRNIP1 as a synthetic-lethal target in cancer.

The LLM will call `research`. Expect a 60-180s wait, then a Markdown
report with inline citations and a numbered Sources list.

## Configuration

| Env var                       | Default                                              | Description |
|-------------------------------|------------------------------------------------------|-------------|
| `SPARKIT_API_KEY`             | (required)                                           | Bearer key from https://app.sparkit.science/keys. |
| `SPARKIT_API_BASE`            | `https://jlsteenwyk--sparkit-api-web.modal.run`      | Override the API base URL. Useful for staging or self-hosted deployments. |
| `SPARKIT_API_TIMEOUT_SECONDS` | `30`                                                 | Per-HTTP-request timeout. Doesn't affect total wait time for `research`; that's `max_wait_seconds`. |

## Tool reference

### `research(question, response_format?, include_citations?, max_wait_seconds?)`

| Arg                | Type                | Default | Description |
|--------------------|---------------------|---------|-------------|
| `question`         | string              | —       | The scientific question. Required. Be specific. |
| `response_format`  | `"full"` or `"brief"` | `"full"` | Length of the returned Markdown report. |
| `include_citations`| boolean             | `true`  | Keep `true` for sourced reports. |
| `max_wait_seconds` | int (30-540)        | `240`   | How long to block waiting before returning the job_id with instructions to poll. |

Returns Markdown. On timeout, returns a status line with the job_id so
the LLM can call `get_job_status` later.

### `get_job_status(job_id)`

Returns the cited Markdown report if the job has completed, a status
line if it's still running, or an error message otherwise.

## Troubleshooting

**Authentication failed** — `SPARKIT_API_KEY` isn't set or is invalid.
Check `claude_desktop_config.json` for typos; restart Claude Desktop
after edits.

**Quota exhausted** — out of monthly queries / Try-it credits. Visit
https://app.sparkit.science/billing.

**Tool isn't appearing in Claude Desktop** — check the Claude Desktop
log:
- macOS: `~/Library/Logs/Claude/mcp-server-sparkit.log`
- Windows: `%LOCALAPPDATA%\Claude\Logs\mcp-server-sparkit.log`

The most common issue is `command: sparkit-mcp` not being on PATH;
substitute the absolute path from `which sparkit-mcp`.

**Job times out** — `max_wait_seconds` cap is 540s (9 min). For very
deep questions, submit then poll `get_job_status` instead of waiting
inline. SPARKIT will also auto-cancel jobs that exceed its own
internal limit.

## License

MIT.

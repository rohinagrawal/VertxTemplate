# SentinelOne Logs MCP

This folder is the shared source for SentinelOne / Scalyr MCP integrations used by repo-local agents.

## Adapter Layout

Tool-specific folders should point here:

| Tool | Adapter path | Shared source |
| --- | --- | --- |
| Codex | `.codex/mcp/sentinelone/server.py` | `.agents/mcp/sentinelone/codex-server.py` |
| Cursor | `.cursor/mcp/sentinelone/server.py` | `.agents/mcp/sentinelone/cursor-server.py` |
| Cursor requirements | `.cursor/mcp/sentinelone/requirements.txt` | `.agents/mcp/sentinelone/requirements.txt` |

The two server variants are intentionally preserved for now because they expose different tool surfaces:

- `codex-server.py`: raw JSON-RPC MCP server with conservative, redacted Platform-Apollo triage helpers.
- `cursor-server.py`: FastMCP-based server with summary-first query helpers plus facet, numeric, power-query, deployment-health, and exception helpers.

Unifying these into one server should be done as a separate compatibility change with validation in both Codex and Cursor.

## API

The MCP servers give read-only access to SentinelOne / Scalyr logs through the query API:

```text
POST https://xdr.aps1.sentinelone.net/api/query
```

Codex MCP server name:

```text
sentinelone_logs
```

Cursor MCP server name:

```text
sentinelone
```

## Codex Tools

- `sentinelone_search_logs`: run a SentinelOne filter and return a compact grouped summary by default.
- `sentinelone_search_by_fields`: build a log filter from structured fields, severity, contains phrases, and optional raw filter fragments; returns a compact grouped summary by default.
- `sentinelone_quick_platform_apollo_health`: run a fast recent-window health check with small tail samples for `severity>=6` and `severity>=5`.
- `sentinelone_search_platform_apollo_logs`: build the common Platform Apollo filter from `deployment_version`, `contains`, `start_time`, and `end_time`.
- `sentinelone_triage_platform_apollo`: run a post-deploy triage set and return redacted samples plus recurring patterns.

Prefer `sentinelone_search_by_fields` when you know the fields but do not want to hand-write query syntax. Use `sentinelone_search_logs` when you need full control over the raw filter. Prefer `sentinelone_quick_platform_apollo_health` for generic "check the last N minutes" requests. Prefer `sentinelone_triage_platform_apollo` for broader post-deploy checks.

## Token-Efficient Search Workflow

For any high-volume Platform Apollo log search, start with summaries:

1. Use `response_mode=summary` (the default) unless exact raw rows are required.
2. Keep first-pass `max_count` around `50-100` and `page_mode=tail` unless the oldest rows are specifically needed.
3. Use structured filters, especially `application`, `deployment_version`, `min_severity`, `severity`, and exact fields, before adding phrases.
4. Read `top_patterns` before deciding whether phrase probes are needed.
5. Probe phrases only after the summary points to a concrete string, and keep each phrase query small.
6. Split long windows before increasing `max_count` when a query times out.
7. Request `response_mode=raw`, `columns`, and small raw row limits only when exact rows are needed.

Avoid these slow/noisy starts:

- Unfiltered `application=='platform-apollo'` probes over busy windows.
- Broad WARN scans such as `severity==4` unless warnings were explicitly requested.
- Free-text `ERROR` searches; they can match normal payload content. Use `severity>=5` for actual error-severity logs.
- Generic terms such as `Exception` or high-volume audio-buffer warnings as default probes.

## Telemetry

MCP responses include `mcp_telemetry` metadata where the host surface supports it. The telemetry is size/latency oriented and does not include secrets:

- `elapsed_ms`
- request and response character counts
- approximate request and response token counts
- upstream SentinelOne endpoint/status details when a live API call was made
- `is_error` for failed guarded or tool calls

Use this metadata to identify slow or noisy tools and feed durable improvements back into `.agents/` when the same friction repeats.

## Cursor Tools

The Cursor FastMCP variant exposes:

- `query_logs`
- `facet_query`
- `numeric_query`
- `power_query`
- `timeseries_query`
- `search_errors`
- `quick_platform_apollo_health`
- `error_summary`
- `error_rate_over_time`
- `deployment_health`
- `find_exceptions`

Cursor `query_logs`, `search_errors`, and `find_exceptions` return compact grouped summaries by default. Set `response_mode="raw"` or `response_mode="exact"` only when exact rows are needed. For raw Cursor rows, keep `max_count` low and pass `columns` plus `max_message_chars`.

## Query API Shape

The MCP maps tool arguments to the `/api/query` JSON payload:

```json
{
  "queryType": "log",
  "filter": "application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' severity==5",
  "startTime": "2026-06-16T12:10:23",
  "endTime": "2026-06-16T12:37:22",
  "maxCount": 100,
  "pageMode": "tail",
  "continuationToken": null
}
```

Important fields:

- `queryType`: defaults to `log`.
- `filter`: SentinelOne / Scalyr filter expression.
- `startTime`, `endTime`: timestamps for the search window. In this tenant, UTC API timestamps matched IST-rendered log lines.
- `maxCount`: defaults to `100` for Codex/Cursor log search and is capped by this MCP at `5000`.
- `pageMode`: `head` for earliest matches, `tail` for latest matches.
- `continuationToken`: pass the previous response token to fetch the next page.
- `response_mode`: MCP option, not sent to SentinelOne. Defaults to `summary`; set `raw` only for exact rows.
- `columns`: optional query payload field that can reduce raw row size when exact rows are needed.

The DataSet UI bundle also builds query requests for log entries, power-query style grouping, plots, top facets, and facet values. This MCP currently exposes the stable log-query path and adds local triage/grouping on top of log results.

## Filter Examples

Exact application and release:

```text
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344'
```

Actual error severity:

```text
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' severity==5
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' severity>=5
application=='platform-apollo' severity>=6
```

Phrase match:

```text
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' 'Logging pdt data'
```

Operational probes:

```text
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' 'Thread blocked'
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' 'VertxException'
application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' 'ClassCastException'
```

## Example Prompts

Raw filter:

```text
Use sentinelone_search_logs with filter application=='platform-apollo' DEPLOYMENT_VERSION=='platform-apollo-release-344' severity>=5 from 2026-06-16T12:10:23 to 2026-06-16T12:37:22. Leave response_mode as summary first.
```

Structured field search:

```text
Use sentinelone_search_by_fields for application platform-apollo, deployment_version platform-apollo-release-344, min_severity 5, start_time 2026-06-16T12:10:23, end_time 2026-06-16T12:37:22.
```

Raw row follow-up:

```text
Use sentinelone_search_by_fields with response_mode raw, columns timestamp,severity,DEPLOYMENT_VERSION,message, max_count 20, max_items_returned 20, after the summary identifies the exact phrase or severity to inspect.
```

Cursor summary-first search:

```text
Use query_logs with application platform-apollo, deployment_version platform-apollo-release-344, min_severity 5, start_time 2026-06-16T12:10:23, end_time 2026-06-16T12:37:22. Leave response_mode as summary first.
```

Cursor raw row follow-up:

```text
Use query_logs with response_mode raw, columns timestamp,severity,DEPLOYMENT_VERSION,message, max_count 20, max_message_chars 1000, after the summary identifies the exact filter to inspect.
```

Post-deploy triage:

```text
Use sentinelone_triage_platform_apollo for deployment_version platform-apollo-release-344, start_time 2026-06-16T12:10:23, end_time 2026-06-16T12:37:22.
```

Recent-window health check:

```text
Use sentinelone_quick_platform_apollo_health for start_time 2026-06-23T08:10:58, end_time 2026-06-23T08:20:58, page_mode tail, max_count_per_query 30.
```

Continuation:

```text
Use sentinelone_search_logs with the same filter and continuation_token from the previous response.
```

## Triage Defaults

`sentinelone_quick_platform_apollo_health` runs:

- `severity>=6`
- `severity>=5`
- optional `focus_terms`

`sentinelone_triage_platform_apollo` runs a bounded version of the same severity probes and optional `focus_terms`. The unfiltered application probe and `severity==4` warning scan are opt-in through `include_application_probe` and `include_warnings`.

Cursor `quick_platform_apollo_health` runs the same bounded `severity>=6` and `severity>=5` probes and accepts optional comma-separated `focus_terms_csv`.

It redacts common UUIDs, long numeric IDs, auth-like tokens, and IPv4 addresses in summaries.

## Credentials

Do not commit tokens in repo files.

Both server variants use the same environment variables:

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `SENTINELONE_API_TOKEN` | Yes | None | Read-only SentinelOne / Scalyr API token. |
| `SENTINELONE_BASE_URL` | No | `https://xdr.aps1.sentinelone.net` | SentinelOne API base URL. |
| `SENTINELONE_QUERY_PATH` | No | `/api/query` | Used by the Codex raw MCP server. |
| `SENTINELONE_TIMEOUT_SECONDS` | No | `30` | HTTP request timeout in seconds for both Codex and Cursor server variants. |
| `SENTINELONE_DEFAULT_APP` | No | `platform-apollo` | Used by the Cursor FastMCP server. |

Base URL and default app can stay in committed config because they are not secrets. The token must not be committed.

Recommended local setup:

Create `.agents/.env.local`:

```dotenv
SENTINELONE_API_TOKEN=<token>
```

`.agents/.env.local` is ignored by Git. The MCP server loads it on startup. If `SENTINELONE_API_TOKEN` is already exported in the process environment, that exported value wins over `.agents/.env.local`.

The loader also accepts repo-root `.env.local` as a fallback.

Optional non-secret overrides can also go in `.agents/.env.local`:

```dotenv
SENTINELONE_BASE_URL=https://xdr.aps1.sentinelone.net
SENTINELONE_DEFAULT_APP=platform-apollo
SENTINELONE_QUERY_PATH=/api/query
SENTINELONE_TIMEOUT_SECONDS=30
```

Codex repo config `.codex/config.toml` and Cursor repo config `.cursor/mcp.json` intentionally do not contain token values. They rely on `SENTINELONE_API_TOKEN` loaded from `.agents/.env.local` or the process environment.

If the token is missing when logs are needed, ask the user to create/update `.agents/.env.local` and relaunch the agent. Do not ask the user to paste the token into chat or committed repo files.

If a token was pasted into chat, command output, or committed history, rotate it before using this integration long-term.

import os
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP


ENV_LOCAL_FILE = ".env.local"
ENV_LOCAL_PATHS = (
    Path(".agents") / ENV_LOCAL_FILE,
    Path(ENV_LOCAL_FILE),
)


def _is_env_key(value: str) -> bool:
    if not value or not (value[0].isalpha() or value[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in value)


def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _iter_repo_roots():
    starts = [Path.cwd(), Path(__file__).resolve()]
    seen: set[Path] = set()
    for start in starts:
        base = start if start.is_dir() else start.parent
        for parent in (base, *base.parents):
            if parent in seen:
                continue
            if (parent / ".git").exists() or (parent / ".agents").exists() or (parent / "AGENTS.md").exists():
                seen.add(parent)
                yield parent
                break


def _iter_candidate_env_files():
    roots = list(_iter_repo_roots()) or [Path.cwd()]
    seen: set[Path] = set()
    for root in roots:
        for relative_path in ENV_LOCAL_PATHS:
            candidate = root / relative_path
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _load_env_local() -> None:
    for path in _iter_candidate_env_files():
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if not _is_env_key(key):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        return


_load_env_local()


SENTINELONE_BASE_URL = os.environ.get("SENTINELONE_BASE_URL", "https://xdr.aps1.sentinelone.net")
SENTINELONE_API_TOKEN = os.environ.get("SENTINELONE_API_TOKEN", "")
SENTINELONE_TIMEOUT_SECONDS = _parse_int(os.environ.get("SENTINELONE_TIMEOUT_SECONDS"), 30, 1, 300)
DEFAULT_APP = os.environ.get("SENTINELONE_DEFAULT_APP", "platform-apollo")
DEFAULT_MAX_COUNT = 100
DEFAULT_RAW_MESSAGE_CHARS = 1000
LOG_LINE_RE = re.compile(r"^([0-9: .-]+)\s+(\w+)\s+\[([^\]]+)\]\s+\(([^)]+)\)\s+-\s+(.*)$", re.S)

mcp = FastMCP("sentinelone")


def _approx_tokens(char_count: int) -> int:
    return max(1, (char_count + 3) // 4) if char_count > 0 else 0


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _ago_iso(hours: float = 1) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _defaults(start_time: Optional[str], end_time: Optional[str], default_hours: float = 1):
    if not start_time:
        start_time = _ago_iso(default_hours)
    if not end_time:
        end_time = _now_iso()
    return start_time, end_time


def _build_filter(
    application: Optional[str] = None,
    deployment_version: Optional[str] = None,
    severity: Optional[str] = None,
    min_severity: Optional[Any] = None,
    severity_gte: Optional[Any] = None,
    extra_filter: Optional[str] = None,
    search_text: Optional[str] = None,
    server_host: Optional[str] = None,
) -> str:
    parts: list[str] = []
    if application:
        parts.append(f"application=='{application}'")
    if deployment_version:
        parts.append(f"DEPLOYMENT_VERSION=='{deployment_version}'")
    if severity:
        parts.append(f"severity=={severity}")
    min_severity_value = min_severity if min_severity not in (None, "") else severity_gte
    if min_severity_value not in (None, ""):
        parts.append(f"severity>={int(min_severity_value)}")
    if server_host:
        parts.append(f"serverHost=='{server_host}'")
    if extra_filter:
        parts.append(extra_filter)
    if search_text:
        parts.append(f"'{search_text}'")
    return " ".join(parts)


async def _post_api(endpoint: str, payload: dict) -> dict:
    request_text = json.dumps(payload, separators=(",", ":"), default=str)
    if not SENTINELONE_API_TOKEN:
        return {
            "error": (
                "SENTINELONE_API_TOKEN is not set. Add SENTINELONE_API_TOKEN=<token> to "
                ".agents/.env.local, or export it before launching Cursor."
            ),
            "_mcp_telemetry": {
                "endpoint": endpoint,
                "elapsed_ms": 0,
                "request_chars": len(request_text),
                "approx_request_tokens": _approx_tokens(len(request_text)),
                "response_chars": 0,
                "approx_response_tokens": 0,
                "is_error": True,
            },
        }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SENTINELONE_API_TOKEN}",
    }
    started_at = time.perf_counter()
    async with httpx.AsyncClient(timeout=float(SENTINELONE_TIMEOUT_SECONDS)) as client:
        resp = await client.post(f"{SENTINELONE_BASE_URL}{endpoint}", headers=headers, json=payload)
        response_chars = len(resp.content)
        resp.raise_for_status()
        result = resp.json()
        telemetry = {
            "endpoint": endpoint,
            "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "request_chars": len(request_text),
            "approx_request_tokens": _approx_tokens(len(request_text)),
            "response_chars": response_chars,
            "approx_response_tokens": _approx_tokens(response_chars),
            "status_code": resp.status_code,
            "is_error": False,
        }
        if isinstance(result, dict):
            result["_mcp_telemetry"] = telemetry
            return result
        return {"value": result, "_mcp_telemetry": telemetry}


def _safe_json(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _is_api_error(result: dict) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _format_matches(matches: list, max_msg_len: int = DEFAULT_RAW_MESSAGE_CHARS) -> list[dict]:
    """Format raw match objects from /api/query into readable dicts."""
    formatted = []
    for m in matches:
        if isinstance(m, str):
            formatted.append({"message": m[:max_msg_len]})
            continue
        msg = m.get("message", m.get("log", ""))
        ts_raw = m.get("timestamp", "")
        sev = m.get("severity", "")
        attrs = m.get("attributes", {})
        entry: dict = {"timestamp": ts_raw, "severity": sev}
        if isinstance(attrs, dict):
            for key in ("servNm", "serv_nm", "className", "apiName", "api_name", "level", "exceptionName"):
                if key in attrs:
                    entry[key] = attrs[key]
        entry["message"] = str(msg)[:max_msg_len]
        formatted.append(entry)
    return formatted


def _redact_log_summary(value: str) -> str:
    value = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<uuid>", value, flags=re.I)
    value = re.sub(r"\b\d{8,}\b", "<num>", value)
    value = re.sub(r"MAT[0-9A-Za-z]+", "<auth>", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", value)
    value = re.sub(r"\btx=[^\s]+", "tx=<id>", value)
    value = re.sub(r"\brequest[_-]?id[\"'=:\s]+[A-Za-z0-9._:-]+", "request_id=<id>", value, flags=re.I)
    value = re.sub(r"\bconversationId:?\s*[A-Za-z0-9._:-]+", "conversationId=<id>", value)
    value = re.sub(r"\buserIdentifier:?\s*[A-Za-z0-9._:-]+", "userIdentifier=<id>", value)
    return value.replace("\n", " ")


def _parse_log_message(message: str, max_chars: int) -> dict[str, Any]:
    match = LOG_LINE_RE.search(message)
    if not match:
        return {"summary": _redact_log_summary(message[:max_chars])}

    timestamp, level, thread, source, rest = match.groups()
    summary = rest.split("|", 1)[1].strip() if "|" in rest else rest.strip()
    return {
        "time": timestamp.strip(),
        "level": level,
        "thread": thread,
        "source": source,
        "summary": _redact_log_summary(summary[:max_chars]),
    }


def _summarize_query_result(
    result: dict,
    payload: dict,
    filter_expr: str,
    start_time: str,
    end_time: str,
    max_samples: int,
    max_patterns: int,
    max_summary_chars: int,
) -> dict:
    matches = result.get("matches", [])
    if not isinstance(matches, list):
        matches = []

    pattern_counts: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for item in matches:
        message = item.get("message", item.get("log", "")) if isinstance(item, dict) else str(item)
        parsed = _parse_log_message(str(message), max_summary_chars)
        if len(samples) < max_samples:
            samples.append(parsed)
        pattern = f"{parsed.get('source', 'unknown')} | {parsed.get('summary', '')[:160]}"
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

    sessions = result.get("sessions", {})
    versions: set[str] = set()
    hosts: set[str] = set()
    if isinstance(sessions, dict):
        for session in sessions.values():
            if isinstance(session, dict):
                if session.get("DEPLOYMENT_VERSION"):
                    versions.add(str(session["DEPLOYMENT_VERSION"]))
                if session.get("IP_ADDRESS"):
                    hosts.add(str(session["IP_ADDRESS"]))

    top_patterns = [
        {"count": count, "pattern": pattern}
        for pattern, count in sorted(pattern_counts.items(), key=lambda item: item[1], reverse=True)[:max_patterns]
    ]
    token = result.get("continuationToken")
    return {
        "status": result.get("status"),
        "returned": len(matches),
        "filter_used": filter_expr,
        "time_range": f"{start_time} -> {end_time}",
        "request": {
            "queryType": payload.get("queryType"),
            "filter": payload.get("filter"),
            "startTime": payload.get("startTime"),
            "endTime": payload.get("endTime"),
            "maxCount": payload.get("maxCount"),
            "pageMode": payload.get("pageMode"),
        },
        "has_more": token is not None,
        "continuation_token": token,
        "cpu_usage": result.get("cpuUsage"),
        "mcp_telemetry": result.get("_mcp_telemetry"),
        "versions": sorted(versions),
        "hosts": sorted(hosts),
        "samples": samples,
        "top_patterns": top_patterns,
    }


@mcp.tool()
async def query_logs(
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    severity: Optional[str] = None,
    min_severity: Optional[int] = None,
    severity_gte: Optional[int] = None,
    extra_filter: Optional[str] = None,
    search_text: Optional[str] = None,
    server_host: Optional[str] = None,
    columns: Optional[str] = None,
    response_mode: str = "summary",
    max_count: int = DEFAULT_MAX_COUNT,
    page_mode: str = "tail",
    continuation_token: Optional[str] = None,
    max_samples: int = 5,
    max_patterns: int = 8,
    max_summary_chars: int = 260,
    max_message_chars: int = DEFAULT_RAW_MESSAGE_CHARS,
) -> str:
    """Search SentinelOne / Scalyr logs.

    Returns a compact grouped summary by default. Set response_mode='raw' or
    response_mode='exact' only when exact rows are required.

    Args:
        application: Application name filter (default: platform-apollo). Set empty string to skip.
        start_time: Start of time range in ISO format (e.g. 2026-06-16T17:00:00) or relative (e.g. '1h', '24h'). Defaults to 1 hour ago.
        end_time: End of time range in ISO format. Defaults to now.
        deployment_version: Filter by DEPLOYMENT_VERSION field.
        severity: Exact severity filter, e.g. '5'.
        min_severity: Minimum severity filter, emitted as severity>=N.
        severity_gte: Alias for min_severity.
        extra_filter: Raw Scalyr filter expression appended to the query (e.g. "status>=500", "serverHost contains 'web'").
        search_text: Free-text substring to search within log messages.
        server_host: Filter by serverHost field.
        columns: Raw-mode optimization; comma-separated fields to return (e.g. "timestamp,severity,message").
        response_mode: 'summary' by default, or 'raw'/'exact' for exact rows.
        max_count: Number of events to return (1–5000, default 100).
        page_mode: 'head' for oldest-first, 'tail' for newest-first.
        continuation_token: Pagination token from a previous response to fetch next page.
        max_samples: Summary-mode sample count.
        max_patterns: Summary-mode pattern count.
        max_summary_chars: Summary-mode per-message summary length.
        max_message_chars: Raw-mode max message length.
    """
    start_time, end_time = _defaults(start_time, end_time)
    filter_expr = _build_filter(
        application,
        deployment_version,
        severity=severity,
        min_severity=min_severity,
        severity_gte=severity_gte,
        extra_filter=extra_filter,
        search_text=search_text,
        server_host=server_host,
    )
    payload: dict = {
        "queryType": "log",
        "filter": filter_expr,
        "startTime": start_time,
        "endTime": end_time,
        "maxCount": min(max_count, 5000),
        "pageMode": page_mode,
        "priority": "low",
    }
    if continuation_token:
        payload["continuationToken"] = continuation_token
    if columns:
        payload["columns"] = columns

    try:
        result = await _post_api("/api/query", payload)
    except httpx.HTTPStatusError as e:
        return _safe_json({"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:2000]})
    except Exception as e:
        return _safe_json({"error": str(e)})

    if _is_api_error(result):
        return _safe_json(result)
    if result.get("status", "").startswith("error"):
        return _safe_json(result)

    matches = result.get("matches", [])
    token = result.get("continuationToken")

    if response_mode not in {"raw", "exact"}:
        return _safe_json(
            _summarize_query_result(
                result=result,
                payload=payload,
                filter_expr=filter_expr,
                start_time=start_time,
                end_time=end_time,
                max_samples=max(0, min(max_samples, 50)),
                max_patterns=max(0, min(max_patterns, 50)),
                max_summary_chars=max(80, min(max_summary_chars, 2000)),
            )
        )

    return _safe_json({
        "status": result.get("status"),
        "total_events": len(matches),
        "filter_used": filter_expr,
        "time_range": f"{start_time} → {end_time}",
        "has_more": token is not None,
        "continuation_token": token,
        "cpu_usage": result.get("cpuUsage"),
        "mcp_telemetry": result.get("_mcp_telemetry"),
        "events": _format_matches(matches, max_msg_len=max(200, min(max_message_chars, 100000))),
    })


@mcp.tool()
async def facet_query(
    field: str,
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    extra_filter: Optional[str] = None,
    server_host: Optional[str] = None,
    max_count: int = 50,
) -> str:
    """Get the most frequent values of a field (top-N). Great for finding common errors, status codes, API names, hosts, etc.

    Args:
        field: The field to get top values for (e.g. 'servNm', 'severity', 'apiName', 'serverHost', 'className', 'status').
        application: Application name filter.
        start_time: Start time (ISO or relative like '1h'). Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        deployment_version: Filter by DEPLOYMENT_VERSION.
        extra_filter: Additional Scalyr filter expression.
        server_host: Filter by serverHost.
        max_count: Max unique values to return (1–1000, default 50).
    """
    start_time, end_time = _defaults(start_time, end_time)
    filter_expr = _build_filter(application, deployment_version, extra_filter=extra_filter,
                                server_host=server_host)
    payload = {
        "queryType": "facet",
        "filter": filter_expr,
        "field": field,
        "maxCount": min(max_count, 1000),
        "startTime": start_time,
        "endTime": end_time,
        "priority": "low",
    }

    try:
        result = await _post_api("/api/facetQuery", payload)
    except httpx.HTTPStatusError as e:
        return _safe_json({"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:2000]})
    except Exception as e:
        return _safe_json({"error": str(e)})

    if _is_api_error(result):
        return _safe_json(result)
    return _safe_json({
        "status": result.get("status"),
        "field": field,
        "filter_used": filter_expr,
        "time_range": f"{start_time} → {end_time}",
        "match_count": result.get("matchCount"),
        "values": result.get("values", []),
        "cpu_usage": result.get("cpuUsage"),
    })


@mcp.tool()
async def numeric_query(
    application: str = DEFAULT_APP,
    function: str = "count",
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    extra_filter: Optional[str] = None,
    server_host: Optional[str] = None,
    buckets: int = 60,
) -> str:
    """Get numeric / time-series data: event counts, rates, averages, percentiles over time buckets.

    Args:
        application: Application name filter.
        function: Aggregation — 'count', 'rate', 'mean(field)', 'min(field)', 'max(field)', 'sumPerSecond(field)', 'p50(field)', 'p90(field)', 'p99(field)', etc.
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        deployment_version: Filter by DEPLOYMENT_VERSION.
        extra_filter: Additional Scalyr filter expression.
        server_host: Filter by serverHost.
        buckets: Number of time buckets to divide the range into (1–5000, default 60).
    """
    start_time, end_time = _defaults(start_time, end_time)
    filter_expr = _build_filter(application, deployment_version, extra_filter=extra_filter,
                                server_host=server_host)
    payload = {
        "queryType": "numeric",
        "filter": filter_expr,
        "function": function,
        "startTime": start_time,
        "endTime": end_time,
        "buckets": min(buckets, 5000),
        "priority": "low",
    }

    try:
        result = await _post_api("/api/numericQuery", payload)
    except httpx.HTTPStatusError as e:
        return _safe_json({"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:2000]})
    except Exception as e:
        return _safe_json({"error": str(e)})

    if _is_api_error(result):
        return _safe_json(result)
    return _safe_json({
        "status": result.get("status"),
        "function": function,
        "filter_used": filter_expr,
        "time_range": f"{start_time} → {end_time}",
        "buckets": buckets,
        "values": result.get("values", []),
        "cpu_usage": result.get("cpuUsage"),
    })


@mcp.tool()
async def power_query(
    query: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> str:
    """Execute a PowerQuery for advanced analytics — filter, group, aggregate, join, parse, sort, limit, let, columns.

    The query uses pipe syntax. The first segment is a standard filter, followed by commands separated by |.

    Example queries:
        "application=='platform-apollo' severity==5 | group count=count() by apiName | sort -count | limit 20"
        "application=='platform-apollo' | group errors=count(severity>=5), total=count() by serverHost | let error_pct=errors*100/total | sort -error_pct"
        "application=='platform-apollo' severity>=4 | parse 'servNm=$servNm$' from message | group count() by servNm | sort -count | limit 30"

    PowerQuery commands: filter, columns, group...by, sort, limit, let, parse, join, union, transpose, timebucket.
    Group functions: count(), sum(), avg(), min(), max(), estimate_distinct(), percentile(), any(), first(), last().

    Args:
        query: The full PowerQuery string (max 5000 chars). First segment is a Scalyr filter, then | pipe commands.
        start_time: Start time (ISO or relative). Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
    """
    start_time, end_time = _defaults(start_time, end_time)
    payload = {
        "query": query,
        "startTime": start_time,
        "endTime": end_time,
        "priority": "low",
    }

    try:
        result = await _post_api("/api/powerQuery", payload)
    except httpx.HTTPStatusError as e:
        return _safe_json({"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:2000]})
    except Exception as e:
        return _safe_json({"error": str(e)})

    if _is_api_error(result):
        return _safe_json(result)
    if result.get("status", "").startswith("error"):
        return _safe_json(result)

    columns = [c.get("name", "") for c in result.get("columns", [])]
    rows = result.get("values", [])

    table = []
    for row in rows:
        table.append({columns[i]: row[i] for i in range(len(columns)) if i < len(row)})

    return _safe_json({
        "status": result.get("status"),
        "query": query,
        "time_range": f"{start_time} → {end_time}",
        "matching_events": result.get("matchingEvents"),
        "omitted_events": result.get("omittedEvents"),
        "columns": columns,
        "row_count": len(table),
        "rows": table,
        "cpu_usage": result.get("cpuUsage"),
    })


@mcp.tool()
async def timeseries_query(
    queries_json: str,
) -> str:
    """Execute one or more timeseries queries in a single call. Useful for comparing metrics side-by-side (e.g. error rate vs total rate).

    Args:
        queries_json: A JSON array of query objects. Each object has: filter (string), function (string, default 'rate'), startTime (string), endTime (string, optional), buckets (int, default 60).

    Example:
        '[{"filter": "application==\\'platform-apollo\\' severity>=5", "startTime": "1h", "function": "count", "buckets": 12}, {"filter": "application==\\'platform-apollo\\'", "startTime": "1h", "function": "count", "buckets": 12}]'
    """
    try:
        queries = json.loads(queries_json)
    except json.JSONDecodeError as e:
        return _safe_json({"error": f"Invalid JSON: {e}"})

    for q in queries:
        q.setdefault("function", "count")
        q.setdefault("buckets", 60)
        q.setdefault("startTime", _ago_iso(1))
        q.setdefault("priority", "low")

    payload = {"queries": queries}

    try:
        result = await _post_api("/api/timeseriesQuery", payload)
    except httpx.HTTPStatusError as e:
        return _safe_json({"error": f"HTTP {e.response.status_code}", "detail": e.response.text[:2000]})
    except Exception as e:
        return _safe_json({"error": str(e)})

    if _is_api_error(result):
        return _safe_json(result)
    return _safe_json({
        "status": result.get("status"),
        "results": result.get("results", []),
    })


@mcp.tool()
async def search_errors(
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    server_host: Optional[str] = None,
    max_count: int = 50,
    response_mode: str = "summary",
) -> str:
    """Quickly summarize ERROR and FATAL level logs.

    Args:
        application: Application name (default: platform-apollo).
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        deployment_version: Filter by DEPLOYMENT_VERSION.
        server_host: Filter by serverHost.
        max_count: Max results (default 50).
        response_mode: 'summary' by default, or 'raw'/'exact' for exact rows.
    """
    return await query_logs(
        application=application,
        start_time=start_time,
        end_time=end_time,
        deployment_version=deployment_version,
        min_severity=5,
        server_host=server_host,
        max_count=max_count,
        response_mode=response_mode,
    )


@mcp.tool()
async def quick_platform_apollo_health(
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    focus_terms_csv: Optional[str] = None,
    max_count_per_query: int = 30,
    page_mode: str = "tail",
) -> str:
    """Fast Platform Apollo health check using compact severity summaries.

    Args:
        application: Application name, default platform-apollo.
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        deployment_version: Optional DEPLOYMENT_VERSION.
        focus_terms_csv: Optional comma-separated phrases to sample after severity probes.
        max_count_per_query: Small per-query cap, default 30.
        page_mode: tail by default for latest rows.
    """
    start_time, end_time = _defaults(start_time, end_time)
    bounded_count = max(1, min(max_count_per_query, 200))
    specs: list[tuple[str, dict[str, Any]]] = [
        ("fatal_severity_gte_6", {"min_severity": 6}),
        ("errors_severity_gte_5", {"min_severity": 5}),
    ]
    if focus_terms_csv:
        for term in [item.strip() for item in focus_terms_csv.split(",") if item.strip()]:
            specs.append((f"contains_{re.sub(r'[^A-Za-z0-9]+', '_', term).strip('_').lower()}", {"search_text": term}))

    queries: list[dict[str, Any]] = []
    for name, overrides in specs:
        result_text = await query_logs(
            application=application,
            start_time=start_time,
            end_time=end_time,
            deployment_version=deployment_version,
            max_count=bounded_count,
            page_mode=page_mode,
            response_mode="summary",
            max_samples=4,
            max_patterns=6,
            max_summary_chars=240,
            **overrides,
        )
        try:
            query_result = json.loads(result_text)
        except json.JSONDecodeError:
            query_result = {"error": result_text}
        query_result["name"] = name
        queries.append(query_result)

    return _safe_json({
        "window": {"start_time": start_time, "end_time": end_time},
        "application": application,
        "deployment_version": deployment_version,
        "queries": queries,
    })


@mcp.tool()
async def error_summary(
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    top_n: int = 20,
) -> str:
    """Get a summary of errors grouped by service name (servNm). Shows top error categories and their counts.

    Args:
        application: Application name.
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        deployment_version: Filter by DEPLOYMENT_VERSION.
        top_n: Number of top error categories to return.
    """
    start_time, end_time = _defaults(start_time, end_time)
    app_filter = f"application=='{application}'" if application else ""
    ver_filter = f"DEPLOYMENT_VERSION=='{deployment_version}'" if deployment_version else ""
    base = " ".join(f for f in [app_filter, ver_filter] if f)

    query = f"{base} severity>=5 | group count=count() by servNm | sort -count | limit {top_n}"
    return await power_query(query=query, start_time=start_time, end_time=end_time)


@mcp.tool()
async def error_rate_over_time(
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    deployment_version: Optional[str] = None,
    buckets: int = 30,
) -> str:
    """Get the error count over time buckets to see if errors are spiking or stable.

    Args:
        application: Application name.
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        deployment_version: Filter by DEPLOYMENT_VERSION.
        buckets: Number of time buckets.
    """
    return await numeric_query(
        application=application,
        function="count",
        start_time=start_time,
        end_time=end_time,
        deployment_version=deployment_version,
        extra_filter="(severity>=5)",
        buckets=buckets,
    )


@mcp.tool()
async def deployment_health(
    application: str = DEFAULT_APP,
    deployment_version: str = "",
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> str:
    """Check deployment health: error count, top error types, and error rate trend for a specific deployment version.

    Args:
        application: Application name.
        deployment_version: The deployment version to check. Leave empty to check all versions.
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
    """
    start_time, end_time = _defaults(start_time, end_time)
    app_filter = f"application=='{application}'" if application else ""
    ver_filter = f"DEPLOYMENT_VERSION=='{deployment_version}'" if deployment_version else ""
    base = " ".join(f for f in [app_filter, ver_filter] if f)

    error_summary_query = f"{base} severity>=5 | group count=count() by servNm | sort -count | limit 15"
    severity_dist_query = f"{base} | group count=count() by severity | sort -count"

    results = {}
    try:
        errors = await _post_api("/api/powerQuery", {
            "query": error_summary_query, "startTime": start_time, "endTime": end_time, "priority": "low"})
        if _is_api_error(errors):
            results["top_errors_error"] = errors
        else:
            cols = [c.get("name") for c in errors.get("columns", [])]
            results["top_errors"] = [{cols[i]: r[i] for i in range(len(cols))} for r in errors.get("values", [])]
            results["total_error_events"] = errors.get("matchingEvents", 0)
    except Exception as e:
        results["top_errors_error"] = str(e)

    try:
        sev = await _post_api("/api/powerQuery", {
            "query": severity_dist_query, "startTime": start_time, "endTime": end_time, "priority": "low"})
        if _is_api_error(sev):
            results["severity_error"] = sev
        else:
            cols = [c.get("name") for c in sev.get("columns", [])]
            results["severity_distribution"] = [{cols[i]: r[i] for i in range(len(cols))} for r in sev.get("values", [])]
            results["total_events"] = sev.get("matchingEvents", 0)
    except Exception as e:
        results["severity_error"] = str(e)

    try:
        rate = await _post_api("/api/numericQuery", {
            "queryType": "numeric", "filter": f"{base} severity>=5",
            "function": "count", "startTime": start_time, "endTime": end_time,
            "buckets": 12, "priority": "low"})
        if _is_api_error(rate):
            results["rate_error"] = rate
        else:
            results["error_rate_trend_12_buckets"] = rate.get("values", [])
    except Exception as e:
        results["rate_error"] = str(e)

    results["filter"] = base
    results["time_range"] = f"{start_time} → {end_time}"
    return _safe_json(results)


@mcp.tool()
async def find_exceptions(
    application: str = DEFAULT_APP,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exception_class: Optional[str] = None,
    max_count: int = 30,
    response_mode: str = "summary",
) -> str:
    """Search for Java/Python exception stack traces in logs.

    Args:
        application: Application name.
        start_time: Start time. Defaults to 1 hour ago.
        end_time: End time. Defaults to now.
        exception_class: Filter to a specific exception class name (e.g. 'NullPointerException', 'RuntimeError').
        max_count: Max results.
        response_mode: 'summary' by default, or 'raw'/'exact' for exact rows.
    """
    extra = ""
    if exception_class:
        extra = f"'{exception_class}'"
    return await query_logs(
        application=application,
        start_time=start_time,
        end_time=end_time,
        min_severity=5,
        extra_filter=extra,
        max_count=max_count,
        response_mode=response_mode,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")

#!/usr/bin/env python3
"""Read-only MCP server for SentinelOne / Scalyr log queries."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "sentinelone-logs"
SERVER_VERSION = "0.3.0"
LOG_LINE_RE = re.compile(r"^([0-9: .-]+)\s+(\w+)\s+\[([^\]]+)\]\s+\(([^)]+)\)\s+-\s+(.*)$", re.S)
ENV_LOCAL_FILE = ".env.local"
ENV_LOCAL_PATHS = (
    Path(".agents") / ENV_LOCAL_FILE,
    Path(ENV_LOCAL_FILE),
)


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def send(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def response(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_error(message: str, structured: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


def tool_success(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(payload, indent=2, sort_keys=True)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def approx_tokens(char_count: int) -> int:
    return max(1, (char_count + 3) // 4) if char_count > 0 else 0


def refresh_tool_text(result: Dict[str, Any]) -> None:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        result["content"] = [{"type": "text", "text": json.dumps(structured, indent=2, sort_keys=True)}]


def attach_tool_telemetry(tool_name: str, arguments: Dict[str, Any], started_at: float, result: Dict[str, Any]) -> Dict[str, Any]:
    argument_text = json.dumps(arguments or {}, separators=(",", ":"), sort_keys=True, default=str)
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        content = result.get("content") or []
        message = content[0].get("text") if content and isinstance(content[0], dict) else ""
        structured = {"error": message}
        result["structuredContent"] = structured

    upstream = structured.get("mcp_telemetry")
    telemetry: Dict[str, Any] = {
        "tool": tool_name,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "argument_chars": len(argument_text),
        "approx_argument_tokens": approx_tokens(len(argument_text)),
        "is_error": bool(result.get("isError")),
    }
    if upstream is not None:
        telemetry["upstream"] = upstream
    structured["mcp_telemetry"] = telemetry
    response_text = json.dumps(structured, separators=(",", ":"), sort_keys=True, default=str)
    telemetry["response_chars"] = len(response_text)
    telemetry["approx_response_tokens"] = approx_tokens(len(response_text))
    refresh_tool_text(result)
    return result


def is_env_key(value: str) -> bool:
    if not value or not (value[0].isalpha() or value[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in value)


def iter_repo_roots() -> Iterable[Path]:
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


def iter_candidate_env_files() -> Iterable[Path]:
    roots = list(iter_repo_roots()) or [Path.cwd()]
    seen: set[Path] = set()
    for root in roots:
        for relative_path in ENV_LOCAL_PATHS:
            candidate = root / relative_path
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def load_env_local() -> None:
    for path in iter_candidate_env_files():
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
            if not is_env_key(key):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        return


load_env_local()


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected integer, got {value!r}") from exc
    return max(minimum, min(maximum, parsed))


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {key: ("<redacted>" if key.lower() in {"authorization", "cookie"} else value) for key, value in headers.items()}


def quote_filter_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def as_string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def format_filter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return quote_filter_value(str(value))


def build_filter_from_parts(arguments: Dict[str, Any]) -> str:
    parts: list[str] = []
    application = arguments.get("application")
    deployment_version = arguments.get("deployment_version")
    severity = arguments.get("severity")
    min_severity = arguments.get("min_severity")
    if min_severity in (None, ""):
        min_severity = arguments.get("severity_gte")
    field_equals = arguments.get("field_equals") or {}

    if application:
        parts.append(f"application=={quote_filter_value(str(application))}")
    if deployment_version:
        parts.append(f"DEPLOYMENT_VERSION=={quote_filter_value(str(deployment_version))}")
    if severity not in (None, ""):
        parts.append(f"severity=={parse_int(severity, 0, 0, 100)}")
    if min_severity not in (None, ""):
        parts.append(f"severity>={parse_int(min_severity, 0, 0, 100)}")
    if isinstance(field_equals, dict):
        for field, value in field_equals.items():
            if field and value not in (None, ""):
                parts.append(f"{field}=={format_filter_value(value)}")

    for phrase in as_string_list(arguments.get("contains")):
        parts.append(quote_filter_value(phrase))
    for phrase in as_string_list(arguments.get("contains_any")):
        parts.append(quote_filter_value(phrase))

    extra_filter = arguments.get("extra_filter")
    if extra_filter:
        parts.append(str(extra_filter))

    if not parts:
        raise ValueError("at least one filter part is required")
    return " ".join(parts)


def sentinelone_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    base_url = env("SENTINELONE_BASE_URL", "https://xdr.aps1.sentinelone.net")
    token = env("SENTINELONE_API_TOKEN")
    query_path = env("SENTINELONE_QUERY_PATH", "/api/query")
    timeout = parse_int(env("SENTINELONE_TIMEOUT_SECONDS"), 30, 1, 300)

    if not token:
        raise RuntimeError(
            "SENTINELONE_API_TOKEN is not set. Add SENTINELONE_API_TOKEN=<token> to "
            ".agents/.env.local, or export it before launching the agent."
        )
    if not base_url:
        raise RuntimeError("SENTINELONE_BASE_URL is not set")

    url = base_url.rstrip("/") + "/" + query_path.lstrip("/")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as res:
            raw = res.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"rawText": text}
            return {
                "status": res.status,
                "headers": dict(res.headers.items()),
                "body": parsed,
                "mcp_telemetry": {
                    "endpoint": query_path,
                    "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "request_chars": len(body),
                    "approx_request_tokens": approx_tokens(len(body)),
                    "response_chars": len(raw),
                    "approx_response_tokens": approx_tokens(len(raw)),
                    "status_code": res.status,
                    "is_error": False,
                },
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"rawText": text}
        raise RuntimeError(json.dumps({
            "status": exc.code,
            "body": parsed,
            "mcp_telemetry": {
                "endpoint": query_path,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "request_chars": len(body),
                "approx_request_tokens": approx_tokens(len(body)),
                "response_chars": len(raw),
                "approx_response_tokens": approx_tokens(len(raw)),
                "status_code": exc.code,
                "is_error": True,
            },
        }, sort_keys=True)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc.reason}") from exc


def trim_value(value: Any, max_list_items: int, max_string_chars: int) -> Any:
    if isinstance(value, dict):
        return {key: trim_value(item, max_list_items, max_string_chars) for key, item in value.items()}
    if isinstance(value, list):
        trimmed = [trim_value(item, max_list_items, max_string_chars) for item in value[:max_list_items]]
        if len(value) > max_list_items:
            trimmed.append({"_truncated_items": len(value) - max_list_items})
        return trimmed
    if isinstance(value, str) and len(value) > max_string_chars:
        return value[:max_string_chars] + f"... <truncated {len(value) - max_string_chars} chars>"
    return value


def make_payload(arguments: Dict[str, Any], filter_value: str) -> Dict[str, Any]:
    max_count = parse_int(arguments.get("max_count"), 100, 1, 5000)
    payload: Dict[str, Any] = {
        "queryType": arguments.get("query_type") or "log",
        "filter": filter_value,
        "startTime": arguments["start_time"],
        "endTime": arguments["end_time"],
        "maxCount": max_count,
        "pageMode": arguments.get("page_mode") or "tail",
        "continuationToken": arguments.get("continuation_token"),
    }
    if arguments.get("columns"):
        payload["columns"] = str(arguments["columns"])
    return payload


def run_query(arguments: Dict[str, Any], filter_value: str) -> Dict[str, Any]:
    payload = make_payload(arguments, filter_value)
    result = sentinelone_request(payload)
    if not arguments.get("include_headers"):
        result.pop("headers", None)
    max_items = parse_int(arguments.get("max_items_returned"), 200, 1, 5000)
    max_string_chars = parse_int(arguments.get("max_string_chars"), 4000, 200, 100000)
    trimmed_result = trim_value(result, max_items, max_string_chars)
    return {
        "request": payload,
        "response": trimmed_result,
    }


def run_search(arguments: Dict[str, Any], filter_value: str, name: str) -> Dict[str, Any]:
    response_mode = str(arguments.get("response_mode") or "summary").lower()
    if response_mode in {"raw", "exact"}:
        payload = run_query(arguments, filter_value)
        payload["response_mode"] = response_mode
        return payload
    if response_mode not in {"summary", "compact"}:
        raise ValueError("response_mode must be one of: summary, compact, raw, exact")
    payload = summarize_query(arguments, filter_value, name)
    payload["response_mode"] = response_mode
    return payload


def redact_log_summary(value: str) -> str:
    value = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<uuid>", value, flags=re.I)
    value = re.sub(r"\b\d{8,}\b", "<num>", value)
    value = re.sub(r"MAT[0-9A-Za-z]+", "<auth>", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", value)
    return value.replace("\n", " ")


def parse_log_message(message: str, max_chars: int) -> Dict[str, Any]:
    match = LOG_LINE_RE.search(message)
    if not match:
        return {"summary": redact_log_summary(message[:max_chars])}

    timestamp, level, thread, source, rest = match.groups()
    summary = rest.split("|", 1)[1].strip() if "|" in rest else rest.strip()
    return {
        "time": timestamp.strip(),
        "level": level,
        "thread": thread,
        "source": source,
        "summary": redact_log_summary(summary[:max_chars]),
    }


def summarize_query(arguments: Dict[str, Any], filter_value: str, name: str) -> Dict[str, Any]:
    payload = make_payload(arguments, filter_value)
    result = sentinelone_request(payload)
    body = result.get("body") if isinstance(result, dict) else {}
    if not isinstance(body, dict):
        return {"name": name, "request": payload, "response": result}

    matches = body.get("matches") or []
    if not isinstance(matches, list):
        matches = []

    max_samples = parse_int(arguments.get("max_samples"), 5, 0, 50)
    max_patterns = parse_int(arguments.get("max_patterns"), 8, 0, 50)
    max_summary_chars = parse_int(arguments.get("max_summary_chars"), 260, 80, 2000)

    pattern_counts: Dict[str, int] = {}
    samples: list[Dict[str, Any]] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        parsed = parse_log_message(str(item.get("message", "")), max_summary_chars)
        if len(samples) < max_samples:
            samples.append(parsed)
        pattern = f"{parsed.get('source', 'unknown')} | {parsed.get('summary', '')[:160]}"
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

    sessions = body.get("sessions") or {}
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
    return {
        "name": name,
        "request": payload,
        "status": result.get("status"),
        "api_status": body.get("status"),
        "returned": len(matches),
        "has_more": bool(body.get("continuationToken")),
        "continuation_token": body.get("continuationToken"),
        "versions": sorted(versions),
        "hosts": sorted(hosts),
        "samples": samples,
        "top_patterns": top_patterns,
        "mcp_telemetry": result.get("mcp_telemetry"),
    }


def summarize_query_safe(arguments: Dict[str, Any], filter_value: str, name: str) -> Dict[str, Any]:
    try:
        return summarize_query(arguments, filter_value, name)
    except Exception as exc:  # noqa: BLE001 - keep broad triage useful after one slow query
        try:
            payload: Dict[str, Any] = make_payload(arguments, filter_value)
        except Exception:  # noqa: BLE001 - preserve the original query failure
            payload = {"filter": filter_value}
        return {
            "name": name,
            "request": payload,
            "error": str(exc),
            "returned": 0,
            "has_more": False,
            "samples": [],
            "top_patterns": [],
        }


def require(arguments: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        if arguments.get(key) in (None, ""):
            return key
    return None


def search_logs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    missing = require(arguments, ["filter", "start_time", "end_time"])
    if missing:
        return tool_error(f"Missing required argument: {missing}")
    try:
        return tool_success(run_search(arguments, str(arguments["filter"]), "sentinelone_search_logs"))
    except Exception as exc:  # noqa: BLE001 - return MCP tool error, not crash server
        return tool_error(str(exc))


def search_platform_apollo_logs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    missing = require(arguments, ["deployment_version", "start_time", "end_time"])
    if missing:
        return tool_error(f"Missing required argument: {missing}")

    application = str(arguments.get("application") or "platform-apollo")
    deployment_version = str(arguments["deployment_version"])
    contains = str(arguments.get("contains") or "Logging pdt data")
    filter_value = (
        f"application=={quote_filter_value(application)} "
        f"DEPLOYMENT_VERSION=={quote_filter_value(deployment_version)} "
        f"{quote_filter_value(contains)}"
    )

    try:
        return tool_success(run_search(arguments, filter_value, "sentinelone_search_platform_apollo_logs"))
    except Exception as exc:  # noqa: BLE001 - return MCP tool error, not crash server
        return tool_error(str(exc))


def search_by_fields(arguments: Dict[str, Any]) -> Dict[str, Any]:
    missing = require(arguments, ["start_time", "end_time"])
    if missing:
        return tool_error(f"Missing required argument: {missing}")
    try:
        filter_value = build_filter_from_parts(arguments)
        return tool_success(run_search(arguments, filter_value, "sentinelone_search_by_fields"))
    except Exception as exc:  # noqa: BLE001 - return MCP tool error, not crash server
        return tool_error(str(exc))


def quick_platform_apollo_health(arguments: Dict[str, Any]) -> Dict[str, Any]:
    missing = require(arguments, ["start_time", "end_time"])
    if missing:
        return tool_error(f"Missing required argument: {missing}")

    application = str(arguments.get("application") or "platform-apollo")
    deployment_version = arguments.get("deployment_version")
    max_count = parse_int(arguments.get("max_count_per_query"), 30, 1, 200)

    base_args = dict(arguments)
    base_args["application"] = application
    base_args["max_count"] = max_count
    base_args["page_mode"] = arguments.get("page_mode") or "tail"
    base_args.setdefault("max_samples", 4)
    base_args.setdefault("max_patterns", 6)
    base_args.setdefault("max_summary_chars", 240)

    scoped: Dict[str, Any] = {"application": application}
    if deployment_version:
        scoped["deployment_version"] = str(deployment_version)

    query_specs: list[tuple[str, Dict[str, Any]]] = [
        ("fatal_severity_gte_6", {**scoped, "min_severity": 6}),
        ("errors_severity_gte_5", {**scoped, "min_severity": 5}),
    ]
    for term in as_string_list(arguments.get("focus_terms")):
        query_specs.append((f"contains_{re.sub(r'[^A-Za-z0-9]+', '_', term).strip('_').lower()}", {**scoped, "contains": term}))

    summaries: list[Dict[str, Any]] = []
    for name, filter_args in query_specs:
        query_args = {**base_args, **filter_args}
        filter_value = build_filter_from_parts(query_args)
        summaries.append(summarize_query_safe(query_args, filter_value, name))

    return tool_success(
        {
            "window": {"start_time": arguments["start_time"], "end_time": arguments["end_time"]},
            "application": application,
            "deployment_version": deployment_version,
            "strategy": (
                "Fast health check avoids unfiltered application probes. It samples severity>=6 first, "
                "then severity>=5 with small tail pages. Add focus_terms only after the error sample points "
                "to a specific phrase."
            ),
            "queries": summaries,
        }
    )


def triage_platform_apollo(arguments: Dict[str, Any]) -> Dict[str, Any]:
    missing = require(arguments, ["start_time", "end_time"])
    if missing:
        return tool_error(f"Missing required argument: {missing}")

    application = str(arguments.get("application") or "platform-apollo")
    deployment_version = arguments.get("deployment_version")
    max_count = parse_int(arguments.get("max_count_per_query"), 50, 1, 5000)
    include_application_probe = bool(arguments.get("include_application_probe"))
    include_warnings = bool(arguments.get("include_warnings"))

    base_args = dict(arguments)
    base_args["application"] = application
    base_args["max_count"] = max_count
    base_args["page_mode"] = arguments.get("page_mode") or "tail"

    focus_terms = as_string_list(arguments.get("focus_terms"))

    query_specs: list[tuple[str, Dict[str, Any]]] = []
    scoped: Dict[str, Any] = {"application": application}
    if deployment_version:
        scoped["deployment_version"] = str(deployment_version)
    if include_application_probe:
        query_specs.append(("application_probe", {"application": application}))
    query_specs.append(("fatal_severity_gte_6", {**scoped, "min_severity": 6}))
    query_specs.append(("errors_severity_gte_5", {**scoped, "min_severity": 5}))
    if include_warnings:
        query_specs.append(("warnings_severity_4", {**scoped, "severity": 4}))
    for term in focus_terms:
        query_specs.append((f"contains_{re.sub(r'[^A-Za-z0-9]+', '_', term).strip('_').lower()}", {**scoped, "contains": term}))

    summaries: list[Dict[str, Any]] = []
    for name, filter_args in query_specs:
        query_args = {**base_args, **filter_args}
        filter_value = build_filter_from_parts(query_args)
        summaries.append(summarize_query_safe(query_args, filter_value, name))
    return tool_success(
        {
            "window": {"start_time": arguments["start_time"], "end_time": arguments["end_time"]},
            "application": application,
            "deployment_version": deployment_version,
            "include_application_probe": include_application_probe,
            "include_warnings": include_warnings,
            "queries": summaries,
        }
    )


TOOLS = [
    {
        "name": "sentinelone_search_logs",
        "title": "Search SentinelOne Logs",
        "description": "Run a read-only SentinelOne/Scalyr log query against /api/query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "SentinelOne log filter expression, for example application=='platform-apollo' 'Logging pdt data'.",
                },
                "start_time": {"type": "string", "description": "Start timestamp, for example 2025-11-07T00:00:00."},
                "end_time": {"type": "string", "description": "End timestamp, for example 2025-11-13T00:00:00."},
                "query_type": {"type": "string", "default": "log"},
                "response_mode": {
                    "type": "string",
                    "default": "summary",
                    "enum": ["summary", "compact", "raw", "exact"],
                    "description": "summary/compact returns grouped samples; raw/exact returns exact rows.",
                },
                "columns": {
                    "type": "string",
                    "description": "Optional raw-mode field list, for example timestamp,severity,DEPLOYMENT_VERSION,message.",
                },
                "include_headers": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include SentinelOne HTTP response headers in raw output. Leave false unless debugging the API response.",
                },
                "max_count": {"type": "integer", "default": 100, "minimum": 1, "maximum": 5000},
                "page_mode": {"type": "string", "default": "tail", "enum": ["head", "tail"]},
                "continuation_token": {"type": ["string", "null"], "default": None},
                "max_items_returned": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Limits list items returned to Codex so large log responses do not flood context.",
                },
                "max_string_chars": {"type": "integer", "default": 4000, "minimum": 200, "maximum": 100000},
            },
            "required": ["filter", "start_time", "end_time"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sentinelone_search_by_fields",
        "title": "Search SentinelOne Logs By Fields",
        "description": "Build a SentinelOne log filter from structured fields, phrases, severity, and optional raw filter fragments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application": {"type": "string", "description": "Application field, for example platform-apollo."},
                "deployment_version": {"type": "string", "description": "DEPLOYMENT_VERSION field value."},
                "severity": {"type": "integer", "minimum": 0, "maximum": 100},
                "min_severity": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Minimum severity filter, emitted as severity>=N. Prefer this for ERROR/FATAL health checks.",
                },
                "severity_gte": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Alias for min_severity.",
                },
                "field_equals": {
                    "type": "object",
                    "description": "Additional exact field matches. Values are quoted unless numeric or boolean.",
                    "additionalProperties": {"type": ["string", "number", "boolean"]},
                },
                "contains": {
                    "description": "Phrase or list of phrases to require in the log line.",
                    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                },
                "extra_filter": {"type": "string", "description": "Raw SentinelOne filter fragment appended as-is."},
                "start_time": {"type": "string", "description": "Start timestamp, for example 2026-06-16T12:10:23."},
                "end_time": {"type": "string", "description": "End timestamp, for example 2026-06-16T12:37:22."},
                "query_type": {"type": "string", "default": "log"},
                "response_mode": {
                    "type": "string",
                    "default": "summary",
                    "enum": ["summary", "compact", "raw", "exact"],
                    "description": "summary/compact returns grouped samples; raw/exact returns exact rows.",
                },
                "columns": {
                    "type": "string",
                    "description": "Optional raw-mode field list, for example timestamp,severity,DEPLOYMENT_VERSION,message.",
                },
                "include_headers": {"type": "boolean", "default": False},
                "max_count": {"type": "integer", "default": 100, "minimum": 1, "maximum": 5000},
                "page_mode": {"type": "string", "default": "tail", "enum": ["head", "tail"]},
                "continuation_token": {"type": ["string", "null"], "default": None},
                "max_items_returned": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
                "max_string_chars": {"type": "integer", "default": 4000, "minimum": 200, "maximum": 100000},
            },
            "required": ["start_time", "end_time"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sentinelone_quick_platform_apollo_health",
        "title": "Quick Platform Apollo Health",
        "description": "Fast health check for recent Platform Apollo logs using small tail samples for severity>=6 and severity>=5.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application": {"type": "string", "default": "platform-apollo"},
                "deployment_version": {"type": "string", "description": "Optional release, for example platform-apollo-release-347."},
                "start_time": {"type": "string", "description": "Start timestamp. Use UTC for the API unless the tenant expects local time."},
                "end_time": {"type": "string", "description": "End timestamp. Use UTC for the API unless the tenant expects local time."},
                "focus_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional phrases to sample after the severity probes point to a specific pattern.",
                },
                "max_count_per_query": {"type": "integer", "default": 30, "minimum": 1, "maximum": 200},
                "max_samples": {"type": "integer", "default": 4, "minimum": 0, "maximum": 50},
                "max_patterns": {"type": "integer", "default": 6, "minimum": 0, "maximum": 50},
                "max_summary_chars": {"type": "integer", "default": 240, "minimum": 80, "maximum": 2000},
                "page_mode": {"type": "string", "default": "tail", "enum": ["head", "tail"]},
            },
            "required": ["start_time", "end_time"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sentinelone_search_platform_apollo_logs",
        "title": "Search Platform Apollo Logs",
        "description": "Convenience wrapper for application=='platform-apollo' DEPLOYMENT_VERSION==... and a contains phrase.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deployment_version": {
                    "type": "string",
                    "description": "Deployment version, for example platform-apollo-release-261.",
                },
                "contains": {"type": "string", "default": "Logging pdt data"},
                "application": {"type": "string", "default": "platform-apollo"},
                "start_time": {"type": "string", "description": "Start timestamp, for example 2025-11-07T00:00:00."},
                "end_time": {"type": "string", "description": "End timestamp, for example 2025-11-13T00:00:00."},
                "response_mode": {
                    "type": "string",
                    "default": "summary",
                    "enum": ["summary", "compact", "raw", "exact"],
                    "description": "summary/compact returns grouped samples; raw/exact returns exact rows.",
                },
                "columns": {
                    "type": "string",
                    "description": "Optional raw-mode field list, for example timestamp,severity,DEPLOYMENT_VERSION,message.",
                },
                "include_headers": {"type": "boolean", "default": False},
                "max_count": {"type": "integer", "default": 100, "minimum": 1, "maximum": 5000},
                "page_mode": {"type": "string", "default": "tail", "enum": ["head", "tail"]},
                "continuation_token": {"type": ["string", "null"], "default": None},
                "max_items_returned": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
                "max_string_chars": {"type": "integer", "default": 4000, "minimum": 200, "maximum": 100000},
            },
            "required": ["deployment_version", "start_time", "end_time"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sentinelone_triage_platform_apollo",
        "title": "Triage Platform Apollo Logs",
        "description": "Run bounded Platform Apollo triage and return redacted samples plus recurring patterns; broad probes and warnings are opt-in.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "application": {"type": "string", "default": "platform-apollo"},
                "deployment_version": {"type": "string", "description": "Optional release, for example platform-apollo-release-344."},
                "start_time": {"type": "string", "description": "Start timestamp. Use UTC for the API unless the tenant expects local time."},
                "end_time": {"type": "string", "description": "End timestamp. Use UTC for the API unless the tenant expects local time."},
                "focus_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional phrases/classes to probe in addition to severity>=6 and severity>=5 scans.",
                },
                "include_application_probe": {
                    "type": "boolean",
                    "default": False,
                    "description": "Run an unfiltered application probe. Leave false for high-volume recent windows.",
                },
                "include_warnings": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also run severity==4. Leave false unless warnings are explicitly needed.",
                },
                "max_count_per_query": {"type": "integer", "default": 50, "minimum": 1, "maximum": 5000},
                "max_samples": {"type": "integer", "default": 5, "minimum": 0, "maximum": 50},
                "max_patterns": {"type": "integer", "default": 8, "minimum": 0, "maximum": 50},
                "max_summary_chars": {"type": "integer", "default": 260, "minimum": 80, "maximum": 2000},
                "page_mode": {"type": "string", "default": "tail", "enum": ["head", "tail"]},
            },
            "required": ["start_time", "end_time"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


def initialize(request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    requested_version = params.get("protocolVersion") or PROTOCOL_VERSION
    return response(
        request_id,
        {
            "protocolVersion": requested_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Read-only access to SentinelOne/Scalyr logs through the query API. Use tight time windows, "
                "structured filters, and conservative max_count values. For recent health checks, prefer "
                "sentinelone_quick_platform_apollo_health before raw search. Prefer triage tools for broad "
                "post-deploy checks because they redact and group high-volume log messages."
            ),
        },
    )


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return initialize(request_id, params)
    if method == "ping":
        return response(request_id, {})
    if method == "tools/list":
        return response(request_id, {"tools": TOOLS})
    if method == "prompts/list":
        return response(request_id, {"prompts": []})
    if method == "resources/list":
        return response(request_id, {"resources": []})
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        started_at = time.perf_counter()
        if tool_name == "sentinelone_search_logs":
            return response(request_id, attach_tool_telemetry(tool_name, arguments, started_at, search_logs(arguments)))
        if tool_name == "sentinelone_search_by_fields":
            return response(request_id, attach_tool_telemetry(tool_name, arguments, started_at, search_by_fields(arguments)))
        if tool_name == "sentinelone_quick_platform_apollo_health":
            return response(request_id, attach_tool_telemetry(tool_name, arguments, started_at, quick_platform_apollo_health(arguments)))
        if tool_name == "sentinelone_search_platform_apollo_logs":
            return response(request_id, attach_tool_telemetry(tool_name, arguments, started_at, search_platform_apollo_logs(arguments)))
        if tool_name == "sentinelone_triage_platform_apollo":
            return response(request_id, attach_tool_telemetry(tool_name, arguments, started_at, triage_platform_apollo(arguments)))
        return error_response(request_id, -32602, f"Unknown tool: {tool_name}")

    if method and method.startswith("notifications/"):
        return None
    return error_response(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            reply = handle_request(message)
            if reply is not None:
                send(reply)
        except Exception:  # noqa: BLE001 - keep MCP server alive after malformed calls
            log(traceback.format_exc())
            try:
                request_id = json.loads(line).get("id")
            except Exception:  # noqa: BLE001
                request_id = None
            send(error_response(request_id, -32603, "Internal error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

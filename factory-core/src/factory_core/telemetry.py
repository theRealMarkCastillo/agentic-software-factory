"""Append-only JSONL telemetry with an optional OTLP/HTTP export."""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from .state_manager import utc_now

OTLP_ENV = "FACTORY_OTLP_ENDPOINT"


def detect_platform() -> str:
    explicit = os.environ.get("FACTORY_PLATFORM")
    if explicit:
        return explicit
    if os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude-code"
    if any(key.startswith("CODEX_") for key in os.environ):
        return "codex"
    if any(key.startswith("ANTIGRAVITY") for key in os.environ):
        return "antigravity"
    return "cli"


def _export_otlp(endpoint: str, event: dict[str, Any]) -> None:
    """Best effort: telemetry must never change a gate outcome."""
    end_ns = time.time_ns()
    start_ns = end_ns - int(event.get("duration_ms", 0)) * 1_000_000
    attributes = [
        {"key": f"factory.{key}", "value": {"stringValue": str(value)}}
        for key, value in event.items()
        if value is not None
    ]
    body = {
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "factory-core"}}]},
            "scopeSpans": [{
                "scope": {"name": "factory_core"},
                "spans": [{
                    "traceId": secrets.token_hex(16),
                    "spanId": secrets.token_hex(8),
                    "name": "holdout-gate",
                    "kind": 1,
                    "startTimeUnixNano": str(start_ns),
                    "endTimeUnixNano": str(end_ns),
                    "attributes": attributes,
                    "status": {"code": 1 if event.get("gate_result") == "pass" else 2},
                }],
            }],
        }]
    }
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except (urllib.error.URLError, OSError, ValueError):
        pass


def record(
    holdout_dir: str | Path,
    task_id: str,
    attempt: int,
    duration_ms: int,
    gate_result: str,
    failed_contract_id: str | None,
    tree_hash: str,
    platform: str | None = None,
) -> dict[str, Any]:
    event = {
        "timestamp": utc_now(),
        "task_id": task_id,
        "platform": platform or detect_platform(),
        "attempt": attempt,
        "duration_ms": duration_ms,
        "gate_result": gate_result,
        "failed_contract_id": failed_contract_id,
        "tree_hash": tree_hash,
    }
    path = Path(holdout_dir) / "telemetry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(event) + "\n")
    endpoint = os.environ.get(OTLP_ENV)
    if endpoint:
        _export_otlp(endpoint, event)
    return event


def read_events(holdout_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(holdout_dir) / "telemetry.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def summarize(holdout_dir: str | Path) -> dict[str, Any]:
    """Aggregate metrics. Contract ids are kept; no sealed content is stored here."""
    events = read_events(holdout_dir)
    passed = sum(1 for e in events if e.get("gate_result") == "pass")
    failed = sum(1 for e in events if e.get("gate_result") == "fail")
    first_pass = sum(1 for e in events if e.get("gate_result") == "pass" and e.get("attempt") == 0)
    contracts = Counter(e["failed_contract_id"] for e in events if e.get("failed_contract_id"))
    durations = [int(e.get("duration_ms", 0)) for e in events]
    total = passed + failed
    return {
        "runs": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total, 3) if total else None,
        "first_pass_count": first_pass,
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
        "top_failed_contracts": contracts.most_common(5),
    }

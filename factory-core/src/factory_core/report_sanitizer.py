"""Reduce raw hold-out logs to behavioral contract failures."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

MAX_LINES = 60
SECRET_KEY = re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token|credential)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
COMPOSE_PREFIX = re.compile(r"^(?P<service>[\w.-]+?)(?:-\d+)?\s+\|\s?(?P<line>.*)$")
FRAME_PYTEST = re.compile(r"^(?:\S*/)?(?:holdout/)?suites/(?P<file>[\w./-]+\.py):\d+: in (?P<func>\S+)")
FRAME_PYTEST_BARE = re.compile(r"^(?:\S*/)?(?:holdout/)?suites/(?P<file>[\w./-]+\.py):\d+:.*$")
FRAME_PYTHON = re.compile(r'File ".*?(?:holdout/)?suites/([^"]+)", line \d+, in (\w+)')
FAILED_ID = re.compile(r"(?:FAILED|ERROR)\s+\S*?([\w-]+\.py::[^\s]+)")


def _redact_secrets(text: str) -> str:
    text = re.sub(
        r"(?i)([\"']?\b[\w-]*(?:api[_-]?key|password|passwd|secret|token)[\w-]*[\"']?\s*[=:]\s*)"
        r"([\"'])[^\"']*\2",
        lambda m: f'{m[1]}"[REDACTED_SECRET]"',
        text,
    )
    text = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", r"\1 [REDACTED_SECRET]", text)
    return text


def _strip_frames(lines: Iterable[str]) -> list[str]:
    """Replace suite frames with a contract label and drop the test source lines."""
    kept: list[str] = []
    in_frame = False
    for line in lines:
        match = FRAME_PYTEST.match(line.strip())
        if match:
            kept.append(f"In Behavioral Contract Check ({match['file']} -> {match['func']})")
            in_frame = True
            continue
        if FRAME_PYTEST_BARE.match(line.strip()):
            in_frame = True
            continue
        if in_frame and (line.startswith("    ") or not line.strip()):
            continue  # Source echoed from the sealed test file.
        in_frame = False
        # pytest's long format marks the failing source line with ">".
        if re.match(r"^>\s", line):
            continue
        kept.append(FRAME_PYTHON.sub(r"In Behavioral Contract Check (\1 -> \2)", line))
    return kept


def _verifier_lines(raw_text: str) -> list[str]:
    """Prefer the verifier's stream; fall back to the raw log for build failures."""
    verifier: list[str] = []
    other: list[str] = []
    saw_prefix = False
    for line in ANSI.sub("", raw_text).splitlines():
        match = COMPOSE_PREFIX.match(line)
        if match:
            saw_prefix = True
            (verifier if match["service"] == "verifier" else other).append(match["line"])
        else:
            other.append(line)
    if saw_prefix and verifier:
        return verifier
    return other[-MAX_LINES:]


def sanitize_output(raw_text: str, repo_slug: str = "default", secrets: Iterable[str] = ()) -> str:
    lines = _strip_frames(_verifier_lines(raw_text))
    sanitized = "\n".join(lines)

    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "[REDACTED_SECRET]")

    sanitized = re.sub(r"(?:/[^/\s:\"']+)*/\.factory-holdouts(?:/[^\s:\"']*)?", "[SEALED_HOLDOUT_FILE]", sanitized)
    sanitized = re.sub(r"(?:/private)?/tmp/sut-[a-f0-9-]+/?", "", sanitized)
    sanitized = re.sub(r"(?:/private)?/tmp/idx-[a-f0-9-]+", "[TEMP_INDEX]", sanitized)
    sanitized = re.sub(r"/holdout/(?:suites|fixtures)/[^\s:\"']+", "[SEALED_HOLDOUT_FILE]", sanitized)
    sanitized = re.sub(r"(?<![\w/])fixtures/[\w./-]+", "[SEALED_HOLDOUT_FILE]", sanitized)
    sanitized = re.sub(r"/(?:Users|home)/[^/\s:\"']+", "[HOST_HOME]", sanitized)
    sanitized = _redact_secrets(sanitized)

    result = sanitized.splitlines()
    if len(result) > MAX_LINES:
        result = ["[... earlier output truncated ...]", *result[-MAX_LINES:]]
    return "\n".join(result).strip()


def failed_contracts(raw_text: str) -> list[str]:
    """Return contract identifiers such as test_api.py::test_health."""
    found: list[str] = []
    for contract in FAILED_ID.findall(ANSI.sub("", raw_text)):
        if contract not in found:
            found.append(contract)
    return found


def _collect_secret_values(value: Any, key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _collect_secret_values(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _collect_secret_values(child, key)
    elif isinstance(value, str) and SECRET_KEY.search(key):
        yield value


def fixture_secrets(fixtures_dir: str | Path) -> set[str]:
    """Read secret-named values from golden JSON fixtures for literal redaction."""
    secrets: set[str] = set()
    for path in Path(fixtures_dir).rglob("*.json"):
        try:
            secrets.update(_collect_secret_values(json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    return secrets


def main() -> None:
    raw_log = sys.stdin.read()
    repo_slug = sys.argv[1] if len(sys.argv) > 1 else "default"
    sys.stdout.write(sanitize_output(raw_log, repo_slug))


if __name__ == "__main__":
    main()

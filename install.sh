#!/usr/bin/env bash
# Bootstrap the agentic software factory on this workstation.
#
#   ./install.sh --core-only                       install factory-core only (default)
#   ./install.sh --platform claude|codex|antigravity|all
#
# Docker Engine and Docker Compose v2 are hard prerequisites. This script exits 2
# without installing anything when either is unavailable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
HOLDOUTS="$HOME/.factory-holdouts"
PLUGIN="agentic-software-factory"
PLATFORM=""
CORE_ONLY=0

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

fail() {
  echo "install.sh: $1" >&2
  [ $# -gt 1 ] && echo "  Fix: $2" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --platform)
      [ $# -ge 2 ] || fail "--platform needs a value" "Use claude, codex, antigravity, or all."
      PLATFORM="$2"; shift 2 ;;
    --platform=*) PLATFORM="${1#*=}"; shift ;;
    --core-only) CORE_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" "Run ./install.sh --help" ;;
  esac
done

if [ "$CORE_ONLY" -eq 1 ] && [ -n "$PLATFORM" ]; then
  fail "--core-only and --platform cannot be combined"
fi
case "$PLATFORM" in
  ""|claude|codex|antigravity|all) ;;
  *) fail "unknown platform: $PLATFORM" "Use claude, codex, antigravity, or all." ;;
esac

# --- 1. Mandatory prerequisites -------------------------------------------------
command -v git >/dev/null 2>&1 || fail "git is not installed" "Install Git 2.30+."
command -v docker >/dev/null 2>&1 || fail "docker is not installed" "Install Docker Engine or Docker Desktop."
docker info >/dev/null 2>&1 || fail "Docker daemon is not responding" "Start Docker Engine, then rerun ./install.sh"
compose_version="$(docker compose version --short 2>/dev/null || true)"
case "${compose_version#v}" in
  ""|0.*|1.*) fail "Docker Compose v2 is required (found: ${compose_version:-none})" \
                   "Install the Compose plugin: https://docs.docker.com/compose/install/" ;;
esac
echo "Docker Engine and Compose ${compose_version#v} verified."

# --- 2. factory-core ------------------------------------------------------------
find_python() {
  local candidate
  for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

mkdir -p "$BIN_DIR"
if command -v uv >/dev/null 2>&1; then
  UV_TOOL_BIN_DIR="$BIN_DIR" uv tool install --force --reinstall --quiet --python ">=3.11" "$ROOT/factory-core"
else
  python_bin="$(find_python)" || fail "Python 3.11+ was not found" "Install Python 3.11 or newer."
  venv="$HOME/.local/share/factory-core/venv"
  rm -rf "$venv"
  "$python_bin" -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet "$ROOT/factory-core"
  ln -sf "$venv/bin/factory-core" "$BIN_DIR/factory-core"
fi
[ -x "$BIN_DIR/factory-core" ] || fail "factory-core was not installed to $BIN_DIR"
echo "Installed factory-core to $BIN_DIR/factory-core"

mkdir -p "$HOLDOUTS"
chmod 700 "$HOLDOUTS"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: $BIN_DIR is not on PATH for this shell; adding it for the checks below." >&2
     export PATH="$BIN_DIR:$PATH" ;;
esac
"$BIN_DIR/factory-core" doctor

# --- 3. Platform plugins --------------------------------------------------------
install_claude() {
  if ! command -v claude >/dev/null 2>&1; then
    echo "Claude Code CLI not found. Inside Claude Code run:" >&2
    echo "  /plugin marketplace add $ROOT" >&2
    echo "  /plugin install $PLUGIN@$PLUGIN" >&2
    return 1
  fi
  claude plugin marketplace add "$ROOT" >/dev/null 2>&1 || claude plugin marketplace update "$PLUGIN" >/dev/null || return 1
  claude plugin install "$PLUGIN@$PLUGIN" >/dev/null || return 1
  echo "Claude Code: installed $PLUGIN@$PLUGIN"
}

install_codex() {
  local codex_home="${CODEX_HOME:-$HOME/.codex}"
  mkdir -p "$codex_home/agents" || return 1
  cp "$ROOT"/plugins/codex/agents/*.toml "$codex_home/agents/" || return 1
  if ! command -v codex >/dev/null 2>&1; then
    echo "Codex CLI not found. Copied agents to $codex_home/agents. Then run:" >&2
    echo "  codex plugin marketplace add $ROOT && codex plugin add $PLUGIN@$PLUGIN" >&2
    return 1
  fi
  codex plugin marketplace add "$ROOT" >/dev/null || return 1
  codex plugin add "$PLUGIN@$PLUGIN" >/dev/null || return 1
  echo "Codex: installed $PLUGIN@$PLUGIN and agents in $codex_home/agents"
  echo "       Approve the hooks trust prompt on first run. See plugins/codex/config/factory.toml."
}

install_antigravity() {
  local dest="$HOME/.gemini/config/plugins/$PLUGIN"
  if command -v agy >/dev/null 2>&1; then
    agy plugin install "$ROOT/plugins/antigravity" >/dev/null || return 1
    echo "Antigravity: installed $PLUGIN with agy"
    return 0
  fi
  rm -rf "$dest" || return 1
  mkdir -p "$(dirname "$dest")" || return 1
  cp -R "$ROOT/plugins/antigravity" "$dest" || return 1
  echo "Antigravity: copied plugin to $dest"
}

status=0
case "$PLATFORM" in
  "") echo "Core only. Add a harness with: ./install.sh --platform claude|codex|antigravity|all" ;;
  claude) install_claude || status=1 ;;
  codex) install_codex || status=1 ;;
  antigravity) install_antigravity || status=1 ;;
  all)
    install_claude || status=1
    install_codex || status=1
    install_antigravity || status=1 ;;
esac

echo "Next: run 'factory-core init' inside each repository you want the factory to manage."
exit "$status"

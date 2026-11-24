"""Static checks on plugin bundles, manifests, and the hold-out template."""

import json
import os
import re
import shutil
import subprocess
import tomllib

import pytest
from conftest import REPO_ROOT

PLUGINS = REPO_ROOT / "plugins"
TEMPLATE = REPO_ROOT / "templates" / "holdout-repo-template"
SKILLS = ("factory-spec-plan", "factory-implement", "factory-triage")


def load(path):
    return json.loads((REPO_ROOT / path).read_text())


def commands(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "command" and isinstance(child, str):
                yield child
            else:
                yield from commands(child)
    elif isinstance(value, list):
        for child in value:
            yield from commands(child)


@pytest.mark.parametrize("path", [
    ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json",
    "plugins/claude-code/.claude-plugin/plugin.json", "plugins/claude-code/hooks/hooks.json", "plugins/claude-code/.mcp.json",
    "plugins/codex/.codex-plugin/plugin.json", "plugins/codex/hooks/hooks.json", "plugins/codex/.mcp.json",
    "plugins/antigravity/plugin.json", "plugins/antigravity/hooks.json", "plugins/antigravity/mcp_config.json",
])
def test_json_manifests_parse(path):
    assert isinstance(load(path), dict)


@pytest.mark.parametrize("plugin", ["claude-code", "codex", "antigravity"])
def test_bundled_skills_match_the_canonical_copies(plugin):
    for skill in SKILLS:
        canonical = (REPO_ROOT / "shared-skills" / skill / "SKILL.md").read_text()
        assert (PLUGINS / plugin / "skills" / skill / "SKILL.md").read_text() == canonical
        frontmatter = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", canonical)
        assert frontmatter and frontmatter[1] == skill


def test_skills_require_state_reconciliation_after_compaction():
    for skill in ("factory-implement", "factory-triage"):
        text = (REPO_ROOT / "shared-skills" / skill / "SKILL.md").read_text()
        assert "rejected_approaches[]" in text and "git status" in text


def test_versions_agree():
    versions = {
        load("plugins/claude-code/.claude-plugin/plugin.json")["version"],
        load("plugins/codex/.codex-plugin/plugin.json")["version"],
        load("plugins/antigravity/plugin.json")["version"],
        load(".claude-plugin/marketplace.json")["plugins"][0]["version"],
        tomllib.loads((REPO_ROOT / "factory-core" / "pyproject.toml").read_text())["project"]["version"],
    }
    assert versions == {"2.4.0"}


def test_marketplaces_point_at_the_plugin_directories():
    claude = load(".claude-plugin/marketplace.json")["plugins"][0]
    assert claude["source"] == "./plugins/claude-code" and (REPO_ROOT / claude["source"] / ".claude-plugin" / "plugin.json").is_file()
    codex = load(".agents/plugins/marketplace.json")["plugins"][0]
    assert (REPO_ROOT / codex["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()


def test_claude_hooks_use_nested_matcher_groups_for_every_event():
    hooks = load("plugins/claude-code/hooks/hooks.json")["hooks"]
    assert set(hooks) >= {"WorktreeCreate", "PreToolUse", "PostToolUse", "PostToolBatch", "PostCompact", "SubagentStop", "Stop"}
    for event, groups in hooks.items():
        assert isinstance(groups, list) and groups, event
        for group in groups:
            assert all(h["type"] == "command" and h["command"] for h in group["hooks"]), event
    assert hooks["PreToolUse"][0]["matcher"] == ".*"
    assert "factory-core guard" in hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert "factory-core holdout-gate" in hooks["Stop"][0]["hooks"][0]["command"]
    assert hooks["SubagentStop"][0]["matcher"] == "adversarial-critic"
    for event in ("Stop", "WorktreeCreate", "PostToolBatch"):
        assert "matcher" not in hooks[event][0], f"{event} does not support matchers"


def test_codex_and_antigravity_hook_bindings():
    codex = load("plugins/codex/hooks/hooks.json")["hooks"]
    assert codex["PreToolUse"][0]["matcher"] == ".*"
    assert codex["PostToolUse"][0]["matcher"] == "^apply_patch$"
    assert set(codex) == {"PreToolUse", "PostToolUse", "Stop"}
    agy = load("plugins/antigravity/hooks.json")
    assert "hooks" not in agy, "Antigravity names each hook at the top level"
    events = {event for hook in agy.values() for event in hook}
    assert events == {"PreToolUse", "PostToolUse", "PostInvocation", "Stop"}
    matcher = agy["factory-guard"]["PreToolUse"][0]["matcher"]
    assert matcher == "*"
    post_matcher = agy["factory-fast-check"]["PostToolUse"][0]["matcher"]
    assert post_matcher == "write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content"
    post_inv_cmd = agy["factory-fast-check"]["PostInvocation"][0]["command"]
    assert "factory-core fast-check --post-invocation --platform antigravity" in post_inv_cmd


@pytest.mark.parametrize("path", ["plugins/claude-code/hooks/hooks.json", "plugins/codex/hooks/hooks.json"])
def test_guard_and_gate_fail_closed_when_factory_core_is_missing(path):
    found = {"guard": False, "holdout-gate": False}
    for command in commands(load(path)):
        for name in found:
            if command.endswith(f"factory-core {name}"):
                found[name] = True
                assert "exit 2" in command and "not installed" in command
    assert all(found.values())


def test_antigravity_hooks_fail_closed_with_exit_0_json_when_factory_core_is_missing(tmp_path):
    hooks = load("plugins/antigravity/hooks.json")
    guard_cmd = hooks["factory-guard"]["PreToolUse"][0]["hooks"][0]["command"]
    fast_cmd = hooks["factory-fast-check"]["PostToolUse"][0]["hooks"][0]["command"]
    gate_cmd = hooks["factory-holdout-gate"]["Stop"][0]["command"]
    assert "factory-core guard --platform antigravity" in guard_cmd
    assert "factory-core fast-check --hook --platform antigravity" in fast_cmd
    assert "factory-core holdout-gate --platform antigravity" in gate_cmd

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    env = {"HOME": str(empty_home), "PATH": "/usr/bin:/bin"}

    guard_res = subprocess.run(["sh", "-c", guard_cmd], input="{}", text=True, capture_output=True, env=env, check=False)
    assert guard_res.returncode == 0
    guard_json = json.loads(guard_res.stdout)
    assert guard_json["decision"] == "deny" and "not installed" in guard_json["reason"]

    fast_res = subprocess.run(["sh", "-c", fast_cmd], input="{}", text=True, capture_output=True, env=env, check=False)
    assert fast_res.returncode == 0 and json.loads(fast_res.stdout) == {}

    gate_res = subprocess.run(["sh", "-c", gate_cmd], input='{"fullyIdle": true}', text=True, capture_output=True, env=env, check=False)
    assert gate_res.returncode == 0
    gate_json = json.loads(gate_res.stdout)
    assert gate_json["decision"] == "continue" and "not installed" in gate_json["reason"]

    gate_loop = subprocess.run(
        ["sh", "-c", gate_cmd], input='{"fullyIdle": true, "continuationActive": true}',
        text=True, capture_output=True, env=env, check=False,
    )
    assert gate_loop.returncode == 0 and json.loads(gate_loop.stdout) == {"decision": "stop"}

    gate_canceled = subprocess.run(
        ["sh", "-c", gate_cmd], input='{"fullyIdle": true, "terminationReason": "USER_CANCELED"}',
        text=True, capture_output=True, env=env, check=False,
    )
    assert gate_canceled.returncode == 0 and json.loads(gate_canceled.stdout) == {"decision": "stop"}


@pytest.mark.skipif(shutil.which("agy") is None, reason="agy CLI not installed")
def test_antigravity_plugin_passes_agy_strict_validation():
    proc = subprocess.run(
        ["agy", "plugin", "validate", "--strict", "--json", str(PLUGINS / "antigravity")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["name"] == "agentic-software-factory"
    assert report["components"] == {"agents": 2, "hooks": 3, "mcpServers": 1, "rules": 1, "sidecars": 0, "skills": 3}
    assert report["findings"] == []


def test_mcp_registration():
    assert load("plugins/antigravity/mcp_config.json") == {
        "mcpServers": {"factory-core": {"command": "factory-core", "args": ["mcp"]}}
    }
    for path in ("plugins/claude-code/.mcp.json", "plugins/codex/.mcp.json"):
        cfg = load(path)["mcpServers"]["factory-core"]
        assert cfg["command"] == "sh" and "exec factory-core mcp" in cfg["args"][1]


def test_claude_plugin_has_icon():
    assert (PLUGINS / "claude-code" / ".claude-plugin" / "icon.png").is_file()


def test_codex_agents_and_config_are_valid_toml():
    critic = tomllib.loads((PLUGINS / "codex" / "agents" / "adversarial-critic.toml").read_text())
    assert critic["name"] == "adversarial-critic" and critic["sandbox_mode"] == "read-only"
    assert ".factory-triage.md" in critic["developer_instructions"]
    implementer = tomllib.loads((PLUGINS / "codex" / "agents" / "implementer.toml").read_text())
    assert implementer["name"] == "implementer" and "factory-holdouts" in implementer["developer_instructions"]
    config = tomllib.loads((PLUGINS / "codex" / "config" / "factory.toml").read_text())
    assert config["features"]["hooks"] is True
    assert config["mcp_servers"]["factory-core"]["command"] == "sh"
    assert "exec factory-core mcp" in config["mcp_servers"]["factory-core"]["args"][1]


@pytest.mark.parametrize("plugin", ["claude-code", "antigravity"])
def test_markdown_agents_have_frontmatter_and_restrictions(plugin):
    for name in ("implementer", "adversarial-critic"):
        text = (PLUGINS / plugin / "agents" / f"{name}.md").read_text()
        assert text.startswith(f"---\nname: {name}\ndescription: ")
        assert "~/.factory-holdouts" in text
    critic = (PLUGINS / plugin / "agents" / "adversarial-critic.md").read_text()
    frontmatter = critic.split("---")[1]
    assert "Edit" not in frontmatter and "replace_file_content" not in frontmatter
    for section in ("Root Cause Analysis", "Unmet Behavioral Contract Clause", "Target Modification Strategy"):
        assert section in critic


def test_antigravity_rule_is_always_on():
    text = (PLUGINS / "antigravity" / "rules" / "factory-invariants.md").read_text()
    assert text.startswith("---\ntrigger: always_on\n---\n")


def test_compose_template_is_internal_only_with_no_host_ports():
    text = (TEMPLATE / "docker-compose.holdout.yml").read_text()
    assert not re.search(r"^\s*ports\s*:", text, re.MULTILINE)
    assert "internal: true" in text and "driver: bridge" in text
    assert "context: ${CLEAN_REPO_DIR}" in text
    assert "./suites:/holdout/suites:ro" in text and "./fixtures:/holdout/fixtures:ro" in text
    assert "SUT_BASE_URL=http://sut:8080" in text and "condition: service_healthy" in text
    assert "read_only: true" in text


def test_verifier_entrypoint_and_compose_command_do_not_double_invoke_pytest():
    dockerfile = (TEMPLATE / "Dockerfile.verifier").read_text()
    assert 'ENTRYPOINT ["pytest"]' in dockerfile
    command = re.search(r"^\s*command:\s*(\[.*\])$", (TEMPLATE / "docker-compose.holdout.yml").read_text(), re.MULTILINE)
    assert json.loads(command[1])[0] != "pytest"


def test_template_files_exist():
    for name in ("Dockerfile.verifier", "telemetry.jsonl", "suites/test_contract_example.py", "fixtures/golden_example.json"):
        assert (TEMPLATE / name).is_file()
    json.loads((TEMPLATE / "fixtures" / "golden_example.json").read_text())


def test_install_script_is_executable_and_supports_documented_flags():
    script = REPO_ROOT / "install.sh"
    assert os.access(script, os.X_OK)
    text = script.read_text()
    for flag in ("--platform", "--core-only", "claude", "codex", "antigravity", "all"):
        assert flag in text
    assert text.index("docker info") < text.index("uv tool install"), "Docker must be verified before installing"

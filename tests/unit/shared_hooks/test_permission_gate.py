"""UT-PMG-001 ~ UT-PMG-010: PermissionGate unit tests."""

import pytest

from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext

from shared_hooks.permission_gate import PermissionGate


def _tool_ctx(tool_name):
    return HookContext(
        event_type=EventType.BEFORE_TOOL_CALL,
        tool_name=tool_name,
    )


class TestThreeLevels:
    def test_pmg001_deny_blocked(self):
        gate = PermissionGate(tools={"shell_executor": "deny"})
        with pytest.raises(GuardrailDeny, match="permission_denied"):
            gate.before_tool_handler(_tool_ctx("shell_executor"))

    def test_pmg002_allow_passes(self):
        gate = PermissionGate(tools={"knowledge_search": "allow"})
        gate.before_tool_handler(_tool_ctx("knowledge_search"))

    def test_pmg003_warn_passes_with_record(self):
        gate = PermissionGate(tools={"file_reader": "warn"})
        gate.before_tool_handler(_tool_ctx("file_reader"))
        assert len(gate.decisions) >= 1
        assert gate.decisions[-1]["permission"] == "warn"


class TestDefaultPolicy:
    def test_pmg004_default_warn(self):
        gate = PermissionGate(
            tools={"shell_executor": "deny"}, default="warn"
        )
        gate.before_tool_handler(_tool_ctx("new_tool"))
        assert gate.decisions[-1]["policy_source"] == "default"
        assert gate.decisions[-1]["permission"] == "warn"

    def test_pmg005_default_deny(self):
        gate = PermissionGate(default="deny")
        with pytest.raises(GuardrailDeny):
            gate.before_tool_handler(_tool_ctx("any_tool"))

    def test_pmg006_default_allow(self):
        gate = PermissionGate(default="allow")
        gate.before_tool_handler(_tool_ctx("any_tool"))


class TestYamlLoading:
    def test_pmg007_load_from_yaml(self, tmp_path):
        policy_file = tmp_path / "security.yaml"
        policy_file.write_text(
            "permissions:\n"
            "  default: warn\n"
            "  tools:\n"
            "    knowledge_search: allow\n"
            "    shell_executor: deny\n"
        )
        gate = PermissionGate.from_yaml(policy_file)
        gate.before_tool_handler(_tool_ctx("knowledge_search"))
        with pytest.raises(GuardrailDeny):
            gate.before_tool_handler(_tool_ctx("shell_executor"))

    def test_pmg008_yaml_default_overrides_constructor(self, tmp_path):
        policy_file = tmp_path / "security.yaml"
        policy_file.write_text(
            "permissions:\n"
            "  default: deny\n"
            "  tools: {}\n"
        )
        gate = PermissionGate.from_yaml(policy_file)
        with pytest.raises(GuardrailDeny):
            gate.before_tool_handler(_tool_ctx("unlisted_tool"))


class TestToolNameCase:
    def test_pmg009_case_insensitive(self):
        gate = PermissionGate(tools={"Shell_Executor": "deny"})
        with pytest.raises(GuardrailDeny):
            gate.before_tool_handler(_tool_ctx("shell_executor"))


class TestMetrics:
    def test_pmg010_metrics(self):
        gate = PermissionGate(
            tools={"a": "allow", "b": "allow", "c": "allow", "d": "deny"}
        )
        for name in ["a", "b", "c"]:
            gate.before_tool_handler(_tool_ctx(name))
        with pytest.raises(GuardrailDeny):
            gate.before_tool_handler(_tool_ctx("d"))
        m = gate.get_metrics()
        assert m["total_decisions"] == 4
        assert m["allow_count"] == 3
        assert m["deny_count"] == 1
        assert "d" in m["denied_tools"]

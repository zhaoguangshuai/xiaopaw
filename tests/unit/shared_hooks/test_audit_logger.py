"""UT-AUD-001 ~ UT-AUD-007: SecurityAuditLogger unit tests."""

import json

import pytest

from xiaopaw.hook_framework.registry import EventType, HookContext

from shared_hooks.audit_logger import SecurityAuditLogger


class TestAuditLogger:
    def test_aud001_record_events_in_memory(self):
        logger = SecurityAuditLogger()
        logger.record_event("permission_deny", tool="shell", detail="blocked")
        logger.record_event("permission_deny", tool="rm", detail="blocked")
        logger.record_event("sandbox_violation", tool="cat", detail="path")
        m = logger.get_metrics()
        assert m["total_security_events"] == 3
        assert m["events_by_type"]["permission_deny"] == 2
        assert m["events_by_type"]["sandbox_violation"] == 1

    def test_aud002_write_to_jsonl(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(audit_file=path)
        logger.record_event("permission_deny", tool="shell")
        logger.record_event("sandbox_violation", tool="cat")
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            data = json.loads(line)
            assert "timestamp" in data
            assert "security_event" in data

    def test_aud003_no_file_memory_only(self, tmp_path):
        logger = SecurityAuditLogger()
        logger.record_event("test_event")
        m = logger.get_metrics()
        assert m["total_security_events"] == 1

    def test_aud004_session_end_summary(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(audit_file=path)
        logger.record_event("permission_deny", tool="x")
        logger.record_event("sandbox_violation", tool="y")
        ctx = HookContext(
            event_type=EventType.SESSION_END,
            session_id="p2p:ou_test",
        )
        logger.session_end_handler(ctx)
        lines = path.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["security_event"] == "session_summary"
        assert last["total_security_events"] == 2

    def test_aud005_session_id_from_context(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(audit_file=path)
        ctx = HookContext(
            event_type=EventType.SESSION_END,
            session_id="p2p:ou_test123",
        )
        logger.session_end_handler(ctx)
        lines = path.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["session_id"] == "p2p:ou_test123"

    def test_aud006_unwritable_path_no_crash(self, capsys):
        logger = SecurityAuditLogger(audit_file="/nonexistent/dir/audit.jsonl")
        logger.record_event("test_event")
        captured = capsys.readouterr()
        assert "error" in captured.err.lower() or "write" in captured.err.lower()

    def test_aud007_env_var_path(self, tmp_path, monkeypatch):
        path = tmp_path / "env_audit.jsonl"
        monkeypatch.setenv("SECURITY_AUDIT_FILE", str(path))
        logger = SecurityAuditLogger()
        logger.record_event("from_env")
        assert path.exists()
        data = json.loads(path.read_text().strip())
        assert data["security_event"] == "from_env"

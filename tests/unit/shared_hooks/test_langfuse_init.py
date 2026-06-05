"""Unit tests for Langfuse _ensure_client warning (FIX-3)."""

import logging
import os
from unittest.mock import patch

import pytest


class TestEnsureClientWarning:
    def test_missing_keys_logs_warning(self, caplog):
        import shared_hooks.langfuse_trace as mod

        old_client = mod._client
        old_failed = mod._init_failed
        try:
            mod._client = None
            mod._init_failed = False

            env = {
                "TRACE_TO_LANGFUSE": "true",
                "XIAOPAW_LANGFUSE_PUBLIC_KEY": "",
                "XIAOPAW_LANGFUSE_SECRET_KEY": "",
                "XIAOPAW_LANGFUSE_BASE_URL": "",
                "LANGFUSE_PUBLIC_KEY": "",
                "LANGFUSE_SECRET_KEY": "",
                "LANGFUSE_BASE_URL": "",
            }
            with patch.dict(os.environ, env, clear=False):
                with caplog.at_level(logging.WARNING, logger="shared_hooks.langfuse_trace"):
                    result = mod._ensure_client()

            assert result is None
            assert mod._init_failed is True
            assert any("missing env vars" in r.message for r in caplog.records)
        finally:
            mod._client = old_client
            mod._init_failed = old_failed

    def test_partial_keys_logs_warning(self, caplog):
        import shared_hooks.langfuse_trace as mod

        old_client = mod._client
        old_failed = mod._init_failed
        try:
            mod._client = None
            mod._init_failed = False

            env = {
                "XIAOPAW_LANGFUSE_PUBLIC_KEY": "pk-test",
                "XIAOPAW_LANGFUSE_SECRET_KEY": "",
                "XIAOPAW_LANGFUSE_BASE_URL": "",
                "LANGFUSE_PUBLIC_KEY": "",
                "LANGFUSE_SECRET_KEY": "",
                "LANGFUSE_BASE_URL": "",
            }
            with patch.dict(os.environ, env, clear=False):
                with caplog.at_level(logging.WARNING, logger="shared_hooks.langfuse_trace"):
                    result = mod._ensure_client()

            assert result is None
            assert mod._init_failed is True
        finally:
            mod._client = old_client
            mod._init_failed = old_failed

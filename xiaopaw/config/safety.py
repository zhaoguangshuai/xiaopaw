"""Startup safety assertions for production deployment."""

from __future__ import annotations

import logging

from xiaopaw.config.validator import AppConfig

logger = logging.getLogger(__name__)

_MIN_CREDENTIAL_LENGTH = 16


class SafetyViolation(Exception):
    pass


def _is_weak_credential(value: str) -> bool:
    if len(value) < _MIN_CREDENTIAL_LENGTH:
        return True
    if value == value[0] * len(value):
        return True
    weak = {"password", "secret", "placeholder", "changeme", "12345678"}
    return value.lower() in weak


def assert_all_production_safe(cfg: AppConfig, *, is_dev: bool = False) -> None:
    violations: list[str] = []

    if not is_dev:
        if _is_weak_credential(cfg.feishu.app_secret):
            violations.append("feishu.app_secret is weak or placeholder")

        if cfg.debug.enable_test_api:
            violations.append("debug.enable_test_api must be false in production")

        if cfg.debug.test_api_host != "127.0.0.1":
            violations.append("debug.test_api_host must be 127.0.0.1")

    if violations:
        for v in violations:
            logger.error("SAFETY VIOLATION: %s", v)
        raise SafetyViolation(
            f"{len(violations)} safety violation(s): {'; '.join(violations)}"
        )

    logger.info("all production safety checks passed")

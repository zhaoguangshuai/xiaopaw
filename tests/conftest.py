"""Global test fixtures for XiaoPaw v2.1 Hook framework tests."""

import pytest

from xiaopaw.hook_framework.registry import EventType, HookContext, HookRegistry


@pytest.fixture
def hook_registry():
    return HookRegistry()


@pytest.fixture(scope="session")
def hook_context_factory():
    def _make(event_type: EventType, **kwargs):
        return HookContext(event_type=event_type, **kwargs)
    return _make

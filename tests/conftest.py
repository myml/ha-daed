"""Shared fixtures for the daed integration tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load this custom integration in every test."""
    # Importing the integration up front also pins ``custom_components`` to this
    # repository. The test harness ships its own ``testing_config/custom_components``
    # package and mounts it on ``sys.path``; if that package wins the import, Home
    # Assistant cannot find this integration ("Integration 'daed' not found").
    # Resolving it here makes discovery deterministic instead of order dependent.
    from custom_components import daed  # noqa: F401

    yield

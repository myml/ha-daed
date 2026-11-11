"""Constants for the daed integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "daed"

CONF_BASE_URL: Final = "base_url"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_SCAN_INTERVAL_SECONDS: Final = "scan_interval_seconds"
CONF_LATENCY_INTERVAL_SECONDS: Final = "latency_interval_seconds"

# daed's runtime traffic is a live counter, so the default poll is short.
DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=30)
MIN_SCAN_INTERVAL_SECONDS: Final = 10
MAX_SCAN_INTERVAL_SECONDS: Final = 60 * 60

# Latency polls are separate and much slower: daed dials every node itself.
DEFAULT_LATENCY_INTERVAL: Final = timedelta(minutes=10)
MIN_LATENCY_INTERVAL_SECONDS: Final = 60
MAX_LATENCY_INTERVAL_SECONDS: Final = 24 * 60 * 60

# Server services.
SERVICE_RUN: Final = "run"
SERVICE_TEST_NODE_LATENCY: Final = "test_node_latency"
SERVICE_UPDATE_SUBSCRIPTIONS: Final = "update_subscriptions"
SERVICE_SELECT_CONFIG: Final = "select_config"

ATTR_ENTRY_ID: Final = "entry_id"
ATTR_CONFIG_ID: Final = "config_id"
ATTR_SUBSCRIPTION_IDS: Final = "subscription_ids"

# daed's group load balancing policies (`Policy` enum upstream).
GROUP_POLICIES: Final = ("random", "fixed", "min_avg10", "min_moving_avg", "min")

# daed status strings that mean "the subscription is healthy".
HEALTHY_STATUSES: Final = ("", "ok", "success")

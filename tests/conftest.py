import pytest

from mcp_clickhouse import mcp_server


@pytest.fixture(autouse=True)
def clear_health_result_cache():
    """Keep the short-lived health result cache from leaking between tests."""
    mcp_server._clear_health_result_cache()
    yield
    mcp_server._clear_health_result_cache()


@pytest.fixture
def clean_env(monkeypatch):
    """Give a test an isolated view of the ClickHouse and SSH environment.

    Clears every variable the SSH tunnel reads or writes so a developer's real
    .env cannot leak into assertions, and lets monkeypatch restore os.environ
    when the test ends.
    """
    for name in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_SECURE",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_SERVER_HOST_NAME",
        "CLICKHOUSE_ENABLED",
        "CLICKHOUSE_SSH_HOST",
        "CLICKHOUSE_SSH_USER",
        "CLICKHOUSE_SSH_PORT",
        "CLICKHOUSE_SSH_KEY",
        "CLICKHOUSE_SSH_READY_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test-password")
    return monkeypatch

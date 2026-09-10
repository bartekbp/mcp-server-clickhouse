import os
import pathlib
from unittest.mock import Mock, patch

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.startup import (
    apply_tunnel_to_environment,
    setup_ssh_tunnel_if_configured,
    shutdown_ssh_tunnel,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ssh"


def a_config(**overrides) -> SshTunnelConfig:
    base = {
        "ssh_host": "bastion.example.com",
        "remote_host": "ch.internal",
        "remote_port": 8443,
    }
    base.update(overrides)
    return SshTunnelConfig(**base)


def test_setup_is_a_noop_when_not_configured(clean_env):
    before = dict(os.environ)

    assert setup_ssh_tunnel_if_configured() is None

    assert dict(os.environ) == before


def test_apply_rewrites_host_and_port(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_HOST"] == "127.0.0.1"
    assert os.environ["CLICKHOUSE_PORT"] == "45678"


def test_apply_sets_server_host_name_when_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "ch.internal"


def test_apply_leaves_server_host_name_alone_when_not_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    apply_tunnel_to_environment(a_config(), 45678)

    assert "CLICKHOUSE_SERVER_HOST_NAME" not in os.environ


def test_apply_does_not_clobber_an_explicit_server_host_name(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")
    clean_env.setenv("CLICKHOUSE_SERVER_HOST_NAME", "chosen-by-user")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "chosen-by-user"


def test_apply_overwrites_a_whitespace_only_server_host_name(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")
    clean_env.setenv("CLICKHOUSE_SERVER_HOST_NAME", "   ")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "ch.internal"


def test_setup_starts_a_tunnel_and_rewrites_the_environment(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion.example.com")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_PORT", "8123")

    tunnel = setup_ssh_tunnel_if_configured(spawn_path=str(FIXTURES / "fake-ssh-ok.sh"))
    try:
        assert tunnel is not None
        assert os.environ["CLICKHOUSE_HOST"] == "127.0.0.1"
        assert os.environ["CLICKHOUSE_PORT"] == str(tunnel.local_port)
    finally:
        tunnel.close()


def test_config_read_happens_before_the_rewrite(clean_env):
    """The remote target must be the pre-rewrite ClickHouse address."""
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion.example.com")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")

    tunnel = setup_ssh_tunnel_if_configured(spawn_path=str(FIXTURES / "fake-ssh-ok.sh"))
    try:
        # 8443 is the secure default; had the rewrite happened first, this would
        # be the loopback port instead of the real host name.
        assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "ch.internal"
    finally:
        tunnel.close()


class TestShutdownOrdering:
    """Tests that shutdown_ssh_tunnel drains ClickHouse clients before closing
    the forward, and that a failure in either step does not block the other or
    propagate out of shutdown_ssh_tunnel.

    Mirrors tests/test_client_cache.py::TestShutdownOrdering. mcp_server._shutdown
    is always mocked: calling the real one would shut down upstream's global
    executors and poison the rest of the test session.
    """

    @patch("mcp_clickhouse.mcp_server._shutdown")
    def test_drain_runs_before_close(self, mock_shutdown):
        call_order = []
        mock_shutdown.side_effect = lambda: call_order.append("drain")
        tunnel = Mock()
        tunnel.close.side_effect = lambda: call_order.append("close")

        shutdown_ssh_tunnel(tunnel)

        assert call_order == ["drain", "close"]
        mock_shutdown.assert_called_once_with()
        tunnel.close.assert_called_once_with()

    @patch("mcp_clickhouse.mcp_server._shutdown")
    def test_drain_failure_does_not_block_close(self, mock_shutdown):
        mock_shutdown.side_effect = RuntimeError("boom")
        tunnel = Mock()

        shutdown_ssh_tunnel(tunnel)  # must not raise

        tunnel.close.assert_called_once_with()

    @patch("mcp_clickhouse.mcp_server._shutdown")
    def test_close_failure_is_contained(self, mock_shutdown):
        tunnel = Mock()
        tunnel.close.side_effect = RuntimeError("boom")

        shutdown_ssh_tunnel(tunnel)  # must not raise

        mock_shutdown.assert_called_once_with()

import os
import pathlib

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.startup import (
    apply_tunnel_to_environment,
    setup_ssh_tunnel_if_configured,
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
    assert setup_ssh_tunnel_if_configured() is None


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

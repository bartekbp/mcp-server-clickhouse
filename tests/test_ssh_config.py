import pytest

from mcp_clickhouse.ssh.config import DEFAULT_READY_TIMEOUT, get_ssh_tunnel_config


def test_returns_none_when_ssh_host_unset(clean_env):
    assert get_ssh_tunnel_config() is None


def test_returns_none_when_ssh_host_blank(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "   ")
    assert get_ssh_tunnel_config() is None


def test_reads_remote_target_from_clickhouse_settings(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion.example.com")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_PORT", "8123")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    cfg = get_ssh_tunnel_config()

    assert cfg is not None
    assert cfg.ssh_host == "bastion.example.com"
    assert cfg.remote_host == "ch.internal"
    assert cfg.remote_port == 8123
    assert cfg.ssh_user is None
    assert cfg.ssh_port is None
    assert cfg.ssh_key is None
    assert cfg.ready_timeout == DEFAULT_READY_TIMEOUT


def test_remote_port_defaults_to_8443_when_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")

    cfg = get_ssh_tunnel_config()

    assert cfg.remote_port == 8443


def test_remote_port_defaults_to_8123_when_not_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    cfg = get_ssh_tunnel_config()

    assert cfg.remote_port == 8123


def test_reads_optional_ssh_settings(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_SSH_USER", "ec2-user")
    clean_env.setenv("CLICKHOUSE_SSH_PORT", "2222")
    clean_env.setenv("CLICKHOUSE_SSH_KEY", "~/.ssh/bastion.pem")
    clean_env.setenv("CLICKHOUSE_SSH_READY_TIMEOUT", "25")

    cfg = get_ssh_tunnel_config()

    assert cfg.ssh_user == "ec2-user"
    assert cfg.ssh_port == 2222
    assert cfg.ssh_key == "~/.ssh/bastion.pem"
    assert cfg.ready_timeout == 25.0


@pytest.mark.parametrize("bad", ["0", "65536", "-1", "not-a-port", "22.5"])
def test_rejects_bad_ssh_port(clean_env, bad):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_SSH_PORT", bad)

    with pytest.raises(ValueError) as exc:
        get_ssh_tunnel_config()
    assert bad in str(exc.value)


@pytest.mark.parametrize("bad", ["0", "-3", "nope"])
def test_rejects_bad_ready_timeout(clean_env, bad):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_SSH_READY_TIMEOUT", bad)

    with pytest.raises(ValueError) as exc:
        get_ssh_tunnel_config()
    assert bad in str(exc.value)


def test_missing_clickhouse_host_is_a_clear_error(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_ENABLED", "false")

    with pytest.raises(ValueError) as exc:
        get_ssh_tunnel_config()
    assert "CLICKHOUSE_HOST" in str(exc.value)


def test_warns_when_remote_port_is_native_protocol(clean_env, caplog):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_PORT", "9000")

    with caplog.at_level("WARNING"):
        cfg = get_ssh_tunnel_config()

    assert cfg.remote_port == 9000
    assert "9000" in caplog.text
    assert "HTTP" in caplog.text

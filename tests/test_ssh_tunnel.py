import pathlib
import signal
import socket
import time

import pytest

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.tunnel import (
    TunnelError,
    build_ssh_argv,
    pick_free_port,
    wait_for_tcp_ready,
)


def minimal_config(**overrides) -> SshTunnelConfig:
    base = {
        "ssh_host": "bastion.example.com",
        "remote_host": "ch.internal",
        "remote_port": 8443,
    }
    base.update(overrides)
    return SshTunnelConfig(**base)


def test_pick_free_port_returns_a_bindable_port():
    port = pick_free_port()

    assert 1024 < port <= 65535
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_pick_free_port_varies():
    ports = {pick_free_port() for _ in range(5)}
    assert len(ports) > 1


def test_build_ssh_argv_minimal():
    argv = build_ssh_argv(minimal_config(), 34567)

    assert argv == [
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-L",
        "34567:ch.internal:8443",
        "bastion.example.com",
    ]


def test_build_ssh_argv_includes_optional_flags():
    cfg = minimal_config(ssh_user="ec2-user", ssh_port=2222, ssh_key="/tmp/key.pem")

    argv = build_ssh_argv(cfg, 1234)

    assert argv[-9:] == [
        "-L",
        "1234:ch.internal:8443",
        "-l",
        "ec2-user",
        "-p",
        "2222",
        "-i",
        "/tmp/key.pem",
        "bastion.example.com",
    ]


def test_build_ssh_argv_expands_home_in_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    argv = build_ssh_argv(minimal_config(ssh_key="~/.ssh/id_ed25519"), 1234)

    assert argv[argv.index("-i") + 1] == str(tmp_path / ".ssh/id_ed25519")


def test_wait_for_tcp_ready_returns_once_port_accepts():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        wait_for_tcp_ready(port, timeout=2.0)


def test_wait_for_tcp_ready_times_out_on_dead_port():
    port = pick_free_port()

    started = time.monotonic()
    with pytest.raises(TunnelError) as exc:
        wait_for_tcp_ready(port, timeout=0.3)
    elapsed = time.monotonic() - started

    assert str(port) in str(exc.value)
    assert 0.25 <= elapsed < 3.0


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ssh"


def fake_ssh(name: str) -> str:
    path = FIXTURES / name
    assert path.exists(), f"missing fixture {path}"
    return str(path)


def test_start_tunnel_returns_once_the_forward_accepts():
    from mcp_clickhouse.ssh.tunnel import start_tunnel, try_connect

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ok.sh"))
    try:
        assert tunnel.local_port > 0
        assert try_connect(tunnel.local_port)
    finally:
        tunnel.close()


def test_start_tunnel_passes_the_remote_target_through_to_ssh(tmp_path):
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    recorder = tmp_path / "argv.txt"
    script = tmp_path / "record-ssh.sh"
    # Absolute path to the fixture: the wrapper lives in tmp_path, so a relative
    # path would not resolve.
    script.write_text(
        "#!/usr/bin/env sh\n"
        f'printf "%s\\n" "$@" > {recorder}\n'
        f'exec {fake_ssh("fake-ssh-ok.sh")} "$@"\n'
    )
    script.chmod(0o755)

    cfg = minimal_config(remote_host="ch.internal", remote_port=8443)
    tunnel = start_tunnel(cfg, spawn_path=str(script))
    try:
        recorded = recorder.read_text().splitlines()
        assert f"{tunnel.local_port}:ch.internal:8443" in recorded
        assert "bastion.example.com" in recorded
    finally:
        tunnel.close()


def test_start_tunnel_reports_early_exit_with_stderr_tail():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    with pytest.raises(TunnelError) as exc:
        start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-crash.sh"))

    message = str(exc.value)
    assert "exit 255" in message
    assert "Permission denied (publickey)." in message


def test_start_tunnel_times_out_when_forward_never_comes_up():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    cfg = minimal_config(ready_timeout=0.4)

    started = time.monotonic()
    with pytest.raises(TunnelError) as exc:
        start_tunnel(cfg, spawn_path=fake_ssh("fake-ssh-hang.sh"))
    elapsed = time.monotonic() - started

    assert "readiness timeout" in str(exc.value)
    assert 0.3 <= elapsed < 5.0


def test_start_tunnel_rejects_missing_key_without_spawning(tmp_path, monkeypatch):
    from mcp_clickhouse.ssh import tunnel as tunnel_mod
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    def explode(*args, **kwargs):
        raise AssertionError("Popen must not be called when the key is missing")

    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", explode)

    missing = tmp_path / "nope.pem"
    cfg = minimal_config(ssh_key=str(missing))

    with pytest.raises(TunnelError) as exc:
        start_tunnel(cfg, spawn_path=fake_ssh("fake-ssh-ok.sh"))

    assert str(missing) in str(exc.value)


def test_start_tunnel_accepts_an_existing_key(tmp_path):
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    key = tmp_path / "id.pem"
    key.write_text("not a real key")
    cfg = minimal_config(ssh_key=str(key))

    tunnel = start_tunnel(cfg, spawn_path=fake_ssh("fake-ssh-ok.sh"))
    tunnel.close()


def test_start_tunnel_reports_missing_ssh_binary():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    with pytest.raises(TunnelError) as exc:
        start_tunnel(minimal_config(), spawn_path="/nonexistent/ssh-binary")

    assert "not found on PATH" in str(exc.value)
    assert "OpenSSH" in str(exc.value)


def test_close_stops_the_forward():
    from mcp_clickhouse.ssh.tunnel import start_tunnel, try_connect

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ok.sh"))
    port = tunnel.local_port
    assert try_connect(port)

    tunnel.close()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not try_connect(port):
            break
        time.sleep(0.05)
    assert not try_connect(port)


def test_close_is_idempotent():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ok.sh"))
    tunnel.close()
    tunnel.close()


def test_close_falls_back_to_sigkill():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ignore-sigterm.sh"))

    started = time.monotonic()
    tunnel.close(grace=0.5)
    elapsed = time.monotonic() - started

    assert tunnel._proc.poll() == -signal.SIGKILL
    assert 0.4 <= elapsed < 5.0


def test_close_does_not_fire_the_runtime_exit_callback():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    fired = []
    tunnel = start_tunnel(
        minimal_config(),
        spawn_path=fake_ssh("fake-ssh-ok.sh"),
        on_runtime_exit=lambda code, tail: fired.append((code, tail)),
    )

    tunnel.close()
    tunnel._watcher.join(timeout=2.0)

    assert fired == []


def test_runtime_exit_callback_fires_when_the_tunnel_dies():
    import threading

    from mcp_clickhouse.ssh.tunnel import start_tunnel

    fired = threading.Event()
    captured = {}

    def on_exit(code, tail):
        captured["code"] = code
        fired.set()

    tunnel = start_tunnel(
        minimal_config(),
        spawn_path=fake_ssh("fake-ssh-ok.sh"),
        on_runtime_exit=on_exit,
    )
    tunnel._proc.kill()

    assert fired.wait(timeout=5.0), "on_runtime_exit never fired"
    assert captured["code"] is not None

import logging
import os
import pathlib
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.tunnel import (
    TunnelError,
    build_ssh_argv,
    pick_free_port,
)


@pytest.fixture(autouse=True)
def clear_shutdown_flag():
    """shutdown_requested is process-wide state; never let it leak between tests."""
    from mcp_clickhouse.ssh import tunnel as tunnel_mod

    tunnel_mod.shutdown_requested.clear()
    yield
    tunnel_mod.shutdown_requested.clear()


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
        "127.0.0.1:34567:ch.internal:8443",
        "--",
        "bastion.example.com",
    ]


def test_build_ssh_argv_binds_the_forward_to_loopback_only():
    """An explicit bind address stops ssh inheriting GatewayPorts."""
    argv = build_ssh_argv(minimal_config(), 34567)

    assert argv[argv.index("-L") + 1].startswith("127.0.0.1:")


def test_build_ssh_argv_brackets_an_ipv6_remote_host():
    argv = build_ssh_argv(minimal_config(remote_host="2001:db8::1"), 34567)

    assert argv[argv.index("-L") + 1] == "127.0.0.1:34567:[2001:db8::1]:8443"


def test_build_ssh_argv_does_not_double_bracket_an_ipv6_remote_host():
    argv = build_ssh_argv(minimal_config(remote_host="[2001:db8::1]"), 34567)

    assert argv[argv.index("-L") + 1] == "127.0.0.1:34567:[2001:db8::1]:8443"


def test_build_ssh_argv_includes_optional_flags():
    cfg = minimal_config(ssh_user="ec2-user", ssh_port=2222, ssh_key="/tmp/key.pem")

    argv = build_ssh_argv(cfg, 1234)

    assert argv[-10:] == [
        "-L",
        "127.0.0.1:1234:ch.internal:8443",
        "-l",
        "ec2-user",
        "-p",
        "2222",
        "-i",
        "/tmp/key.pem",
        "--",
        "bastion.example.com",
    ]


def test_build_ssh_argv_terminates_options_before_a_dash_leading_host():
    argv = build_ssh_argv(minimal_config(ssh_host="-oProxyCommand=touch /tmp/pwned"), 1234)

    assert argv[-2:] == ["--", "-oProxyCommand=touch /tmp/pwned"]


def test_build_ssh_argv_expands_home_in_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    argv = build_ssh_argv(minimal_config(ssh_key="~/.ssh/id_ed25519"), 1234)

    assert argv[argv.index("-i") + 1] == str(tmp_path / ".ssh/id_ed25519")


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
        assert f"127.0.0.1:{tunnel.local_port}:ch.internal:8443" in recorded
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


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signalled_tunnel_exit_is_logged_rather_than_swallowed(caplog, sig):
    """Suppressing the crash path must never be silent.

    A shared Ctrl-C reaches the ssh child too, so this cannot become a hard
    exit. But `pkill ssh` looks identical from here and really does kill the
    forward, so it has to be visible at WARNING, which the project's default
    INFO logging shows.
    """
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    fired = threading.Event()
    tunnel = start_tunnel(
        minimal_config(),
        spawn_path=fake_ssh("fake-ssh-ok.sh"),
        on_runtime_exit=lambda code, tail: fired.set(),
    )
    try:
        with caplog.at_level(logging.WARNING, logger="mcp-clickhouse.ssh"):
            tunnel._proc.send_signal(sig)
            tunnel._watcher.join(timeout=5.0)
            assert not tunnel._watcher.is_alive()
    finally:
        tunnel.close()

    assert not fired.is_set(), "a co-shutdown signal must not trigger the crash exit"
    assert "not treating it as a crash" in caplog.text
    assert "the forward is now dead" in caplog.text


def test_shutdown_flag_suppresses_the_crash_path_for_any_exit_code(caplog):
    from mcp_clickhouse.ssh import tunnel as tunnel_mod
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    fired = threading.Event()
    tunnel = start_tunnel(
        minimal_config(),
        spawn_path=fake_ssh("fake-ssh-ok.sh"),
        on_runtime_exit=lambda code, tail: fired.set(),
    )
    tunnel_mod.shutdown_requested.set()
    try:
        with caplog.at_level(logging.WARNING, logger="mcp-clickhouse.ssh"):
            # SIGKILL is not one of the co-shutdown signals; only the flag saves it.
            tunnel._proc.kill()
            tunnel._watcher.join(timeout=5.0)
            assert not tunnel._watcher.is_alive()
    finally:
        tunnel_mod.shutdown_requested.clear()
        tunnel.close()

    assert not fired.is_set()
    assert "not treating it as a crash" in caplog.text


CTRL_C_CHILD = """
import sys, time

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.tunnel import start_tunnel

marker, ready, spawn = sys.argv[1], sys.argv[2], sys.argv[3]

cfg = SshTunnelConfig(
    ssh_host="bastion.example.com", remote_host="ch.internal", remote_port=8443
)
tunnel = start_tunnel(cfg, spawn_path=spawn)
try:
    with open(ready, "w") as fh:
        fh.write("ready")
    while True:
        time.sleep(0.05)
finally:
    # Real teardown takes real time: mcp.run() unwinds through the transport and
    # shutdown_ssh_tunnel() drains every cached ClickHouse client first. Sleeping
    # reproduces that window. A watcher thread that mistakes a shared SIGINT for a
    # tunnel crash calls os._exit(1) inside it and this marker is never written.
    time.sleep(1.0)
    with open(marker, "w") as fh:
        fh.write("teardown ran")
    tunnel.close()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and SIGINT")
def test_ctrl_c_lets_teardown_run_instead_of_exiting_from_the_watcher(tmp_path):
    """Ctrl-C signals the whole foreground process group, ssh child included.

    The watcher thread sees ssh die first. If it treated that as a crash it would
    call os._exit(1) and skip the caller's teardown entirely, which is what this
    test guards against: the marker file proves the `finally` ran.
    """
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    marker = tmp_path / "teardown.txt"
    ready = tmp_path / "ready.txt"

    env = dict(os.environ)
    # _default_runtime_exit no-ops when it sees this, which would mask the bug.
    env.pop("PYTEST_CURRENT_TEST", None)
    env["CLICKHOUSE_ENABLED"] = "false"
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(CTRL_C_CHILD),
            str(marker),
            str(ready),
            fake_ssh("fake-ssh-ok.sh"),
        ],
        cwd=str(repo_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Give the child its own process group so killpg mimics a terminal's
        # Ctrl-C without signalling pytest itself.
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60.0
        while not ready.exists():
            assert proc.poll() is None, (
                f"child died before the tunnel came up: {proc.communicate()}"
            )
            assert time.monotonic() < deadline, "child never reported a ready tunnel"
            time.sleep(0.05)

        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=60.0)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
            proc.wait(timeout=30.0)

    detail = stderr.decode("utf-8", "replace")
    assert marker.exists(), f"teardown never ran after SIGINT. stderr: {detail}"
    assert marker.read_text() == "teardown ran"
    # os._exit(1) from the watcher thread is the failure being guarded against.
    assert proc.returncode != 1, f"process exited 1, as os._exit would. stderr: {detail}"
    assert "exited unexpectedly" not in detail, f"logged a false crash: {detail}"

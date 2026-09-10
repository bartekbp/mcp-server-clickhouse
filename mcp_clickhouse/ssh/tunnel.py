"""Spawn and supervise an SSH local port forward.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts). The system `ssh`
binary is used rather than a Python SSH library so that ~/.ssh/config — Host
aliases, ProxyJump chains, IdentityFile — ssh-agent, and hardware-backed keys
all work without reimplementation.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

from .config import SshTunnelConfig

logger = logging.getLogger("mcp-clickhouse.ssh")

READINESS_POLL_INTERVAL = 0.05
SHUTDOWN_GRACE = 3.0
STDERR_TAIL_BYTES = 500
_CONNECT_PROBE_TIMEOUT = 0.5


class TunnelError(RuntimeError):
    """Raised when the SSH tunnel cannot be established or supervised."""


def pick_free_port() -> int:
    """Ask the kernel for an unused loopback port.

    Inherently racy: another process can claim the port between this call and
    ssh binding it. `ExitOnForwardFailure=yes` turns a lost race into a clean
    startup error rather than a silently unforwarded port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_ssh_argv(cfg: SshTunnelConfig, local_port: int) -> list[str]:
    """Build the ssh arguments (without the binary name) for the forward."""
    argv = [
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        # BatchMode makes a key that needs an interactive passphrase fail fast
        # instead of blocking on a prompt with nowhere to render under stdio.
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-L",
        f"{local_port}:{cfg.remote_host}:{cfg.remote_port}",
    ]
    if cfg.ssh_user:
        argv += ["-l", cfg.ssh_user]
    if cfg.ssh_port is not None:
        argv += ["-p", str(cfg.ssh_port)]
    if cfg.ssh_key:
        argv += ["-i", os.path.expanduser(cfg.ssh_key)]
    argv.append(cfg.ssh_host)
    return argv


def try_connect(port: int) -> bool:
    """Return whether 127.0.0.1:port accepts a TCP connection right now."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_CONNECT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def wait_for_tcp_ready(
    port: int,
    timeout: float,
    interval: float = READINESS_POLL_INTERVAL,
) -> None:
    """Block until 127.0.0.1:port accepts a connection, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if try_connect(port):
            return
        if time.monotonic() >= deadline:
            raise TunnelError(
                f"TCP readiness timeout: 127.0.0.1:{port} did not accept within {timeout}s"
            )
        time.sleep(interval)


class _StderrTail(threading.Thread):
    """Drain ssh's stderr, log each line, and keep a rolling tail for errors."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._buf = ""
        self._lock = threading.Lock()

    def run(self) -> None:
        if self._stream is None:
            return
        try:
            for raw in iter(self._stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                logger.info("[ssh] %s", line)
                with self._lock:
                    self._buf = (self._buf + line + "\n")[-STDERR_TAIL_BYTES:]
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def text(self) -> str:
        with self._lock:
            return self._buf.strip()


class ActiveTunnel:
    """A running ssh forward. Close it to tear the forward down."""

    def __init__(
        self,
        local_port: int,
        proc: "subprocess.Popen[bytes]",
        tail: _StderrTail,
        on_runtime_exit: Optional[Callable[[Optional[int], str], None]] = None,
    ) -> None:
        self.local_port = local_port
        self._proc = proc
        self._tail = tail
        self._on_runtime_exit = on_runtime_exit or _default_runtime_exit
        self._closing = False
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        code = self._proc.wait()
        # A negative code means the child was signalled. SIGINT/SIGHUP reach it only
        # via the terminal's process group, i.e. we were signalled too — that is a
        # shutdown in progress, not a tunnel crash. Treating it as a crash here would
        # call os._exit() from this thread and skip main()'s teardown entirely.
        if self._closing or code in (-signal.SIGINT, -signal.SIGHUP, -signal.SIGTERM):
            return
        self._tail.join(timeout=1.0)
        tail = self._tail.text()
        logger.error("[ssh] tunnel exited unexpectedly (code %s). stderr tail: %s", code, tail)
        self._on_runtime_exit(code, tail)

    def close(self, grace: float = SHUTDOWN_GRACE) -> None:
        """Stop the forward. Safe to call more than once."""
        if self._closing:
            return
        self._closing = True
        if self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            logger.warning("[ssh] tunnel ignored SIGTERM; sending SIGKILL")
        self._proc.kill()
        try:
            self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            logger.error("[ssh] tunnel survived SIGKILL")


def _default_runtime_exit(code: Optional[int], stderr_tail: str) -> None:
    """Exit the process when the tunnel dies mid-session.

    A dead forward leaves every later query failing against a closed local port,
    with cached clients holding connections that will never recover. Exiting
    surfaces the problem immediately and lets the MCP client restart the server.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        logger.error("[test mode] would have exited(1) after tunnel death")
        return
    os._exit(1)


def start_tunnel(
    cfg: SshTunnelConfig,
    *,
    spawn_path: str = "ssh",
    on_runtime_exit: Optional[Callable[[Optional[int], str], None]] = None,
) -> ActiveTunnel:
    """Open the forward and block until it accepts TCP, or raise TunnelError."""
    if cfg.ssh_key:
        expanded = os.path.expanduser(cfg.ssh_key)
        if not os.path.exists(expanded):
            raise TunnelError(f"SSH key not found: {cfg.ssh_key}")

    local_port = pick_free_port()
    argv = [spawn_path] + build_ssh_argv(cfg, local_port)

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TunnelError(
            f"SSH tunnel: '{spawn_path}' not found on PATH. Install an OpenSSH client."
        ) from exc

    tail = _StderrTail(proc.stderr)
    tail.start()

    deadline = time.monotonic() + cfg.ready_timeout
    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            tail.join(timeout=1.0)
            raise TunnelError(f"SSH tunnel failed (exit {exit_code}): {tail.text()}")
        if try_connect(local_port):
            break
        if time.monotonic() >= deadline:
            _kill_quietly(proc)
            tail.join(timeout=1.0)
            raise TunnelError(
                f"SSH tunnel readiness timeout: 127.0.0.1:{local_port} did not accept "
                f"within {cfg.ready_timeout}s. ssh stderr: {tail.text()}"
            )
        time.sleep(READINESS_POLL_INTERVAL)

    return ActiveTunnel(local_port, proc, tail, on_runtime_exit)


def _kill_quietly(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=SHUTDOWN_GRACE)
    except subprocess.TimeoutExpired:
        logger.error("[ssh] failed to kill tunnel process %s", proc.pid)

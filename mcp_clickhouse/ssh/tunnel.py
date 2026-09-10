"""Spawn and supervise an SSH local port forward.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts). The system `ssh`
binary is used rather than a Python SSH library so that ~/.ssh/config — Host
aliases, ProxyJump chains, IdentityFile — ssh-agent, and hardware-backed keys
all work without reimplementation.
"""

from __future__ import annotations

import logging
import os
import socket
import time

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

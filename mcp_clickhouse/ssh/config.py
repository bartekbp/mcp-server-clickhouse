"""Environment parsing for the SSH bastion tunnel."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from ..mcp_env import ClickHouseConfig

logger = logging.getLogger("mcp-clickhouse.ssh")

DEFAULT_READY_TIMEOUT = 10.0

# clickhouse-client's native TCP ports. mcp-clickhouse speaks the HTTP interface,
# so forwarding one of these will connect but never answer.
NATIVE_PROTOCOL_PORTS = frozenset({9000, 9440})


@dataclass(frozen=True)
class SshTunnelConfig:
    """A single SSH local port forward to a ClickHouse endpoint.

    The invariants live here rather than only in get_ssh_tunnel_config() so that
    a directly constructed config cannot carry a value that would produce a
    malformed ssh command line. The factory still validates first, so a bad
    environment variable is reported with its own name.
    """

    ssh_host: str
    remote_host: str
    remote_port: int
    ssh_user: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_key: Optional[str] = None
    ready_timeout: float = DEFAULT_READY_TIMEOUT

    def __post_init__(self) -> None:
        if not self.ssh_host or not self.ssh_host.strip():
            raise ValueError(f"ssh_host must be a non-blank host, got {self.ssh_host!r}")
        if not 1 <= self.remote_port <= 65535:
            raise ValueError(f"remote_port must be in 1..65535, got {self.remote_port!r}")
        if self.ssh_port is not None and not 1 <= self.ssh_port <= 65535:
            raise ValueError(f"ssh_port must be None or in 1..65535, got {self.ssh_port!r}")
        if self.ready_timeout <= 0:
            raise ValueError(f"ready_timeout must be greater than 0, got {self.ready_timeout!r}")


def get_ssh_tunnel_config() -> Optional[SshTunnelConfig]:
    """Build the tunnel config from the environment, or None when not configured.

    Must be called before the environment is rewritten by
    :func:`mcp_clickhouse.ssh.startup.apply_tunnel_to_environment`, because the
    remote target is read from CLICKHOUSE_HOST and CLICKHOUSE_PORT.
    """
    ssh_host = (os.getenv("CLICKHOUSE_SSH_HOST") or "").strip()
    if not ssh_host:
        return None

    clickhouse = ClickHouseConfig()
    try:
        remote_host = clickhouse.host
    except KeyError as exc:
        raise ValueError(
            "CLICKHOUSE_SSH_HOST is set but CLICKHOUSE_HOST is not. "
            "CLICKHOUSE_HOST must name the ClickHouse server as seen from the bastion."
        ) from exc
    remote_port = clickhouse.port

    if remote_port in NATIVE_PROTOCOL_PORTS:
        logger.warning(
            "Tunnelling to port %s, which is a ClickHouse native TCP port. "
            "mcp-clickhouse uses the HTTP interface; prefer 8123 (HTTP) or 8443 (HTTPS).",
            remote_port,
        )

    return SshTunnelConfig(
        ssh_host=ssh_host,
        remote_host=remote_host,
        remote_port=remote_port,
        ssh_user=(os.getenv("CLICKHOUSE_SSH_USER") or "").strip() or None,
        ssh_port=_parse_ssh_port(os.getenv("CLICKHOUSE_SSH_PORT")),
        ssh_key=(os.getenv("CLICKHOUSE_SSH_KEY") or "").strip() or None,
        ready_timeout=_parse_ready_timeout(os.getenv("CLICKHOUSE_SSH_READY_TIMEOUT")),
    )


def _parse_ssh_port(raw: Optional[str]) -> Optional[int]:
    if raw is None or not raw.strip():
        return None
    try:
        port = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid CLICKHOUSE_SSH_PORT: {raw}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid CLICKHOUSE_SSH_PORT: {raw}")
    return port


def _parse_ready_timeout(raw: Optional[str]) -> float:
    if raw is None or not raw.strip():
        return DEFAULT_READY_TIMEOUT
    try:
        timeout = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid CLICKHOUSE_SSH_READY_TIMEOUT: {raw}") from exc
    if timeout <= 0:
        raise ValueError(f"Invalid CLICKHOUSE_SSH_READY_TIMEOUT: {raw}")
    return timeout

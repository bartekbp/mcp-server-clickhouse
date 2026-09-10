"""Wire the SSH tunnel into the server lifecycle."""

from __future__ import annotations

import logging
import os
from typing import Optional

from .config import SshTunnelConfig, get_ssh_tunnel_config
from .tunnel import ActiveTunnel, start_tunnel

logger = logging.getLogger("mcp-clickhouse.ssh")


def setup_ssh_tunnel_if_configured(**start_tunnel_kwargs) -> Optional[ActiveTunnel]:
    """Start the tunnel and repoint ClickHouse at it, when one is configured.

    Returns None when CLICKHOUSE_SSH_HOST is unset, leaving the environment
    untouched. Must be called before the first ClickHouse client is created:
    a client built from the pre-tunnel address would be cached and would bypass
    the forward entirely.
    """
    cfg = get_ssh_tunnel_config()
    if cfg is None:
        return None

    logger.info(
        "Starting SSH tunnel via %s -> %s:%s ...",
        cfg.ssh_host,
        cfg.remote_host,
        cfg.remote_port,
    )
    tunnel = start_tunnel(cfg, **start_tunnel_kwargs)
    logger.info("SSH tunnel ready on 127.0.0.1:%s", tunnel.local_port)

    apply_tunnel_to_environment(cfg, tunnel.local_port)
    return tunnel


def apply_tunnel_to_environment(cfg: SshTunnelConfig, local_port: int) -> None:
    """Point CLICKHOUSE_HOST/PORT at the forward, preserving TLS verification.

    ClickHouseConfig reads os.environ on every property access, so rewriting it
    here is picked up by the cached get_config() singleton and by every client
    the client cache creates afterwards.
    """
    secure = os.getenv("CLICKHOUSE_SECURE", "true").lower() == "true"

    os.environ["CLICKHOUSE_HOST"] = "127.0.0.1"
    os.environ["CLICKHOUSE_PORT"] = str(local_port)

    if secure and not (os.getenv("CLICKHOUSE_SERVER_HOST_NAME") or "").strip():
        # Over the forward the TCP peer is 127.0.0.1, but the server's
        # certificate names the real host. Without this override SNI and
        # certificate verification both fail.
        os.environ["CLICKHOUSE_SERVER_HOST_NAME"] = cfg.remote_host
        logger.info(
            "Set CLICKHOUSE_SERVER_HOST_NAME=%s so TLS still verifies against the "
            "real host through the tunnel",
            cfg.remote_host,
        )


def shutdown_ssh_tunnel(tunnel: ActiveTunnel) -> None:
    """Drain ClickHouse clients, then close the forward.

    Order matters: closing the forward first would leave clients trying to speak
    to a dead local port during their own teardown.
    """
    try:
        from .. import mcp_server

        mcp_server._shutdown()
    except Exception:
        logger.exception("Error draining ClickHouse clients before closing SSH tunnel")

    try:
        tunnel.close()
    except Exception:
        logger.exception("Error closing SSH tunnel")

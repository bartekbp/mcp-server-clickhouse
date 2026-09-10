"""Wire the SSH tunnel into the server lifecycle."""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from .. import mcp_server
from .config import SshTunnelConfig, get_ssh_tunnel_config
from .tunnel import ActiveTunnel, start_tunnel

logger = logging.getLogger("mcp-clickhouse.ssh")


def setup_ssh_tunnel_if_configured(
    *,
    on_configured: Optional[Callable[[], None]] = None,
    **start_tunnel_kwargs,
) -> Optional[ActiveTunnel]:
    """Start the tunnel and repoint ClickHouse at it, when one is configured.

    Returns None when CLICKHOUSE_SSH_HOST is unset, leaving the environment
    untouched. Must be called before the first ClickHouse client is created:
    a client built from the pre-tunnel address would be cached and would bypass
    the forward entirely.

    `on_configured` runs once a tunnel is known to be wanted and before the ssh
    child is spawned, so the caller can install its termination handlers first.
    Waiting until this function returns would leave a signal arriving during ssh
    authentication or the readiness wait killing the process with the child still
    running. It is deliberately not called when no tunnel is configured, which is
    what keeps the unset-CLICKHOUSE_SSH_HOST path free of any signal handling.
    """
    cfg = get_ssh_tunnel_config()
    if cfg is None:
        return None

    if on_configured is not None:
        on_configured()

    logger.info(
        "Starting SSH tunnel via %s -> %s:%s ...",
        cfg.ssh_host,
        cfg.remote_host,
        cfg.remote_port,
    )
    tunnel = start_tunnel(cfg, **start_tunnel_kwargs)
    logger.info("SSH tunnel ready on 127.0.0.1:%s", tunnel.local_port)

    try:
        apply_tunnel_to_environment(cfg, tunnel.local_port)
    except BaseException:
        # Never return without either a live tunnel or a closed one: the caller
        # only learns about the ssh child through the value we return.
        tunnel.close()
        raise
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
        mcp_server._shutdown()
    except Exception:
        logger.exception(
            "Error draining ClickHouse clients before closing SSH tunnel. Some "
            "clients or query executors may not have been drained; they will see "
            "the forward close under them and any in-flight query fail."
        )

    try:
        if not tunnel.close():
            logger.error(
                "SSH tunnel process could not be reaped; the forward may still be "
                "open. Check for a stray ssh process and kill it by hand."
            )
    except Exception:
        logger.exception("Error closing SSH tunnel")

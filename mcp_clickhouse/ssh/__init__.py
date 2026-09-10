"""SSH local port forwarding so ClickHouse can be reached through a bastion host.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts, src/ssh/startup.ts).
"""

from __future__ import annotations

from .config import DEFAULT_READY_TIMEOUT, SshTunnelConfig, get_ssh_tunnel_config
from .startup import (
    apply_tunnel_to_environment,
    setup_ssh_tunnel_if_configured,
    shutdown_ssh_tunnel,
)
from .tunnel import (
    ActiveTunnel,
    TunnelError,
    build_ssh_argv,
    pick_free_port,
    shutdown_requested,
    start_tunnel,
    try_connect,
)

__all__ = [
    "DEFAULT_READY_TIMEOUT",
    "ActiveTunnel",
    "SshTunnelConfig",
    "TunnelError",
    "apply_tunnel_to_environment",
    "build_ssh_argv",
    "get_ssh_tunnel_config",
    "pick_free_port",
    "setup_ssh_tunnel_if_configured",
    "shutdown_requested",
    "shutdown_ssh_tunnel",
    "start_tunnel",
    "try_connect",
]

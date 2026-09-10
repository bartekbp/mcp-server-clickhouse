"""SSH local port forwarding so ClickHouse can be reached through a bastion host.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts, src/ssh/startup.ts).
"""

from __future__ import annotations

from .config import DEFAULT_READY_TIMEOUT, SshTunnelConfig, get_ssh_tunnel_config
from .tunnel import TunnelError, build_ssh_argv, pick_free_port, wait_for_tcp_ready

__all__ = [
    "DEFAULT_READY_TIMEOUT",
    "SshTunnelConfig",
    "TunnelError",
    "build_ssh_argv",
    "get_ssh_tunnel_config",
    "pick_free_port",
    "wait_for_tcp_ready",
]

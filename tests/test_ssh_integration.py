"""End-to-end test against the real ssh binary.

Opt in by pointing CLICKHOUSE_SSH_IT_HOST at an SSH host you can reach with key
auth and CLICKHOUSE_SSH_IT_TARGET at a "host:port" that host can reach. The
simplest local setup is an sshd on localhost with your own key authorised:

    CLICKHOUSE_SSH_IT_HOST=localhost \
    CLICKHOUSE_SSH_IT_TARGET=127.0.0.1:22 \
    uv run pytest tests/test_ssh_integration.py -v

The forward is expected to land on an sshd: the test asserts that connecting to
the forwarded local port yields an unprompted banner, which only an SSH server
sends immediately on connect.
"""

import os
import socket

import pytest

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.tunnel import start_tunnel, try_connect

IT_HOST = os.getenv("CLICKHOUSE_SSH_IT_HOST")
IT_TARGET = os.getenv("CLICKHOUSE_SSH_IT_TARGET")

pytestmark = pytest.mark.skipif(
    not (IT_HOST and IT_TARGET),
    reason="set CLICKHOUSE_SSH_IT_HOST and CLICKHOUSE_SSH_IT_TARGET to run",
)


def test_real_ssh_forward_carries_traffic():
    host, _, port = IT_TARGET.rpartition(":")
    cfg = SshTunnelConfig(
        ssh_host=IT_HOST,
        remote_host=host,
        remote_port=int(port),
        ssh_user=os.getenv("CLICKHOUSE_SSH_IT_USER") or None,
        ready_timeout=15.0,
    )

    tunnel = start_tunnel(cfg)
    try:
        assert try_connect(tunnel.local_port)
        # Prove bytes actually traverse the forward, not just that something
        # is listening: an sshd answers with its version banner unprompted.
        with socket.create_connection(("127.0.0.1", tunnel.local_port), timeout=5) as sock:
            sock.settimeout(5)
            banner = sock.recv(64)
        assert banner, "forward accepted but carried no data"
    finally:
        tunnel.close()

    assert not try_connect(tunnel.local_port)

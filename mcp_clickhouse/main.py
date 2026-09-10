import logging
import signal

from .mcp_server import mcp
from .mcp_env import get_mcp_config, TransportType
from .mcp_middleware_hook import setup_middleware
from .ssh import setup_ssh_tunnel_if_configured, shutdown_ssh_tunnel

logger = logging.getLogger("mcp-clickhouse")


def _install_termination_handlers() -> None:
    """Turn SIGTERM into SystemExit so the tunnel teardown in main() runs.

    Python's default SIGTERM disposition kills the process outright, skipping
    both `finally` blocks and atexit handlers, which would strand the ssh child.
    Only installed when a tunnel is actually running, so the no-tunnel path keeps
    upstream's signal behaviour exactly.

    On the HTTP/SSE transports, uvicorn's Server.capture_signals() temporarily
    overwrites this handler for the duration of serve(), then restores it and
    re-raises the captured signal via signal.raise_signal() once serve() returns.
    So this handler still fires and mcp.run()'s finally still tears down the
    tunnel, but only after uvicorn's graceful-shutdown window (fastmcp sets
    timeout_graceful_shutdown=2), i.e. up to ~2s later than on stdio.
    """

    def _handler(signum, _frame):
        raise SystemExit(128 + int(signum))

    try:
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # Not on the main thread; nothing to install.
        logger.debug("Could not install SIGTERM handler off the main thread")


def main():
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport

    # Setup any custom middleware
    setup_middleware(mcp)

    # Open the SSH forward, if configured, *before* the server can serve a tool
    # call. A client created from the pre-tunnel address would be cached and
    # would bypass the forward for the rest of the process's life.
    tunnel = setup_ssh_tunnel_if_configured()
    if tunnel is not None:
        _install_termination_handlers()

    try:
        # For HTTP and SSE transports, we need to specify host and port
        http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
        if transport in http_transports:
            # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
            # and bind port (defaults to 8000)
            mcp.run(
                transport=transport,
                host=mcp_config.bind_host,
                port=mcp_config.bind_port,
            )
        else:
            # For stdio transport, no host or port is needed
            mcp.run(transport=transport)
    finally:
        if tunnel is not None:
            shutdown_ssh_tunnel(tunnel)


if __name__ == "__main__":
    main()

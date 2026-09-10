import logging
import signal

from .mcp_server import mcp
from .mcp_env import get_mcp_config, TransportType
from .mcp_middleware_hook import setup_middleware
from .ssh import setup_ssh_tunnel_if_configured, shutdown_ssh_tunnel, shutdown_requested

logger = logging.getLogger("mcp-clickhouse")


def _install_termination_handlers() -> None:
    """Turn SIGTERM into SystemExit so the tunnel teardown in main() runs.

    Python's default SIGTERM disposition kills the process outright, skipping
    both `finally` blocks and atexit handlers, which would strand the ssh child.
    Only installed when a tunnel is actually running, so the no-tunnel path keeps
    upstream's signal behaviour exactly.

    SIGINT needs its own handler only to set the shutdown flag: Python's default
    already raises KeyboardInterrupt, which unwinds main()'s `finally` on its own.

    On the HTTP/SSE transports, uvicorn's Server.capture_signals() temporarily
    overwrites these handlers for the duration of serve(), then restores them and
    re-raises the captured signal via signal.raise_signal() once serve() returns.
    So the handler still fires and mcp.run()'s finally still tears down the
    tunnel, but only after uvicorn's graceful-shutdown window (fastmcp sets
    timeout_graceful_shutdown=2), i.e. up to ~2s later than on stdio.
    """

    def _terminate(signum, _frame):
        shutdown_requested.set()
        raise SystemExit(128 + int(signum))

    def _interrupt(_signum, _frame):
        shutdown_requested.set()
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except ValueError:
        logger.error(
            "Could not install SIGTERM handler (not on the main thread); SIGTERM "
            "will now kill the process without closing the SSH tunnel or draining "
            "ClickHouse clients."
        )

    try:
        signal.signal(signal.SIGINT, _interrupt)
    except ValueError:
        logger.error(
            "Could not install SIGINT handler (not on the main thread); a Ctrl-C "
            "will not be recorded as an intentional shutdown, so a tunnel exit "
            "caused by it may be logged as a crash."
        )


def main():
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport

    tunnel = None
    try:
        # Open the SSH forward, if configured, *before* anything else can reach
        # ClickHouse - including the custom middleware set up below, which is
        # user-supplied and may touch ClickHouse at import time. A client created
        # from the pre-tunnel address would be cached and would bypass the forward
        # for the rest of the process's life.
        #
        # Inside the try so that a signal arriving during ssh authentication or
        # the readiness wait still reaches the teardown below. on_configured fires
        # only when a tunnel is actually configured, so the unset-SSH path installs
        # no handler and keeps upstream's signal behaviour exactly.
        tunnel = setup_ssh_tunnel_if_configured(on_configured=_install_termination_handlers)

        # Setup any custom middleware. Inside the try so a failing middleware
        # import tears the tunnel down instead of leaking the ssh child.
        setup_middleware(mcp)

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

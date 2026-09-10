import signal
from unittest import mock

import pytest

from mcp_clickhouse import main as main_module


@pytest.fixture(autouse=True)
def restore_sigterm():
    """main() really installs a SIGTERM handler when a tunnel is present.

    Without this, a test that exercises the tunnel path permanently rewires the
    pytest process's SIGTERM disposition for every test that runs after it.
    """
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_main_runs_without_a_tunnel_when_unconfigured(clean_env):
    with (
        mock.patch.object(
            main_module, "setup_ssh_tunnel_if_configured", return_value=None
        ) as setup,
        mock.patch.object(main_module, "shutdown_ssh_tunnel") as shutdown,
        mock.patch.object(main_module.mcp, "run") as run,
        mock.patch.object(main_module, "setup_middleware"),
        mock.patch.object(main_module, "_install_termination_handlers") as install_handlers,
    ):
        main_module.main()

    setup.assert_called_once_with()
    run.assert_called_once()
    shutdown.assert_not_called()
    install_handlers.assert_not_called()


def test_main_starts_the_tunnel_before_middleware_and_the_server(clean_env):
    """The tunnel must precede user-supplied middleware, not just mcp.run().

    A MCP_MIDDLEWARE_MODULE that touches ClickHouse at import time would build
    and cache a client from the pre-tunnel address, bypassing the forward.
    """
    calls = []
    fake_tunnel = mock.Mock()

    def fake_setup():
        calls.append("setup")
        return fake_tunnel

    with (
        mock.patch.object(main_module, "setup_ssh_tunnel_if_configured", fake_setup),
        mock.patch.object(main_module, "shutdown_ssh_tunnel", lambda t: calls.append("shutdown")),
        mock.patch.object(main_module.mcp, "run", lambda **kw: calls.append("run")),
        mock.patch.object(main_module, "setup_middleware", lambda m: calls.append("middleware")),
    ):
        main_module.main()

    assert calls == ["setup", "middleware", "run", "shutdown"]


def test_main_closes_the_tunnel_when_middleware_setup_raises(clean_env):
    fake_tunnel = mock.Mock()

    with (
        mock.patch.object(main_module, "setup_ssh_tunnel_if_configured", return_value=fake_tunnel),
        mock.patch.object(main_module, "shutdown_ssh_tunnel") as shutdown,
        mock.patch.object(main_module.mcp, "run") as run,
        mock.patch.object(main_module, "setup_middleware", side_effect=RuntimeError("bad module")),
    ):
        with pytest.raises(RuntimeError):
            main_module.main()

    run.assert_not_called()
    shutdown.assert_called_once_with(fake_tunnel)


def test_main_closes_the_tunnel_when_the_server_raises(clean_env):
    fake_tunnel = mock.Mock()

    with (
        mock.patch.object(main_module, "setup_ssh_tunnel_if_configured", return_value=fake_tunnel),
        mock.patch.object(main_module, "shutdown_ssh_tunnel") as shutdown,
        mock.patch.object(main_module.mcp, "run", side_effect=RuntimeError("boom")),
        mock.patch.object(main_module, "setup_middleware"),
    ):
        try:
            main_module.main()
        except RuntimeError:
            pass

    shutdown.assert_called_once_with(fake_tunnel)


def test_sigterm_handler_raises_system_exit():
    with mock.patch("signal.signal") as sig:
        main_module._install_termination_handlers()

    assert sig.called
    handler = sig.call_args_list[0].args[1]
    try:
        handler(signal.SIGTERM, None)
    except SystemExit as exc:
        assert exc.code == 128 + int(signal.SIGTERM)
    else:
        raise AssertionError("handler did not raise SystemExit")

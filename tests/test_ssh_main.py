import signal
from unittest import mock

import pytest

from mcp_clickhouse import main as main_module
from mcp_clickhouse.ssh import shutdown_requested


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """main() really installs SIGTERM and SIGINT handlers when a tunnel is present.

    Without this, a test that exercises the tunnel path permanently rewires the
    pytest process's signal dispositions for every test that runs after it.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def clear_shutdown_flag():
    """The shutdown flag is module-level state shared by every test in the process."""
    shutdown_requested.clear()
    yield
    shutdown_requested.clear()


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


def _installed_handlers():
    with mock.patch("signal.signal") as sig:
        main_module._install_termination_handlers()

    assert sig.called
    return {call.args[0]: call.args[1] for call in sig.call_args_list}


def test_sigterm_handler_raises_system_exit_and_flags_shutdown():
    handler = _installed_handlers()[signal.SIGTERM]

    try:
        handler(signal.SIGTERM, None)
    except SystemExit as exc:
        assert exc.code == 128 + int(signal.SIGTERM)
    else:
        raise AssertionError("handler did not raise SystemExit")

    assert shutdown_requested.is_set()


def test_sigint_handler_raises_keyboard_interrupt_and_flags_shutdown():
    """The tunnel watcher reads the flag to tell a co-shutdown from a crash."""
    handler = _installed_handlers()[signal.SIGINT]

    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)

    assert shutdown_requested.is_set()

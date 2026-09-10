# ClickHouse SSH Bastion Tunnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `mcp-clickhouse` reach a ClickHouse server that is only routable through an SSH bastion, by opening an SSH local port forward at server startup and pointing the ClickHouse client at it.

**Architecture:** A new self-contained `mcp_clickhouse/ssh/` package spawns the system `ssh` binary with `-L <free port>:<clickhouse host>:<clickhouse port>`, waits for the forward to accept TCP, then rewrites `CLICKHOUSE_HOST`/`CLICKHOUSE_PORT` in `os.environ` before `mcp.run()` is called. Upstream's `ClickHouseConfig` reads `os.environ` on every property access, so no upstream module needs to change except a small edit to `main.py`.

**Tech Stack:** Python 3.10+, stdlib only for the new package (`subprocess`, `socket`, `threading`, `signal`, `dataclasses`), pytest for tests, `ruff` for lint.

**Spec:** `docs/superpowers/specs/2026-09-10-clickhouse-ssh-tunnel-design.md`

## Global Constraints

- Python floor is **3.10** (`pyproject.toml: requires-python = ">=3.10"`). Do not use `match`-only-3.11 features, `typing.Self`, or `except*`. `X | None` annotations are fine at 3.10 only inside `from __future__ import annotations` modules — every new module in this plan starts with that import, so use it consistently.
- Ruff line length is **100** (`[tool.ruff] line-length = 100`). Run `uv run ruff check .` and `uv run ruff format --check .` before every commit.
- The new package adds **no new runtime dependencies**. Standard library only.
- **The feature is opt-in.** When `CLICKHOUSE_SSH_HOST` is unset or blank, every code path added by this plan must be a no-op and upstream behaviour must be unchanged.
- No changes to `mcp_clickhouse/mcp_env.py` or `mcp_clickhouse/mcp_server.py`. The only upstream file modified is `mcp_clickhouse/main.py`.
- Logger names live under upstream's `MCP_SERVER_NAME = "mcp-clickhouse"`; new modules use `logging.getLogger("mcp-clickhouse.ssh")` so they inherit its configuration.
- Environment variable names are exactly: `CLICKHOUSE_SSH_HOST`, `CLICKHOUSE_SSH_USER`, `CLICKHOUSE_SSH_PORT`, `CLICKHOUSE_SSH_KEY`, `CLICKHOUSE_SSH_READY_TIMEOUT`.
- macOS and Linux only. Shell fixtures may assume `/usr/bin/env sh` and `python3`.
- Every test that touches `os.environ` must restore it. Task 1 creates the fixture that does this; later tasks reuse it.

---

## File Structure

**Create:**

- `mcp_clickhouse/ssh/__init__.py` — public exports for the package
- `mcp_clickhouse/ssh/config.py` — `SshTunnelConfig` dataclass, `get_ssh_tunnel_config()`, env parsing and validation
- `mcp_clickhouse/ssh/tunnel.py` — `TunnelError`, `pick_free_port()`, `build_ssh_argv()`, `wait_for_tcp_ready()`, `start_tunnel()`, `ActiveTunnel`
- `mcp_clickhouse/ssh/startup.py` — `setup_ssh_tunnel_if_configured()`, `apply_tunnel_to_environment()`, `shutdown_ssh_tunnel()`
- `tests/test_ssh_config.py` — tests for `config.py`
- `tests/test_ssh_tunnel.py` — tests for `tunnel.py`
- `tests/test_ssh_startup.py` — tests for `startup.py`
- `tests/test_ssh_integration.py` — one opt-in test against the real `ssh` binary
- `tests/fixtures/ssh/fake-ssh-ok.sh` — binds the forwarded port, idles
- `tests/fixtures/ssh/fake-ssh-crash.sh` — writes to stderr, exits 255
- `tests/fixtures/ssh/fake-ssh-hang.sh` — idles without binding
- `tests/fixtures/ssh/fake-ssh-ignore-sigterm.sh` — binds, ignores SIGTERM
- `tests/fixtures/ssh/README.md` — explains the fixture technique

**Modify:**

- `tests/conftest.py` — add the `os.environ` snapshot/restore fixture
- `mcp_clickhouse/main.py` — start the tunnel before `mcp.run()`, tear it down after
- `README.md` — new "SSH bastion tunnel" section, fork note
- `CHANGELOG.md` — fork entry

---

## Task 1: `SshTunnelConfig` and environment parsing

**Files:**
- Create: `mcp_clickhouse/ssh/__init__.py`
- Create: `mcp_clickhouse/ssh/config.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_ssh_config.py`

**Interfaces:**
- Consumes: `mcp_clickhouse.mcp_env.ClickHouseConfig` (upstream, unmodified) for `.host`, `.port`, `.secure`
- Produces:
  - `SshTunnelConfig` frozen dataclass with fields `ssh_host: str`, `remote_host: str`, `remote_port: int`, `ssh_user: str | None`, `ssh_port: int | None`, `ssh_key: str | None`, `ready_timeout: float`
  - `get_ssh_tunnel_config() -> SshTunnelConfig | None`
  - `DEFAULT_READY_TIMEOUT: float = 10.0`
  - pytest fixture `clean_env` in `tests/conftest.py`

**Why `ClickHouseConfig` is reused rather than re-reading `CLICKHOUSE_PORT` directly:** upstream's port default is `8443` when secure and `8123` otherwise, and `CLICKHOUSE_PORT` is frequently unset. Duplicating that rule here would silently drift from upstream. `ClickHouseConfig` is constructed fresh (not via the `get_config()` singleton) so this call cannot install a singleton built from pre-tunnel values.

- [ ] **Step 1: Add the environment-restoring fixture to `tests/conftest.py`**

Append to the existing `tests/conftest.py` (keep the existing `clear_health_result_cache` fixture as-is):

```python
import os


@pytest.fixture
def clean_env(monkeypatch):
    """Give a test an isolated view of the ClickHouse and SSH environment.

    Clears every variable the SSH tunnel reads or writes so a developer's real
    .env cannot leak into assertions, and lets monkeypatch restore os.environ
    when the test ends.
    """
    for name in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_SECURE",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_SERVER_HOST_NAME",
        "CLICKHOUSE_ENABLED",
        "CLICKHOUSE_SSH_HOST",
        "CLICKHOUSE_SSH_USER",
        "CLICKHOUSE_SSH_PORT",
        "CLICKHOUSE_SSH_KEY",
        "CLICKHOUSE_SSH_READY_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ssh_config.py`:

```python
import pytest

from mcp_clickhouse.ssh.config import DEFAULT_READY_TIMEOUT, get_ssh_tunnel_config


def test_returns_none_when_ssh_host_unset(clean_env):
    assert get_ssh_tunnel_config() is None


def test_returns_none_when_ssh_host_blank(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "   ")
    assert get_ssh_tunnel_config() is None


def test_reads_remote_target_from_clickhouse_settings(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion.example.com")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_PORT", "8123")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    cfg = get_ssh_tunnel_config()

    assert cfg is not None
    assert cfg.ssh_host == "bastion.example.com"
    assert cfg.remote_host == "ch.internal"
    assert cfg.remote_port == 8123
    assert cfg.ssh_user is None
    assert cfg.ssh_port is None
    assert cfg.ssh_key is None
    assert cfg.ready_timeout == DEFAULT_READY_TIMEOUT


def test_remote_port_defaults_to_8443_when_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")

    cfg = get_ssh_tunnel_config()

    assert cfg.remote_port == 8443


def test_remote_port_defaults_to_8123_when_not_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    cfg = get_ssh_tunnel_config()

    assert cfg.remote_port == 8123


def test_reads_optional_ssh_settings(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_SSH_USER", "ec2-user")
    clean_env.setenv("CLICKHOUSE_SSH_PORT", "2222")
    clean_env.setenv("CLICKHOUSE_SSH_KEY", "~/.ssh/bastion.pem")
    clean_env.setenv("CLICKHOUSE_SSH_READY_TIMEOUT", "25")

    cfg = get_ssh_tunnel_config()

    assert cfg.ssh_user == "ec2-user"
    assert cfg.ssh_port == 2222
    assert cfg.ssh_key == "~/.ssh/bastion.pem"
    assert cfg.ready_timeout == 25.0


@pytest.mark.parametrize("bad", ["0", "65536", "-1", "not-a-port", "22.5"])
def test_rejects_bad_ssh_port(clean_env, bad):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_SSH_PORT", bad)

    with pytest.raises(ValueError) as exc:
        get_ssh_tunnel_config()
    assert bad in str(exc.value)


@pytest.mark.parametrize("bad", ["0", "-3", "nope"])
def test_rejects_bad_ready_timeout(clean_env, bad):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_SSH_READY_TIMEOUT", bad)

    with pytest.raises(ValueError) as exc:
        get_ssh_tunnel_config()
    assert bad in str(exc.value)


def test_missing_clickhouse_host_is_a_clear_error(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_ENABLED", "false")

    with pytest.raises(ValueError) as exc:
        get_ssh_tunnel_config()
    assert "CLICKHOUSE_HOST" in str(exc.value)


def test_warns_when_remote_port_is_native_protocol(clean_env, caplog):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_PORT", "9000")

    with caplog.at_level("WARNING"):
        cfg = get_ssh_tunnel_config()

    assert cfg.remote_port == 9000
    assert "9000" in caplog.text
    assert "HTTP" in caplog.text
```

`test_missing_clickhouse_host_is_a_clear_error` sets `CLICKHOUSE_ENABLED=false` so upstream's constructor skips its own validation and the missing-host case reaches our error path.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_clickhouse.ssh'`

- [ ] **Step 4: Create the package init**

Create `mcp_clickhouse/ssh/__init__.py`:

```python
"""SSH local port forwarding so ClickHouse can be reached through a bastion host.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts, src/ssh/startup.ts).
"""

from .config import DEFAULT_READY_TIMEOUT, SshTunnelConfig, get_ssh_tunnel_config

__all__ = [
    "DEFAULT_READY_TIMEOUT",
    "SshTunnelConfig",
    "get_ssh_tunnel_config",
]
```

Later tasks extend `__all__`; do not add names for code that does not exist yet.

- [ ] **Step 5: Implement `config.py`**

Create `mcp_clickhouse/ssh/config.py`:

```python
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
    """A single SSH local port forward to a ClickHouse endpoint."""

    ssh_host: str
    remote_host: str
    remote_port: int
    ssh_user: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_key: Optional[str] = None
    ready_timeout: float = DEFAULT_READY_TIMEOUT


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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh_config.py -v`
Expected: PASS, all cases.

Also run the full suite to prove upstream is untouched:
Run: `uv run pytest -q`
Expected: same result as on a clean `upstream/main` checkout.

- [ ] **Step 7: Lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean. If `ruff format` reports files, run `uv run ruff format .` and re-check.

- [ ] **Step 8: Commit**

```bash
git add mcp_clickhouse/ssh/__init__.py mcp_clickhouse/ssh/config.py tests/conftest.py tests/test_ssh_config.py
git commit -m "feat(ssh): add CLICKHOUSE_SSH_* env parsing"
```

---

## Task 2: Tunnel primitives — free port, argv, TCP readiness

**Files:**
- Create: `mcp_clickhouse/ssh/tunnel.py`
- Modify: `mcp_clickhouse/ssh/__init__.py`
- Test: `tests/test_ssh_tunnel.py`

**Interfaces:**
- Consumes: `SshTunnelConfig` from Task 1
- Produces:
  - `class TunnelError(RuntimeError)`
  - `pick_free_port() -> int`
  - `build_ssh_argv(cfg: SshTunnelConfig, local_port: int) -> list[str]` — returns ssh *arguments only*, without the binary name at index 0
  - `wait_for_tcp_ready(port: int, timeout: float, interval: float = READINESS_POLL_INTERVAL) -> None`
  - `READINESS_POLL_INTERVAL: float = 0.05`
  - `SHUTDOWN_GRACE: float = 3.0`
  - `STDERR_TAIL_BYTES: int = 500`

`build_ssh_argv` deliberately excludes the binary name so tests can assert on flags without threading the `spawn_path` through, and so `start_tunnel` can prepend a fake binary in Task 3.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ssh_tunnel.py`:

```python
import socket
import time

import pytest

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.tunnel import (
    TunnelError,
    build_ssh_argv,
    pick_free_port,
    wait_for_tcp_ready,
)


def minimal_config(**overrides) -> SshTunnelConfig:
    base = {
        "ssh_host": "bastion.example.com",
        "remote_host": "ch.internal",
        "remote_port": 8443,
    }
    base.update(overrides)
    return SshTunnelConfig(**base)


def test_pick_free_port_returns_a_bindable_port():
    port = pick_free_port()

    assert 1024 < port <= 65535
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_pick_free_port_varies():
    ports = {pick_free_port() for _ in range(5)}
    assert len(ports) > 1


def test_build_ssh_argv_minimal():
    argv = build_ssh_argv(minimal_config(), 34567)

    assert argv == [
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-L",
        "34567:ch.internal:8443",
        "bastion.example.com",
    ]


def test_build_ssh_argv_includes_optional_flags():
    cfg = minimal_config(ssh_user="ec2-user", ssh_port=2222, ssh_key="/tmp/key.pem")

    argv = build_ssh_argv(cfg, 1234)

    assert argv[-9:] == [
        "-L",
        "1234:ch.internal:8443",
        "-l",
        "ec2-user",
        "-p",
        "2222",
        "-i",
        "/tmp/key.pem",
        "bastion.example.com",
    ]


def test_build_ssh_argv_expands_home_in_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    argv = build_ssh_argv(minimal_config(ssh_key="~/.ssh/id_ed25519"), 1234)

    assert argv[argv.index("-i") + 1] == str(tmp_path / ".ssh/id_ed25519")


def test_wait_for_tcp_ready_returns_once_port_accepts():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        wait_for_tcp_ready(port, timeout=2.0)


def test_wait_for_tcp_ready_times_out_on_dead_port():
    port = pick_free_port()

    started = time.monotonic()
    with pytest.raises(TunnelError) as exc:
        wait_for_tcp_ready(port, timeout=0.3)
    elapsed = time.monotonic() - started

    assert str(port) in str(exc.value)
    assert 0.25 <= elapsed < 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh_tunnel.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_ssh_argv'`

- [ ] **Step 3: Implement the primitives**

Create `mcp_clickhouse/ssh/tunnel.py`:

```python
"""Spawn and supervise an SSH local port forward.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts). The system `ssh`
binary is used rather than a Python SSH library so that ~/.ssh/config — Host
aliases, ProxyJump chains, IdentityFile — ssh-agent, and hardware-backed keys
all work without reimplementation.
"""

from __future__ import annotations

import logging
import os
import socket
import time

from .config import SshTunnelConfig

logger = logging.getLogger("mcp-clickhouse.ssh")

READINESS_POLL_INTERVAL = 0.05
SHUTDOWN_GRACE = 3.0
STDERR_TAIL_BYTES = 500
_CONNECT_PROBE_TIMEOUT = 0.5


class TunnelError(RuntimeError):
    """Raised when the SSH tunnel cannot be established or supervised."""


def pick_free_port() -> int:
    """Ask the kernel for an unused loopback port.

    Inherently racy: another process can claim the port between this call and
    ssh binding it. `ExitOnForwardFailure=yes` turns a lost race into a clean
    startup error rather than a silently unforwarded port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_ssh_argv(cfg: SshTunnelConfig, local_port: int) -> list[str]:
    """Build the ssh arguments (without the binary name) for the forward."""
    argv = [
        "-N",
        "-T",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        # BatchMode makes a key that needs an interactive passphrase fail fast
        # instead of blocking on a prompt with nowhere to render under stdio.
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-L",
        f"{local_port}:{cfg.remote_host}:{cfg.remote_port}",
    ]
    if cfg.ssh_user:
        argv += ["-l", cfg.ssh_user]
    if cfg.ssh_port is not None:
        argv += ["-p", str(cfg.ssh_port)]
    if cfg.ssh_key:
        argv += ["-i", os.path.expanduser(cfg.ssh_key)]
    argv.append(cfg.ssh_host)
    return argv


def try_connect(port: int) -> bool:
    """Return whether 127.0.0.1:port accepts a TCP connection right now."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_CONNECT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def wait_for_tcp_ready(
    port: int,
    timeout: float,
    interval: float = READINESS_POLL_INTERVAL,
) -> None:
    """Block until 127.0.0.1:port accepts a connection, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if try_connect(port):
            return
        if time.monotonic() >= deadline:
            raise TunnelError(
                f"TCP readiness timeout: 127.0.0.1:{port} did not accept within {timeout}s"
            )
        time.sleep(interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh_tunnel.py -v`
Expected: PASS

- [ ] **Step 5: Export from the package**

Edit `mcp_clickhouse/ssh/__init__.py` to read:

```python
"""SSH local port forwarding so ClickHouse can be reached through a bastion host.

Ported from bartekbp/mcp-server-mysql (src/ssh/tunnel.ts, src/ssh/startup.ts).
"""

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
```

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add mcp_clickhouse/ssh/tunnel.py mcp_clickhouse/ssh/__init__.py tests/test_ssh_tunnel.py
git commit -m "feat(ssh): add free-port, argv and TCP readiness helpers"
```

---

## Task 3: Fake `ssh` fixtures and `start_tunnel` happy path

**Files:**
- Create: `tests/fixtures/ssh/fake-ssh-ok.sh`
- Create: `tests/fixtures/ssh/fake-ssh-crash.sh`
- Create: `tests/fixtures/ssh/fake-ssh-hang.sh`
- Create: `tests/fixtures/ssh/fake-ssh-ignore-sigterm.sh`
- Create: `tests/fixtures/ssh/README.md`
- Modify: `mcp_clickhouse/ssh/tunnel.py`
- Test: `tests/test_ssh_tunnel.py`

**Interfaces:**
- Consumes: `build_ssh_argv`, `pick_free_port`, `try_connect`, `TunnelError` from Task 2
- Produces:
  - `class ActiveTunnel` with attribute `local_port: int` and method `close(grace: float = SHUTDOWN_GRACE) -> None`
  - `start_tunnel(cfg: SshTunnelConfig, *, spawn_path: str = "ssh", on_runtime_exit: Callable[[int | None, str], None] | None = None) -> ActiveTunnel`
  - `_StderrTail` internal helper with `.text() -> str`

All four fixtures are created in this task even though only two are exercised here — they are one conceptual unit and later tasks reference them by name.

- [ ] **Step 1: Create the fake ssh fixtures**

Create `tests/fixtures/ssh/fake-ssh-ok.sh`:

```sh
#!/usr/bin/env sh
# Stands in for `ssh`: parse -L <localPort>:<remoteHost>:<remotePort>, then bind
# localPort so the readiness probe succeeds. Exits on SIGTERM.
LOCAL_PORT=""
next_is_L=""
for arg in "$@"; do
  if [ "$next_is_L" = "1" ]; then
    LOCAL_PORT=$(echo "$arg" | cut -d: -f1)
    next_is_L=""
    break
  fi
  if [ "$arg" = "-L" ]; then
    next_is_L="1"
  fi
done
if [ -z "$LOCAL_PORT" ]; then
  echo "fake-ssh-ok: no -L arg found" >&2
  exit 2
fi
exec python3 -c "
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $LOCAL_PORT))
s.listen(8)
while True:
    time.sleep(60)
"
```

Create `tests/fixtures/ssh/fake-ssh-crash.sh`:

```sh
#!/usr/bin/env sh
# Stands in for `ssh` refusing the connection. Exits before binding anything.
echo "Permission denied (publickey)." >&2
echo "fake-ssh-crash: simulated auth failure" >&2
exit 255
```

Create `tests/fixtures/ssh/fake-ssh-hang.sh`:

```sh
#!/usr/bin/env sh
# Stands in for `ssh` that connects but never establishes the forward.
# Exercises the readiness timeout.
exec python3 -c "
import time
while True:
    time.sleep(60)
"
```

Create `tests/fixtures/ssh/fake-ssh-ignore-sigterm.sh`:

```sh
#!/usr/bin/env sh
# Binds the forwarded port like fake-ssh-ok, but ignores SIGTERM so close()
# has to fall through to SIGKILL.
LOCAL_PORT=""
next_is_L=""
for arg in "$@"; do
  if [ "$next_is_L" = "1" ]; then
    LOCAL_PORT=$(echo "$arg" | cut -d: -f1)
    next_is_L=""
    break
  fi
  if [ "$arg" = "-L" ]; then
    next_is_L="1"
  fi
done
if [ -z "$LOCAL_PORT" ]; then
  echo "fake-ssh-ignore-sigterm: no -L arg found" >&2
  exit 2
fi
exec python3 -c "
import signal, socket, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $LOCAL_PORT))
s.listen(8)
while True:
    time.sleep(60)
"
```

Create `tests/fixtures/ssh/README.md`:

```markdown
# Fake SSH fixtures

Shell scripts that stand in for the `ssh` binary in unit tests. Each parses the
`-L localPort:remoteHost:remotePort` argument and simulates one scenario. Tests
select one by passing its path as `start_tunnel(..., spawn_path=...)`, so no real
SSH server, key, or network is needed.

| Fixture | Simulates |
|---|---|
| `fake-ssh-ok.sh` | Forward established; port accepts |
| `fake-ssh-crash.sh` | Auth failure; exits 255 with stderr |
| `fake-ssh-hang.sh` | Process alive, forward never comes up |
| `fake-ssh-ignore-sigterm.sh` | Forward established, but SIGTERM is ignored |

All four must stay executable (`chmod +x`). Git tracks the executable bit.
```

Make them executable:

```bash
chmod +x tests/fixtures/ssh/*.sh
```

- [ ] **Step 2: Write the failing happy-path test**

Append to `tests/test_ssh_tunnel.py`:

```python
import pathlib

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ssh"


def fake_ssh(name: str) -> str:
    path = FIXTURES / name
    assert path.exists(), f"missing fixture {path}"
    return str(path)


def test_start_tunnel_returns_once_the_forward_accepts():
    from mcp_clickhouse.ssh.tunnel import start_tunnel, try_connect

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ok.sh"))
    try:
        assert tunnel.local_port > 0
        assert try_connect(tunnel.local_port)
    finally:
        tunnel.close()


def test_start_tunnel_passes_the_remote_target_through_to_ssh(tmp_path):
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    recorder = tmp_path / "argv.txt"
    script = tmp_path / "record-ssh.sh"
    # Absolute path to the fixture: the wrapper lives in tmp_path, so a relative
    # path would not resolve.
    script.write_text(
        "#!/usr/bin/env sh\n"
        f'printf "%s\\n" "$@" > {recorder}\n'
        f'exec {fake_ssh("fake-ssh-ok.sh")} "$@"\n'
    )
    script.chmod(0o755)

    cfg = minimal_config(remote_host="ch.internal", remote_port=8443)
    tunnel = start_tunnel(cfg, spawn_path=str(script))
    try:
        recorded = recorder.read_text().splitlines()
        assert f"{tunnel.local_port}:ch.internal:8443" in recorded
        assert "bastion.example.com" in recorded
    finally:
        tunnel.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh_tunnel.py -k start_tunnel -v`
Expected: FAIL — `ImportError: cannot import name 'start_tunnel'`

- [ ] **Step 4: Implement `_StderrTail`, `ActiveTunnel` and `start_tunnel`**

Append to `mcp_clickhouse/ssh/tunnel.py` (and extend the imports at the top of the file to include `subprocess`, `threading`, and `from typing import Callable, Optional`):

```python
class _StderrTail(threading.Thread):
    """Drain ssh's stderr, log each line, and keep a rolling tail for errors."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._buf = ""
        self._lock = threading.Lock()

    def run(self) -> None:
        if self._stream is None:
            return
        try:
            for raw in iter(self._stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                logger.info("[ssh] %s", line)
                with self._lock:
                    self._buf = (self._buf + line + "\n")[-STDERR_TAIL_BYTES:]
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def text(self) -> str:
        with self._lock:
            return self._buf.strip()


class ActiveTunnel:
    """A running ssh forward. Close it to tear the forward down."""

    def __init__(
        self,
        local_port: int,
        proc: "subprocess.Popen[bytes]",
        tail: _StderrTail,
        on_runtime_exit: Optional[Callable[[Optional[int], str], None]] = None,
    ) -> None:
        self.local_port = local_port
        self._proc = proc
        self._tail = tail
        self._on_runtime_exit = on_runtime_exit or _default_runtime_exit
        self._closing = False
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        code = self._proc.wait()
        if self._closing:
            return
        self._tail.join(timeout=1.0)
        tail = self._tail.text()
        logger.error(
            "[ssh] tunnel exited unexpectedly (code %s). stderr tail: %s", code, tail
        )
        self._on_runtime_exit(code, tail)

    def close(self, grace: float = SHUTDOWN_GRACE) -> None:
        """Stop the forward. Safe to call more than once."""
        if self._closing:
            return
        self._closing = True
        if self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            logger.warning("[ssh] tunnel ignored SIGTERM; sending SIGKILL")
        self._proc.kill()
        try:
            self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            logger.error("[ssh] tunnel survived SIGKILL")


def _default_runtime_exit(code: Optional[int], stderr_tail: str) -> None:
    """Exit the process when the tunnel dies mid-session.

    A dead forward leaves every later query failing against a closed local port,
    with cached clients holding connections that will never recover. Exiting
    surfaces the problem immediately and lets the MCP client restart the server.
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        logger.error("[test mode] would have exited(1) after tunnel death")
        return
    os._exit(1)


def start_tunnel(
    cfg: SshTunnelConfig,
    *,
    spawn_path: str = "ssh",
    on_runtime_exit: Optional[Callable[[Optional[int], str], None]] = None,
) -> ActiveTunnel:
    """Open the forward and block until it accepts TCP, or raise TunnelError."""
    if cfg.ssh_key:
        expanded = os.path.expanduser(cfg.ssh_key)
        if not os.path.exists(expanded):
            raise TunnelError(f"SSH key not found: {cfg.ssh_key}")

    local_port = pick_free_port()
    argv = [spawn_path] + build_ssh_argv(cfg, local_port)

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise TunnelError(
            f"SSH tunnel: '{spawn_path}' not found on PATH. Install an OpenSSH client."
        ) from exc

    tail = _StderrTail(proc.stderr)
    tail.start()

    deadline = time.monotonic() + cfg.ready_timeout
    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            tail.join(timeout=1.0)
            raise TunnelError(f"SSH tunnel failed (exit {exit_code}): {tail.text()}")
        if try_connect(local_port):
            break
        if time.monotonic() >= deadline:
            _kill_quietly(proc)
            tail.join(timeout=1.0)
            raise TunnelError(
                f"SSH tunnel readiness timeout: 127.0.0.1:{local_port} did not accept "
                f"within {cfg.ready_timeout}s. ssh stderr: {tail.text()}"
            )
        time.sleep(READINESS_POLL_INTERVAL)

    return ActiveTunnel(local_port, proc, tail, on_runtime_exit)


def _kill_quietly(proc: "subprocess.Popen[bytes]") -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=SHUTDOWN_GRACE)
    except subprocess.TimeoutExpired:
        logger.error("[ssh] failed to kill tunnel process %s", proc.pid)
```

Note the readiness loop checks `proc.poll()` *before* probing the port, so a fixture that exits instantly is reported as an early exit rather than a timeout.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh_tunnel.py -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add tests/fixtures/ssh mcp_clickhouse/ssh/tunnel.py tests/test_ssh_tunnel.py
git commit -m "feat(ssh): implement start_tunnel with fake-ssh test fixtures"
```

Verify the executable bit survived:

```bash
git ls-files -s tests/fixtures/ssh/
```
Expected: mode `100755` on all four `.sh` files. If any is `100644`, run `chmod +x` and `git update-index --chmod=+x <path>`, then amend.

---

## Task 4: Startup failure paths

**Files:**
- Test: `tests/test_ssh_tunnel.py`

**Interfaces:**
- Consumes: `start_tunnel`, `TunnelError` from Task 3; all four fixtures from Task 3
- Produces: nothing new — this task proves the error behaviour Task 3 implemented

No production code should be needed. If a test here fails, fix `tunnel.py`; do not weaken the test.

- [ ] **Step 1: Write the failure-path tests**

Append to `tests/test_ssh_tunnel.py`:

```python
def test_start_tunnel_reports_early_exit_with_stderr_tail():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    with pytest.raises(TunnelError) as exc:
        start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-crash.sh"))

    message = str(exc.value)
    assert "exit 255" in message
    assert "Permission denied (publickey)." in message


def test_start_tunnel_times_out_when_forward_never_comes_up():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    cfg = minimal_config(ready_timeout=0.4)

    started = time.monotonic()
    with pytest.raises(TunnelError) as exc:
        start_tunnel(cfg, spawn_path=fake_ssh("fake-ssh-hang.sh"))
    elapsed = time.monotonic() - started

    assert "readiness timeout" in str(exc.value)
    assert 0.3 <= elapsed < 5.0


def test_start_tunnel_rejects_missing_key_without_spawning(tmp_path):
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    missing = tmp_path / "nope.pem"
    cfg = minimal_config(ssh_key=str(missing))

    with pytest.raises(TunnelError) as exc:
        start_tunnel(cfg, spawn_path=fake_ssh("fake-ssh-ok.sh"))

    assert str(missing) in str(exc.value)


def test_start_tunnel_accepts_an_existing_key(tmp_path):
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    key = tmp_path / "id.pem"
    key.write_text("not a real key")
    cfg = minimal_config(ssh_key=str(key))

    tunnel = start_tunnel(cfg, spawn_path=fake_ssh("fake-ssh-ok.sh"))
    tunnel.close()


def test_start_tunnel_reports_missing_ssh_binary():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    with pytest.raises(TunnelError) as exc:
        start_tunnel(minimal_config(), spawn_path="/nonexistent/ssh-binary")

    assert "not found on PATH" in str(exc.value)
    assert "OpenSSH" in str(exc.value)
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_ssh_tunnel.py -v`
Expected: PASS. If the timeout test hangs past 5 s, the readiness loop is not checking its deadline — fix `start_tunnel`.

- [ ] **Step 3: Confirm no stray processes are left behind**

Run: `uv run pytest tests/test_ssh_tunnel.py -q && pgrep -f fake-ssh || echo "no leftovers"`
Expected: `no leftovers`. A leftover means the timeout path is not killing the child.

- [ ] **Step 4: Lint and commit**

```bash
git add tests/test_ssh_tunnel.py
git commit -m "test(ssh): cover start_tunnel failure paths"
```

---

## Task 5: Shutdown and runtime-death behaviour

**Files:**
- Test: `tests/test_ssh_tunnel.py`

**Interfaces:**
- Consumes: `ActiveTunnel.close`, `start_tunnel(on_runtime_exit=...)` from Task 3
- Produces: nothing new

- [ ] **Step 1: Write the shutdown tests**

Append to `tests/test_ssh_tunnel.py`:

```python
def test_close_stops_the_forward():
    from mcp_clickhouse.ssh.tunnel import start_tunnel, try_connect

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ok.sh"))
    port = tunnel.local_port
    assert try_connect(port)

    tunnel.close()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not try_connect(port):
            break
        time.sleep(0.05)
    assert not try_connect(port)


def test_close_is_idempotent():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    tunnel = start_tunnel(minimal_config(), spawn_path=fake_ssh("fake-ssh-ok.sh"))
    tunnel.close()
    tunnel.close()


def test_close_falls_back_to_sigkill():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    tunnel = start_tunnel(
        minimal_config(), spawn_path=fake_ssh("fake-ssh-ignore-sigterm.sh")
    )

    started = time.monotonic()
    tunnel.close(grace=0.5)
    elapsed = time.monotonic() - started

    assert tunnel._proc.poll() is not None
    assert 0.4 <= elapsed < 5.0


def test_close_does_not_fire_the_runtime_exit_callback():
    from mcp_clickhouse.ssh.tunnel import start_tunnel

    fired = []
    tunnel = start_tunnel(
        minimal_config(),
        spawn_path=fake_ssh("fake-ssh-ok.sh"),
        on_runtime_exit=lambda code, tail: fired.append((code, tail)),
    )

    tunnel.close()
    time.sleep(0.3)

    assert fired == []


def test_runtime_exit_callback_fires_when_the_tunnel_dies():
    import threading

    from mcp_clickhouse.ssh.tunnel import start_tunnel

    fired = threading.Event()
    captured = {}

    def on_exit(code, tail):
        captured["code"] = code
        fired.set()

    tunnel = start_tunnel(
        minimal_config(),
        spawn_path=fake_ssh("fake-ssh-ok.sh"),
        on_runtime_exit=on_exit,
    )
    tunnel._proc.kill()

    assert fired.wait(timeout=5.0), "on_runtime_exit never fired"
    assert captured["code"] is not None
```

`test_close_falls_back_to_sigkill` and the last two tests reach into `tunnel._proc`. That is deliberate: the alternative is exposing the child process on the public surface purely for tests.

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_ssh_tunnel.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_ssh_tunnel.py
git commit -m "test(ssh): cover graceful close, SIGKILL fallback and runtime death"
```

---

## Task 6: Startup wiring and environment rewrite

**Files:**
- Create: `mcp_clickhouse/ssh/startup.py`
- Modify: `mcp_clickhouse/ssh/__init__.py`
- Test: `tests/test_ssh_startup.py`

**Interfaces:**
- Consumes: `get_ssh_tunnel_config`, `SshTunnelConfig` (Task 1); `start_tunnel`, `ActiveTunnel` (Task 3)
- Produces:
  - `setup_ssh_tunnel_if_configured(**start_tunnel_kwargs) -> ActiveTunnel | None`
  - `apply_tunnel_to_environment(cfg: SshTunnelConfig, local_port: int) -> None`
  - `shutdown_ssh_tunnel(tunnel: ActiveTunnel) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ssh_startup.py`:

```python
import os
import pathlib

from mcp_clickhouse.ssh.config import SshTunnelConfig
from mcp_clickhouse.ssh.startup import (
    apply_tunnel_to_environment,
    setup_ssh_tunnel_if_configured,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ssh"


def a_config(**overrides) -> SshTunnelConfig:
    base = {
        "ssh_host": "bastion.example.com",
        "remote_host": "ch.internal",
        "remote_port": 8443,
    }
    base.update(overrides)
    return SshTunnelConfig(**base)


def test_setup_is_a_noop_when_not_configured(clean_env):
    assert setup_ssh_tunnel_if_configured() is None


def test_apply_rewrites_host_and_port(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_HOST"] == "127.0.0.1"
    assert os.environ["CLICKHOUSE_PORT"] == "45678"


def test_apply_sets_server_host_name_when_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "ch.internal"


def test_apply_leaves_server_host_name_alone_when_not_secure(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")

    apply_tunnel_to_environment(a_config(), 45678)

    assert "CLICKHOUSE_SERVER_HOST_NAME" not in os.environ


def test_apply_does_not_clobber_an_explicit_server_host_name(clean_env):
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")
    clean_env.setenv("CLICKHOUSE_SERVER_HOST_NAME", "chosen-by-user")

    apply_tunnel_to_environment(a_config(), 45678)

    assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "chosen-by-user"


def test_setup_starts_a_tunnel_and_rewrites_the_environment(clean_env):
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion.example.com")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "false")
    clean_env.setenv("CLICKHOUSE_PORT", "8123")

    tunnel = setup_ssh_tunnel_if_configured(
        spawn_path=str(FIXTURES / "fake-ssh-ok.sh")
    )
    try:
        assert tunnel is not None
        assert os.environ["CLICKHOUSE_HOST"] == "127.0.0.1"
        assert os.environ["CLICKHOUSE_PORT"] == str(tunnel.local_port)
    finally:
        tunnel.close()


def test_config_read_happens_before_the_rewrite(clean_env):
    """The remote target must be the pre-rewrite ClickHouse address."""
    clean_env.setenv("CLICKHOUSE_SSH_HOST", "bastion.example.com")
    clean_env.setenv("CLICKHOUSE_HOST", "ch.internal")
    clean_env.setenv("CLICKHOUSE_USER", "reader")
    clean_env.setenv("CLICKHOUSE_SECURE", "true")

    tunnel = setup_ssh_tunnel_if_configured(
        spawn_path=str(FIXTURES / "fake-ssh-ok.sh")
    )
    try:
        # 8443 is the secure default; had the rewrite happened first, this would
        # be the loopback port instead of the real host name.
        assert os.environ["CLICKHOUSE_SERVER_HOST_NAME"] == "ch.internal"
    finally:
        tunnel.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh_startup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_clickhouse.ssh.startup'`

- [ ] **Step 3: Implement `startup.py`**

Create `mcp_clickhouse/ssh/startup.py`:

```python
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
```

`mcp_server._shutdown()` is upstream-private but is the only call that drains the query executors *and* clears the client cache; `_clear_client_cache()` alone would leave in-flight workers holding clients. It is also registered with `atexit` upstream, and is safe to call twice — the second call finds already-shut-down executors and an empty cache.

The `mcp_server` import is deferred into the function body so that importing `mcp_clickhouse.ssh` never pulls in the heavy server module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh_startup.py -v`
Expected: PASS

- [ ] **Step 5: Export from the package**

Edit `mcp_clickhouse/ssh/__init__.py`, adding to the imports and `__all__`:

```python
from .startup import (
    apply_tunnel_to_environment,
    setup_ssh_tunnel_if_configured,
    shutdown_ssh_tunnel,
)
from .tunnel import ActiveTunnel, start_tunnel
```

Final `__all__`:

```python
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
    "shutdown_ssh_tunnel",
    "start_tunnel",
    "wait_for_tcp_ready",
]
```

Note `startup.py` must be imported *after* `tunnel.py` in the file to avoid a partially-initialised module; keep imports alphabetised by module (`config`, `startup`, `tunnel`) — Python resolves this fine because `startup` imports `tunnel` directly, not via the package.

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add mcp_clickhouse/ssh/startup.py mcp_clickhouse/ssh/__init__.py tests/test_ssh_startup.py
git commit -m "feat(ssh): start tunnel and repoint ClickHouse env at the forward"
```

---

## Task 7: Hook the tunnel into `main()`

**Files:**
- Modify: `mcp_clickhouse/main.py`
- Test: `tests/test_ssh_main.py`

**Interfaces:**
- Consumes: `setup_ssh_tunnel_if_configured`, `shutdown_ssh_tunnel` from Task 6
- Produces: `_install_termination_handlers() -> None` in `main.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ssh_main.py`:

```python
from unittest import mock

from mcp_clickhouse import main as main_module


def test_main_runs_without_a_tunnel_when_unconfigured(clean_env):
    with (
        mock.patch.object(main_module, "setup_ssh_tunnel_if_configured", return_value=None) as setup,
        mock.patch.object(main_module, "shutdown_ssh_tunnel") as shutdown,
        mock.patch.object(main_module.mcp, "run") as run,
        mock.patch.object(main_module, "setup_middleware"),
    ):
        main_module.main()

    setup.assert_called_once_with()
    run.assert_called_once()
    shutdown.assert_not_called()


def test_main_starts_the_tunnel_before_running_the_server(clean_env):
    calls = []
    fake_tunnel = mock.Mock()

    def fake_setup():
        calls.append("setup")
        return fake_tunnel

    with (
        mock.patch.object(main_module, "setup_ssh_tunnel_if_configured", fake_setup),
        mock.patch.object(main_module, "shutdown_ssh_tunnel", lambda t: calls.append("shutdown")),
        mock.patch.object(main_module.mcp, "run", lambda **kw: calls.append("run")),
        mock.patch.object(main_module, "setup_middleware"),
    ):
        main_module.main()

    assert calls == ["setup", "run", "shutdown"]


def test_main_closes_the_tunnel_when_the_server_raises(clean_env):
    fake_tunnel = mock.Mock()

    with (
        mock.patch.object(
            main_module, "setup_ssh_tunnel_if_configured", return_value=fake_tunnel
        ),
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
    import signal

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ssh_main.py -v`
Expected: FAIL — `AttributeError: <module 'mcp_clickhouse.main'> does not have the attribute 'setup_ssh_tunnel_if_configured'`

- [ ] **Step 3: Rewrite `main.py`**

Replace the whole of `mcp_clickhouse/main.py` with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ssh_main.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS. No upstream test should have changed behaviour — `main()` with no `CLICKHOUSE_SSH_HOST` calls `setup_ssh_tunnel_if_configured()`, gets `None`, and proceeds exactly as before.

- [ ] **Step 6: Smoke-test the no-tunnel path by hand**

```bash
CLICKHOUSE_HOST=localhost CLICKHOUSE_USER=default CLICKHOUSE_SECURE=false \
  timeout 5 uv run mcp-clickhouse < /dev/null; echo "exit=$?"
```
Expected: the server starts and exits on EOF or the timeout (`exit=124` from `timeout` is fine). It must not raise anything mentioning SSH.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add mcp_clickhouse/main.py tests/test_ssh_main.py
git commit -m "feat(ssh): start and stop the tunnel around mcp.run()"
```

---

## Task 8: Opt-in integration test against real `ssh`

**Files:**
- Create: `tests/test_ssh_integration.py`

**Interfaces:**
- Consumes: `start_tunnel`, `try_connect` from Task 3

This test needs a real SSH server that the running user can reach with key auth. It is skipped by default and must never gate CI.

- [ ] **Step 1: Write the test**

Create `tests/test_ssh_integration.py`:

```python
"""End-to-end test against the real ssh binary.

Opt in by pointing CLICKHOUSE_SSH_IT_HOST at an SSH host you can reach with key
auth and CLICKHOUSE_SSH_IT_TARGET at a "host:port" that host can reach. The
simplest local setup is an sshd on localhost with your own key authorised:

    CLICKHOUSE_SSH_IT_HOST=localhost \
    CLICKHOUSE_SSH_IT_TARGET=127.0.0.1:22 \
    uv run pytest tests/test_ssh_integration.py -v
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
```

The banner assertion assumes `CLICKHOUSE_SSH_IT_TARGET` points at an sshd. Document that in the module docstring, which it does.

- [ ] **Step 2: Verify it skips cleanly by default**

Run: `uv run pytest tests/test_ssh_integration.py -v`
Expected: `1 skipped`, with the reason naming both variables.

- [ ] **Step 3: Run it for real if an sshd is available**

```bash
CLICKHOUSE_SSH_IT_HOST=localhost CLICKHOUSE_SSH_IT_TARGET=127.0.0.1:22 \
  uv run pytest tests/test_ssh_integration.py -v
```
Expected: PASS, or a clear skip if there is no local sshd. Do not weaken the test to make it pass in an environment without one.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ssh_integration.py
git commit -m "test(ssh): add opt-in integration test against real ssh"
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the README section**

Add to `README.md`, immediately after the ClickHouse environment-variable section (find it with `grep -n "CLICKHOUSE_SECURE" README.md`):

````markdown
### SSH bastion tunnel

> This is an addition in this fork, not present in upstream `ClickHouse/mcp-clickhouse`.

When ClickHouse is only routable from inside a private network, the server can open
an SSH local port forward at startup and talk to ClickHouse through it. The forward
is torn down when the server exits.

Set `CLICKHOUSE_SSH_HOST` to enable it. With that unset, nothing changes.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CLICKHOUSE_SSH_HOST` | to enable | — | Bastion / jump host. A `~/.ssh/config` `Host` alias works, including one with `ProxyJump` |
| `CLICKHOUSE_SSH_USER` | no | ssh default | Login user on the bastion |
| `CLICKHOUSE_SSH_PORT` | no | `22` | SSH port on the bastion |
| `CLICKHOUSE_SSH_KEY` | no | ssh default | Identity file; `~` is expanded and the path is checked at startup |
| `CLICKHOUSE_SSH_READY_TIMEOUT` | no | `10` | Seconds to wait for the forward to accept connections |

`CLICKHOUSE_HOST` and `CLICKHOUSE_PORT` keep their usual meaning: the ClickHouse
endpoint **as seen from the bastion**. You do not pick or manage a local port —
a free one is chosen at startup.

When `CLICKHOUSE_SECURE=true`, the server also sets `CLICKHOUSE_SERVER_HOST_NAME`
to the real ClickHouse host so TLS still verifies through the forward, unless you
have set that variable yourself.

Example:

```json
{
  "mcpServers": {
    "mcp-clickhouse": {
      "command": "uv",
      "args": ["run", "--with", "mcp-clickhouse", "--python", "3.12", "mcp-clickhouse"],
      "env": {
        "CLICKHOUSE_HOST": "clickhouse.internal",
        "CLICKHOUSE_PORT": "8443",
        "CLICKHOUSE_USER": "reporting_reader",
        "CLICKHOUSE_PASSWORD": "<password>",
        "CLICKHOUSE_DATABASE": "reporting",
        "CLICKHOUSE_SECURE": "true",
        "CLICKHOUSE_SSH_HOST": "bastion.example.com",
        "CLICKHOUSE_SSH_USER": "ec2-user",
        "CLICKHOUSE_SSH_KEY": "~/.ssh/bastion.pem"
      }
    }
  }
}
```

Requirements and limits:

- An OpenSSH client (`ssh`) on `PATH`. macOS and Linux only.
- Key-based auth only — the forward is opened with `BatchMode=yes`, so a key that
  needs an interactive passphrase fails fast rather than hanging. Use `ssh-agent`.
- If the forward dies while the server is running, the server logs `ssh`'s stderr
  and exits so your MCP client can restart it. A silently dead forward would make
  every later query fail against a closed port.
````

- [ ] **Step 2: Add the fork note near the top of the README**

Insert directly under the README's main title:

```markdown
> **Fork.** This is a fork of [ClickHouse/mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse)
> that adds [SSH bastion tunnelling](#ssh-bastion-tunnel) for ClickHouse servers reachable
> only through a jump host. Everything else tracks upstream.
```

- [ ] **Step 3: Add the changelog entry**

Add at the top of `CHANGELOG.md`, under a new heading:

```markdown
## Unreleased (fork)

- Added optional SSH bastion tunnelling. Setting `CLICKHOUSE_SSH_HOST` makes the
  server open an SSH local port forward to `CLICKHOUSE_HOST:CLICKHOUSE_PORT` at
  startup, point the ClickHouse client at it, and close it on shutdown. Also
  reads `CLICKHOUSE_SSH_USER`, `CLICKHOUSE_SSH_PORT`, `CLICKHOUSE_SSH_KEY` and
  `CLICKHOUSE_SSH_READY_TIMEOUT`. When unset, behaviour is unchanged.
```

- [ ] **Step 4: Check the docs against the code**

Run: `grep -o 'CLICKHOUSE_SSH_[A-Z_]*' README.md | sort -u`
Compare against: `grep -o 'CLICKHOUSE_SSH_[A-Z_]*' mcp_clickhouse/ssh/config.py | sort -u`
Expected: identical sets. Any variable documented but not read, or read but not documented, is a bug in one of the two.

- [ ] **Step 5: Lint markdown and commit**

Run: `uv run ruff check . && uv run pytest -q`
Expected: clean, all tests pass.

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document the SSH bastion tunnel"
```

---

## Final verification

- [ ] `uv run pytest -q` — all tests pass
- [ ] `uv run ruff check . && uv run ruff format --check .` — clean
- [ ] `pgrep -f fake-ssh || echo clean` — no leaked test processes
- [ ] `git diff upstream/main --stat -- mcp_clickhouse/` — shows only `main.py` modified plus the new `ssh/` package
- [ ] `git log --oneline upstream/main..HEAD` — one commit per task, readable messages
- [ ] Manual: run the server against a real bastion and call `list_databases`, confirming the log line `SSH tunnel ready on 127.0.0.1:<port>` and a successful query

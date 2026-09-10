# SSH Bastion Tunnel for mcp-clickhouse — Design

**Date:** 2026-09-10
**Status:** Approved, ready for implementation planning

## Goal

Let `mcp-clickhouse` reach a ClickHouse server that is only reachable through an SSH
bastion (jump host). The server opens an SSH local port forward at startup, points the
ClickHouse client at the forwarded local port, and tears the forward down cleanly at
shutdown.

The feature is opt-in. When `CLICKHOUSE_SSH_HOST` is unset, behaviour is byte-identical
to upstream.

## Background

This repository is a fork of [ClickHouse/mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse)
(Python 3.10+, FastMCP, `clickhouse-connect`). The SSH layer is a port of the tunnel
implementation in [bartekbp/mcp-server-mysql](https://github.com/bartekbp/mcp-server-mysql)
(`src/ssh/tunnel.ts`, `src/ssh/startup.ts`), which has been in use against RDS behind a
bastion. Behaviour, option flags and failure semantics are carried over deliberately so
the two servers behave the same way.

The port is kept as an additive, self-contained package so it stays rebasable on
upstream and could be offered upstream later.

## Non-goals

- Pure-Python SSH (paramiko / sshtunnel). The system `ssh` binary is used so that
  `~/.ssh/config` — `Host` aliases, `ProxyJump` chains, `IdentityFile`, `IdentityAgent` —
  ssh-agent, and hardware-backed keys all work without reimplementation.
- Remote port forwards, dynamic (SOCKS) forwards, or multiple simultaneous tunnels.
- Windows support. macOS and Linux only, matching the MySQL server.
- Tunnelling chDB. chDB is an embedded engine and has no network endpoint.

## Architecture

```
mcp_clickhouse/ssh/__init__.py     public exports
mcp_clickhouse/ssh/config.py       SshTunnelConfig, get_ssh_tunnel_config()
mcp_clickhouse/ssh/tunnel.py       pick_free_port, build_ssh_argv, wait_for_tcp_ready,
                                   start_tunnel, ActiveTunnel
mcp_clickhouse/ssh/startup.py      setup_ssh_tunnel_if_configured(), shutdown_ssh_tunnel()
mcp_clickhouse/main.py             MODIFIED — start before mcp.run(), stop after
```

No changes to `mcp_env.py` or `mcp_server.py`.

### Why no changes to the config layer

`ClickHouseConfig.host` and `.port` read `os.environ` on **every** property access rather
than caching at construction time. Rewriting the environment after the tunnel is up is
therefore observed by the cached `get_config()` singleton and by every client the client
cache creates afterwards. This keeps the whole feature to new files plus a small edit to
`main.py`.

## Components

### `SshTunnelConfig` and `get_ssh_tunnel_config()`

A frozen dataclass:

```python
@dataclass(frozen=True)
class SshTunnelConfig:
    ssh_host: str
    remote_host: str
    remote_port: int
    ssh_user: str | None = None
    ssh_port: int | None = None
    ssh_key: str | None = None
    ready_timeout: float = 10.0
```

`get_ssh_tunnel_config()` returns `None` when `CLICKHOUSE_SSH_HOST` is unset or blank.
Otherwise it builds the config from the environment, resolving the remote target from the
ClickHouse settings **as they stand before any rewrite**.

The remote port must reproduce upstream's default rather than assuming a value:
`CLICKHOUSE_PORT` if set, else `8443` when `CLICKHOUSE_SECURE` is true (its own default),
else `8123`. Reusing `ClickHouseConfig.port` for this is preferred over duplicating the
rule.

Validation: `CLICKHOUSE_SSH_PORT` and `CLICKHOUSE_SSH_READY_TIMEOUT`, when present, must
parse and fall in range (1–65535 and > 0 respectively); anything else raises `ValueError`
with the offending value in the message.

### `tunnel.py`

Direct port of `src/ssh/tunnel.ts`.

- `pick_free_port() -> int` — bind `127.0.0.1:0`, read the assigned port, close.
  Inherently racy against another process claiming the port in the gap; `ssh` is spawned
  with `ExitOnForwardFailure=yes` so a lost race surfaces as a clean startup error rather
  than a silent bad forward.
- `build_ssh_argv(cfg, local_port) -> list[str]` — pure, unit-testable:

  ```
  -N -T
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -L <local_port>:<remote_host>:<remote_port>
  [-l <ssh_user>] [-p <ssh_port>] [-i <expanded ssh_key>]
  <ssh_host>
  ```

  `BatchMode=yes` means a key needing an interactive passphrase fails fast instead of
  hanging on a prompt that has nowhere to render under stdio transport.
- `wait_for_tcp_ready(port, timeout, interval)` — poll `connect()` to `127.0.0.1:port`
  every 50 ms until the deadline; raise on timeout.
- `start_tunnel(cfg, *, spawn_path="ssh", on_runtime_exit=None) -> ActiveTunnel`:
  1. If `ssh_key` is set, expand `~` and check the file exists — a clear pre-flight error
     beats an opaque ssh failure.
  2. Pick a free port, spawn `ssh` with stdin closed and stdout/stderr piped.
  3. Stream stderr to the logger line by line, retaining a rolling 500-byte tail.
  4. Race readiness polling against early process exit. Whichever resolves first wins; on
     early exit the error carries the stderr tail, on timeout the error names the port and
     the elapsed budget. Either way the child is killed before the error propagates.
  5. A `spawn_path` that does not exist raises a message telling the user to install an
     OpenSSH client.
- `ActiveTunnel` — holds `local_port` and `close()`. `close()` is idempotent: it detaches
  the runtime-exit watcher first (so a deliberate shutdown is not reported as a crash),
  sends `SIGTERM`, waits a 3 s grace period, then `SIGKILL`.

### `startup.py`

`setup_ssh_tunnel_if_configured()`:

1. `get_ssh_tunnel_config()`; `None` → return `None`, nothing else happens.
2. Log the intent (`bastion -> host:port`) before the blocking call, so a hang is
   attributable.
3. `start_tunnel(...)`.
4. Rewrite the environment:
   - `CLICKHOUSE_HOST = "127.0.0.1"`
   - `CLICKHOUSE_PORT = str(tunnel.local_port)`
   - if the connection is secure **and** `CLICKHOUSE_SERVER_HOST_NAME` is unset or blank,
     set it to the original host.
5. Return the `ActiveTunnel`.

Step 4's third clause is load-bearing. Over a forward the TCP peer is `127.0.0.1`, but the
server's certificate names the real host, so TLS verification fails without an SNI and
verification-name override. `CLICKHOUSE_SERVER_HOST_NAME` is upstream's existing knob for
exactly this. An explicit user value is never clobbered.

`shutdown_ssh_tunnel(tunnel)` closes the ClickHouse client cache first
(`mcp_server._clear_client_cache()`), then the tunnel. Closing in the other order leaves
clients trying to speak to a dead local port during their own teardown. This mirrors the
MySQL server's pool-then-tunnel ordering.

### `main.py` changes

```python
def main():
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport
    setup_middleware(mcp)

    tunnel = setup_ssh_tunnel_if_configured()
    try:
        ...existing mcp.run(...) branches, unchanged...
    finally:
        if tunnel is not None:
            shutdown_ssh_tunnel(tunnel)
```

The tunnel starts **before** `mcp.run()`, never lazily on first query. A lazy tunnel would
let a tool call reach `create_clickhouse_client()` while the forward is still coming up,
creating and caching a client bound to the pre-tunnel host. The MySQL server shipped that
bug and fixed it in commit `31bde4a`; the ordering is fixed here from the start.

`SIGINT`/`SIGTERM` handlers are installed alongside so the `finally` block is reached on
signal-driven shutdown, chaining to any pre-existing handler.

## Failure handling

| Failure | Behaviour |
|---|---|
| `ssh` not on `PATH` | Startup error naming the missing binary and pointing at OpenSSH |
| Key file missing | Pre-flight error naming the configured path |
| Auth rejected / host unreachable | Startup error carrying the ssh stderr tail |
| Forward bound but never ready | Timeout error naming the local port and the budget; child killed |
| `CLICKHOUSE_SSH_PORT` unparseable or out of range | `ValueError` at config read |
| Tunnel dies mid-session | stderr tail logged at error, then the process exits non-zero |

The last row is a deliberate choice carried over from the MySQL server. A dead forward
leaves every later query failing against a closed local port, with cached clients holding
connections that will never recover. Exiting surfaces the problem immediately and lets the
MCP client restart the server. It is injectable via `on_runtime_exit` so tests can observe
it without killing the test runner.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CLICKHOUSE_SSH_HOST` | yes, to enable | — | Bastion. A `~/.ssh/config` `Host` alias works, including `ProxyJump` chains |
| `CLICKHOUSE_SSH_USER` | no | ssh default | `-l` |
| `CLICKHOUSE_SSH_PORT` | no | 22 | `-p` |
| `CLICKHOUSE_SSH_KEY` | no | ssh default | `-i`; `~` expanded, existence checked |
| `CLICKHOUSE_SSH_READY_TIMEOUT` | no | `10` | Seconds to wait for the forward to accept TCP |

The `CLICKHOUSE_SSH_` prefix keeps these from colliding with the bare `SSH_HOST` family
used by the MySQL server when both run from one environment or one `.env` file.

`CLICKHOUSE_HOST` and `CLICKHOUSE_PORT` keep their normal meaning — the ClickHouse
endpoint **as seen from the bastion**. Users do not hand-manage a local port.

## Testing

`pytest`, matching upstream. Unit tests use fake `ssh` binaries — small `sh` scripts that
parse the `-L` argument and simulate one scenario each, passed via `start_tunnel`'s
`spawn_path`. The MySQL server's fixtures drive Node; these use `python3 -c` instead.

Fixtures under `tests/fixtures/ssh/`:

- `fake-ssh-ok.sh` — binds the forwarded local port and idles; exits cleanly on `SIGTERM`
- `fake-ssh-crash.sh` — writes an auth-failure message to stderr and exits 255
- `fake-ssh-hang.sh` — idles without ever binding, exercising the readiness timeout
- `fake-ssh-ignore-sigterm.sh` — binds, then traps and ignores `SIGTERM`, forcing `SIGKILL`

Cases:

- `build_ssh_argv` output for the minimal config and for every optional flag
- `pick_free_port` returns a port that is genuinely bindable
- happy path — `start_tunnel` returns once the port accepts
- early exit — the raised error contains the fixture's stderr text
- readiness timeout — raises within roughly the configured budget, child not left running
- `close()` is graceful, and idempotent when called twice
- `SIGKILL` fallback against the SIGTERM-ignoring fixture
- `on_runtime_exit` fires when the child dies after readiness, and does **not** fire for a
  deliberate `close()`
- missing key path fails pre-flight without spawning
- `get_ssh_tunnel_config` returns `None` when unset/blank; parses each variable; rejects
  bad port and timeout values; derives the remote port from `CLICKHOUSE_SECURE` when
  `CLICKHOUSE_PORT` is absent
- env rewrite sets host and port; sets `CLICKHOUSE_SERVER_HOST_NAME` when secure; leaves an
  explicit `CLICKHOUSE_SERVER_HOST_NAME` alone; does not set it when not secure

Environment mutation is confined by a fixture that snapshots and restores `os.environ`.

One integration test drives the real `ssh` binary against `localhost`, skipped unless an
opt-in variable is set, mirroring the MySQL server's gated test.

## Documentation

- README gains an "SSH bastion tunnel" section: the variables, a worked `.mcp.json`
  example, the `~/.ssh/config` alias trick, and an explicit note that `CLICKHOUSE_HOST`
  stays the address as seen from the bastion.
- `CHANGELOG.md` gains an entry describing the fork's addition.
- README states the fork relationship and links upstream.

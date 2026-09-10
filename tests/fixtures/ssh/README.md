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

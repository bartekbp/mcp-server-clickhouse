#!/usr/bin/env sh
# Stands in for `ssh`: parse -L <bindAddr>:<localPort>:<remoteHost>:<remotePort>, then bind
# localPort so the readiness probe succeeds. Exits on SIGTERM.
LOCAL_PORT=""
next_is_L=""
for arg in "$@"; do
  if [ "$next_is_L" = "1" ]; then
    LOCAL_PORT=$(echo "$arg" | cut -d: -f2)
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

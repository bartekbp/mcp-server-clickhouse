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

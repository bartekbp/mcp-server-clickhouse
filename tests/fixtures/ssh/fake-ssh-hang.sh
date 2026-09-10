#!/usr/bin/env sh
# Stands in for `ssh` that connects but never establishes the forward.
# Exercises the readiness timeout.
exec python3 -c "
import time
while True:
    time.sleep(60)
"

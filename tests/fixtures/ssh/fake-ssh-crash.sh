#!/usr/bin/env sh
# Stands in for `ssh` refusing the connection. Exits before binding anything.
echo "Permission denied (publickey)." >&2
echo "fake-ssh-crash: simulated auth failure" >&2
exit 255

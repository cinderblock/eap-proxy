#!/bin/sh
# Run eap_proxy's tests on Linux (on Windows: wsl ./run-tests.sh). The
# integration tests need a network namespace; `unshare` makes a throwaway one
# as an unprivileged user, so nothing touches the host's interfaces. A mount
# namespace with a fresh sysfs lets /sys/class/net show that namespace's
# interfaces, as it does on the gateway.
set -eu
cd "$(dirname "$0")"
exec unshare --map-root-user --net --mount sh -c '
	mount -t sysfs sysfs /sys &&
	exec env EAP_PROXY_NETNS=1 "$0" -m unittest -v test_eap_proxy "$@"
' "${PYTHON:-python3}" "$@"

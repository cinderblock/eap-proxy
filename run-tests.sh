#!/bin/sh
# Run eap_proxy's tests on Linux (on Windows: wsl ./run-tests.sh). The
# integration tests need a network namespace; `unshare -rn` makes a throwaway
# one as an unprivileged user, so nothing touches the host's interfaces.
set -eu
cd "$(dirname "$0")"
exec unshare --map-root-user --net env EAP_PROXY_NETNS=1 \
	"${PYTHON:-python3}" -m unittest -v test_eap_proxy "$@"

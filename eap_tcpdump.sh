#!/bin/sh
# Use tcpdump to debug EAP traffic
set -x
interface=${1:?usage: eap_tcpdump.sh INTERFACE}
tcpdump -xx -e -n -vvv -i $interface ether proto 0x888e

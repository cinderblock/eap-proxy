# eap_proxy for UniFi OS gateways

AT&T fiber authenticates the line with 802.1X (EAP-TLS), and only AT&T's own gateway has the certificate. `eap_proxy`
lets your UniFi gateway take AT&T's fiber directly: it relays the 802.1X frames between the fiber and the AT&T gateway,
which stays plugged into a spare port only to log in. Everything else (the address, the routing, the firewall) is your
UniFi gateway's.

This is v2, rewritten for UniFi OS gateways (we run it on UXG-Pros, UniFi OS 6, Debian 13, Python 3.13). It started as a
fork of [jaysoffian/eap_proxy](https://github.com/jaysoffian/eap_proxy), which is archived. v1 (tag `v1.1.0`) keeps
upstream's EdgeOS support; use that, or upstream, on an EdgeRouter or USG.

## What it does

- Relays EAPOL between `WAN_PORT` (the fiber or ONT) and `ROUTER_PORT` (the AT&T gateway's WAN port).
- Frames from AT&T always reach the AT&T gateway. Frames from the AT&T gateway are **dropped while your WAN is up**, so a
  reboot or hiccup of the AT&T gateway can't disturb a working session, and an **EAPOL-Logoff is never relayed**.
- Finds the WAN itself: the default route, in any of UniFi's per-WAN routing tables, whose interface is `WAN_PORT` or a
  VLAN on it (`eth2`, `eth2.2`, …). The WAN is up when that interface has an address and its gateway answers a ping. The
  VLAN is set once, in UniFi.
- Warns loudly, and keeps relaying, when the setup is wrong:
  - `ROUTER_PORT` is in a bridge, so the AT&T gateway sits on one of your LANs.
  - The WAN's MAC address isn't the AT&T gateway's (learned from its EAPOL frames), so the MAC clone is missing.
  - No default route on `WAN_PORT` or its VLANs.
- Runs under systemd: `Type=notify`, a watchdog (a hung proxy is restarted, not just a dead one), logs with priorities
  to the journal, and a status file.

## Setting up the UniFi side

1. **`WAN_PORT`: a UniFi WAN**, DHCP, with **MAC address clone** set to the AT&T gateway's WAN MAC (on its label). AT&T
   ties the session and the DHCP lease to that MAC.
2. **The VLAN, in that WAN's settings**, depends on the AT&T service:
   - **Active Ethernet ("iPAG")**, fiber straight into an SFP, no ONT: 802.1X is untagged but **data is on VLAN 2**.
     Without it, 802.1X succeeds and DHCP gets no answer.
   - **GPON with an ONT**: data is untagged or priority-tagged (VLAN 0).
   - Don't copy the AT&T gateway's own framing: we saw a BGW320 send DHCP as VLAN 0 on an iPAG line, where only VLAN 2
     got a reply.
3. **`ROUTER_PORT`: a WAN as well**, never a LAN port. UniFi OS puts every LAN-role port into every LAN bridge,
   whatever its native network, even when the port shows as "disabled". Make it a third WAN with a static address in an
   unused /30, a gateway nothing answers, IPv6 off, failover-only.
4. Some AT&T gateways (the BGW320) pick their WAN port at boot. With its SFP moved to your gateway, power-cycle it once
   so it uses its Ethernet WAN port.

## Install

SSH to the gateway as root. Files in `/data` survive firmware updates; check the unit after one
(`systemctl is-enabled eap_proxy`).

```sh
mkdir -p /data/eap_proxy
# copy eap_proxy.py and LICENSE to /data/eap_proxy/, and eap_proxy.service
# (with your two ports) to /etc/systemd/system/
python3 -I /data/eap_proxy/eap_proxy.py --help
systemctl daemon-reload
systemctl enable --now eap_proxy
journalctl -u eap_proxy -n 20 --no-pager
```

`eap_proxy.service` in this repo is the unit to start from. Edit `ExecStart`'s two ports.

## Running it

```
python3 -I eap_proxy.py [options] WAN_PORT ROUTER_PORT
```

| Option                     | Default                       | Meaning                                                      |
| -------------------------- | ----------------------------- | ------------------------------------------------------------ |
| `--ping-ip IP`             | the WAN's gateway             | what to ping to decide the WAN is up                         |
| `--relay-always`           | off                           | relay the AT&T gateway's frames even while the WAN is up     |
| `--relay-logoff`           | off                           | relay EAPOL-Logoff (it ends AT&T's session)                  |
| `--check-interval SECONDS` | 30                            | how often to recheck the WAN, the bridge and the MAC         |
| `--status-file PATH`       | `/run/eap_proxy/status.json`  | status JSON; empty to skip                                   |
| `--promiscuous`            | off                           | promiscuous mode instead of joining the EAPOL group address |
| `--debug`, `--debug-packets` | off                         | more logging; `--debug-packets` adds each frame in hex       |

v1's `--vlan-id`, `--ping-gateway`, `--ignore-*`, `--set-mac`, `--restart-dhcp`, `--daemon`, `--pidfile`, `--syslog`
and `--run-as` are gone; using one prints what replaced it.

**An empty log is the healthy state** apart from the WAN state at startup: the AT&T gateway only talks while the link
is coming up, and its frames are dropped while the WAN is up.

### Status file

`/run/eap_proxy/status.json` is rewritten whenever something changes:

```json
{
 "version": "2.0.0",
 "wan": {"up": true, "dev": "eth2.2", "address": "203.0.113.10", "gateway": "203.0.113.1", "table": "201.eth2.2"},
 "router_mac": "02:11:11:11:11:11",
 "last_relay": {"at": 1794700000.0, "from": "eth2", "to": "eth1", "frame": "EAP packet Success id 7"},
 "last_success": 1794700000.0,
 "warnings": []
}
```

(Trimmed: it also has `pid`, `started`, both port names and `wan.checked`.) Times are Unix seconds.

## Tests

`./run-tests.sh` on Linux (on Windows: `wsl ./run-tests.sh`). It runs the real proxy between veth pairs in a throwaway
network and mount namespace, as an unprivileged user: WAN on a VLAN, routes only in a per-WAN table, a bridged router
port, a missing MAC clone, the systemd watchdog, the status file, and SIGTERM under a flood of frames.

## License

BSD-3-Clause, see `LICENSE`. Originally by Jay Soffian.

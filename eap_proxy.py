#!/usr/bin/env python3
"""Relay AT&T's 802.1X (EAPOL) between the fiber and the AT&T gateway, on a
UniFi OS gateway (UXG).

usage: eap_proxy.py [options] WAN_PORT ROUTER_PORT

WAN_PORT is the port with AT&T's fiber or ONT (the UniFi WAN). ROUTER_PORT is
the port cabled to the AT&T gateway's WAN port. Everything else is read from
the system: see README.md.
"""
# Copyright (c) 2019, Jay Soffian. Copyright (c) 2026, Cameron Tacklind.
# BSD-3-Clause: see LICENSE next to this file.
#
# This is https://github.com/cinderblock/eap-proxy v2, rewritten for UniFi OS
# from jaysoffian/eap_proxy (archived 2026-01-06). v1 (the tag v1.1.0) keeps
# upstream's EdgeOS paths and --vlan-id.
import argparse
import array
import ctypes
import json
import logging
import os
import random
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from fcntl import ioctl

__version__ = "2.0.0"

### Constants

EAP_MULTICAST_ADDR = (0x01, 0x80, 0xC2, 0x00, 0x00, 0x03)
ETH_P_PAE = 0x888E  # IEEE 802.1X (Port Access Entity)
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_MULTICAST = 0
PACKET_MR_PROMISC = 1
PACKET_AUXDATA = 8
PACKET_OUTGOING = 4
TP_STATUS_VLAN_VALID = 0x10
SIOCGIFADDR = 0x8915
SOL_PACKET = 263
SYS_NET = "/sys/class/net"
DEFAULT_STATUS_FILE = "/run/eap_proxy/status.json"

# Flags from v1 that v2 no longer has, with what replaces them.
REMOVED_FLAGS = {
    "--vlan-id": "the WAN is found from the routing tables; set the VLAN in UniFi only",
    "--ping-gateway": "the WAN's gateway is always pinged",
    "--ignore-when-wan-up": "this is the default (see --relay-always)",
    "--ignore-logoff": "this is the default (see --relay-logoff)",
    "--ignore-start": "not supported",
    "--set-mac": "use the UniFi WAN's MAC address clone",
    "--restart-dhcp": "UniFi runs its own DHCP client",
    "--daemon": "run it under systemd",
    "--pidfile": "run it under systemd",
    "--syslog": "it logs to stderr, which systemd sends to the journal",
    "--run-as": "not supported: it needs CAP_NET_RAW for its sockets and pings",
}

### Network interfaces


class struct_packet_mreq(ctypes.Structure):
    # pylint:disable=too-few-public-methods,invalid-name
    _fields_ = (
        ("mr_ifindex", ctypes.c_int),
        ("mr_type", ctypes.c_ushort),
        ("mr_alen", ctypes.c_ushort),
        ("mr_address", ctypes.c_ubyte * 8),
    )


def rawsocket(ifname, promisc=False):
    """Return a packet socket that receives only 802.1X frames on `ifname`.

    Binding with ETH_P_PAE makes the kernel deliver nothing else. The socket
    joins the EAPOL group address (or goes promiscuous), and reports stripped
    VLAN tags through PACKET_AUXDATA.
    """
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_PAE))
    s.bind((ifname, 0))
    mreq = struct_packet_mreq()
    mreq.mr_ifindex = socket.if_nametoindex(ifname)
    if promisc:
        mreq.mr_type = PACKET_MR_PROMISC
    else:
        mreq.mr_type = PACKET_MR_MULTICAST
        mreq.mr_alen = len(EAP_MULTICAST_ADDR)
        mreq.mr_address = (ctypes.c_ubyte * 8)(*EAP_MULTICAST_ADDR)
    s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, bytes(mreq))
    s.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
    return s


def recv_frame(sock):
    """Return (frame, vlan) for the next frame `sock` received, or (None,
    None) for one this host sent. `vlan` is the tag the NIC stripped, if any.
    """
    frame, ancdata, __, addr = sock.recvmsg(4096, socket.CMSG_SPACE(32))
    if addr[2] == PACKET_OUTGOING:
        return None, None
    vlan = None
    for level, kind, data in ancdata:
        if level == SOL_PACKET and kind == PACKET_AUXDATA and len(data) >= 20:
            status, __, __, __, __, tci, __ = struct.unpack("IIIHHHH", data[:20])
            if status & TP_STATUS_VLAN_VALID:
                vlan = tci & 0xFFF
    return frame, vlan


def interface_exists(ifname):
    try:
        socket.if_nametoindex(ifname)
    except OSError:
        return False
    return True


def getifaddr(ifname):
    """Return the IPv4 address of `ifname`, or None."""
    ifreq = struct.pack("256s", ifname.encode()[:15])
    with socket.socket() as s:
        try:
            result = ioctl(s, SIOCGIFADDR, ifreq)
        except OSError:
            return None
    return socket.inet_ntoa(result[20:24])


def getifhwaddr(ifname):
    """Return the MAC address of `ifname` as aa:bb:cc:dd:ee:ff."""
    with open(os.path.join(SYS_NET, ifname, "address")) as f:
        return f.read().strip().lower()


def bridge_of(ifname):
    """Return the name of the bridge `ifname` is a port of, or None."""
    master = os.path.join(SYS_NET, ifname, "master")
    if not os.path.exists(master):
        return None
    return os.path.basename(os.readlink(master))


def wan_devices(wan_port):
    """Return `wan_port` and every interface stacked on it (its VLANs)."""
    devices = {wan_port}
    for name in os.listdir(SYS_NET):
        if os.path.exists(os.path.join(SYS_NET, name, "lower_" + wan_port)):
            devices.add(name)
    return devices


Wan = namedtuple("Wan", "dev gateway table")


def pick_wan_route(routes, devices):
    """From `ip -j route` output `routes`, return the lowest-metric default
    route (a Wan) whose device is in `devices`, or None.
    """
    best, best_metric = None, None
    for route in routes:
        if route.get("dst") != "default" or route.get("dev") not in devices:
            continue
        if "gateway" not in route or route.get("type", "unicast") != "unicast":
            continue
        metric = route.get("metric", 0)
        if best_metric is None or metric < best_metric:
            best = Wan(route["dev"], route["gateway"], route.get("table", "main"))
            best_metric = metric
    return best


def default_routes():
    """Return every IPv4 default route in every table, as `ip -j` parsed it."""
    out = subprocess.run(
        ["ip", "-j", "-4", "route", "show", "table", "all", "default"],
        check=True, stdout=subprocess.PIPE,
    )  # fmt: skip
    return json.loads(out.stdout or b"[]")


### Ping


def ipchecksum(packet):
    """Return the RFC 1071 checksum of `packet`."""
    arr = array.array("H", packet + b"\0" if len(packet) % 2 else packet)
    chksum = sum(arr)
    chksum = (chksum >> 16) + (chksum & 0xFFFF)
    chksum += chksum >> 16
    return socket.htons(~chksum & 0xFFFF)


def pingaddr(ipaddr, timeout=1.0):
    """Return True if `ipaddr` answers an ICMP echo within `timeout` seconds."""
    icmp = struct.Struct("!BBHHH")
    echoid, seq = os.getpid() & 0xFFFF, random.randint(0, 0xFFFF)
    chksum = ipchecksum(icmp.pack(8, 0, 0, echoid, seq))
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as s:
        s.sendto(icmp.pack(8, 0, chksum, echoid, seq), (ipaddr, 0))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([s], [], [], left)[0]:
                return False
            packet, peer = s.recvfrom(2048)
            if peer[0] != ipaddr:
                continue
            ihl = (packet[0] & 0x0F) * 4
            kind, __, __, rid, rseq = icmp.unpack(packet[ihl : ihl + 8])
            if kind == 0 and (rid, rseq) == (echoid, seq):
                return True


### EAPOL frames
# c.f. https://github.com/the-tcpdump-group/tcpdump/blob/master/print-eap.c


def strmac(mac):
    return ":".join("%02x" % b for b in mac[:6])


class EAPFrame(namedtuple("EAPFrame", "dst src version type length packet")):
    __slots__ = ()
    _struct = struct.Struct("!6s6sHBBH")  # includes the ethernet header
    TYPE_PACKET, TYPE_START, TYPE_LOGOFF = 0, 1, 2
    _types = {0: "EAP packet", 1: "EAPOL start", 2: "EAPOL logoff", 3: "EAPOL key"}

    @classmethod
    def from_buf(cls, buf):
        dst, src, etype, ver, ptype, length = cls._struct.unpack_from(buf)
        if etype != ETH_P_PAE:
            raise ValueError("not an 802.1X frame: ethertype 0x%04x" % etype)
        size = cls._struct.size
        packet = None
        if ptype == cls.TYPE_PACKET:
            packet = EAPPacket.from_buf(buf[size : size + length])
        return cls(dst, src, ver, ptype, length, packet)

    @property
    def is_logoff(self):
        return self.type == self.TYPE_LOGOFF

    @property
    def is_success(self):
        return bool(self.packet and self.packet.code == EAPPacket.SUCCESS)

    def describe(self):
        text = self._types.get(self.type, "type %d" % self.type)
        if self.packet:
            text += " " + self.packet.describe()
        return text

    def __str__(self):
        return "%s > %s, %s" % (strmac(self.src), strmac(self.dst), self.describe())


class EAPPacket(namedtuple("EAPPacket", "code id length")):
    __slots__ = ()
    REQUEST, RESPONSE, SUCCESS, FAILURE = 1, 2, 3, 4
    _codes = {1: "Request", 2: "Response", 3: "Success", 4: "Failure"}

    @classmethod
    def from_buf(cls, buf):
        return cls(*struct.unpack_from("!BBH", buf))

    def describe(self):
        return "%s id %d" % (self._codes.get(self.code, "code %d" % self.code), self.id)


### systemd


class Systemd:
    """sd_notify(3) over $NOTIFY_SOCKET, without libsystemd."""

    def __init__(self):
        self.addr = os.environ.get("NOTIFY_SOCKET")
        if self.addr and self.addr.startswith("@"):
            self.addr = "\0" + self.addr[1:]
        usec = int(os.environ.get("WATCHDOG_USEC", "0") or 0)
        pid = os.environ.get("WATCHDOG_PID")
        if pid and int(pid) != os.getpid():
            usec = 0
        # Ping at half the timeout, as sd_watchdog_enabled(3) advises.
        self.watchdog_interval = usec / 2e6 if usec else None
        self.last_watchdog = 0.0

    def notify(self, message):
        if not self.addr:
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode(), self.addr)

    def watchdog(self):
        now = time.monotonic()
        if self.watchdog_interval and now - self.last_watchdog >= self.watchdog_interval:
            self.notify("WATCHDOG=1")
            self.last_watchdog = now


### Logging


class JournalFormatter(logging.Formatter):
    """Prefix each line with its syslog priority (<3> and so on), which
    journald reads from a service's stderr. Without a journal, add the time.
    """

    PRIORITY = {"DEBUG": 7, "INFO": 6, "WARNING": 4, "ERROR": 3, "CRITICAL": 2}

    def __init__(self, journal):
        super().__init__("%(message)s" if journal else "[%(asctime)s] %(message)s")
        self.journal = journal

    def format(self, record):
        line = super().format(record)
        if self.journal:
            return "<%d>%s" % (self.PRIORITY.get(record.levelname, 6), line)
        return line


def make_logger(debug):
    handler = logging.StreamHandler()
    handler.setFormatter(JournalFormatter("JOURNAL_STREAM" in os.environ))
    log = logging.getLogger("eap_proxy")
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    log.addHandler(handler)
    return log


### The proxy


class EAPProxy:
    # pylint:disable=too-many-instance-attributes

    def __init__(self, args, log, systemd):
        self.args, self.log, self.systemd = args, log, systemd
        self.s_wan = rawsocket(args.wan_port, args.promiscuous)
        self.s_rtr = rawsocket(args.router_port, args.promiscuous)
        self.started = time.time()
        # "up": None until the first check, so that check is always logged.
        self.wan = {"up": None, "dev": None, "address": None, "gateway": None,
                    "table": None, "checked": None}  # fmt: skip
        self.router_mac = None
        self.warnings = {}
        self.last_relay = None
        self.last_success = None
        self.status_dirty = True

    ## WAN state

    def check_wan(self):
        """Work out whether the WAN is up, log any change, return it."""
        args = self.args
        route = pick_wan_route(default_routes(), wan_devices(args.wan_port))
        state = {"up": False, "dev": None, "address": None, "gateway": None,
                 "table": None}  # fmt: skip
        if route is None:
            reason = "no default route on %s or a VLAN on it" % args.wan_port
        else:
            state.update(dev=route.dev, gateway=route.gateway, table=route.table)
            state["address"] = getifaddr(route.dev)
            target = args.ping_ip or route.gateway
            if not state["address"]:
                reason = "%s has no IPv4 address" % route.dev
            else:
                try:
                    answered = pingaddr(target)
                except OSError as ex:
                    answered, reason = False, "can't ping %s: %s" % (target, ex.strerror)
                else:
                    reason = None if answered else "%s doesn't answer a ping" % target
                state["up"] = answered
        self.set_warning(
            "no-wan-route",
            "no default route on %s or a VLAN on it: is the UniFi WAN on this "
            "port, and is the VLAN right?" % args.wan_port
            if route is None else None,
        )  # fmt: skip
        changed = any(self.wan[k] != v for k, v in state.items())
        self.wan.update(state, checked=time.time())
        if changed:
            if state["up"]:
                self.log.info(
                    "WAN up: %s %s via %s (table %s)",
                    state["dev"], state["address"], state["gateway"], state["table"],
                )  # fmt: skip
            else:
                self.log.warning("WAN down: %s", reason)
            self.status_dirty = True
            self.check_mac()
        return state["up"]

    ## Configuration checks

    def set_warning(self, key, message):
        """Record (or clear, with None) a configuration warning; log changes."""
        if self.warnings.get(key) == message:
            return
        if message:
            self.log.error("%s", message)
        elif key in self.warnings:
            self.log.info("resolved: %s", self.warnings[key])
        if message:
            self.warnings[key] = message
        else:
            self.warnings.pop(key, None)
        self.status_dirty = True

    def check_bridge(self):
        bridge = bridge_of(self.args.router_port)
        self.set_warning(
            "router-port-bridged",
            "%s is in bridge %s, so the AT&T gateway is on that LAN: make this "
            "port a UniFi WAN (UniFi OS bridges every LAN-role port)"
            % (self.args.router_port, bridge) if bridge else None,
        )  # fmt: skip

    def check_mac(self):
        if not self.router_mac:
            return
        dev = self.wan["dev"] or self.args.wan_port
        wan_mac = getifhwaddr(dev)
        self.set_warning(
            "mac-mismatch",
            "%s has MAC %s but the AT&T gateway's is %s: set the UniFi WAN's MAC "
            "address clone to %s" % (dev, wan_mac, self.router_mac, self.router_mac)
            if wan_mac != self.router_mac else None,
        )  # fmt: skip

    def periodic_checks(self):
        self.check_bridge()
        self.check_wan()
        self.check_mac()

    ## Status

    def write_status(self):
        if not self.status_dirty or not self.args.status_file:
            return
        status = {
            "version": __version__,
            "pid": os.getpid(),
            "started": self.started,
            "wan_port": self.args.wan_port,
            "router_port": self.args.router_port,
            "wan": self.wan,
            "router_mac": self.router_mac,
            "last_relay": self.last_relay,
            "last_success": self.last_success,
            "warnings": sorted(self.warnings.values()),
        }
        path = self.args.status_file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".status")
        with os.fdopen(fd, "w") as f:
            json.dump(status, f, indent=1)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        self.status_dirty = False
        self.systemd.notify(
            "STATUS=WAN %s%s" % (
                "up on %s" % self.wan["dev"] if self.wan["up"] else "down",
                ", %d warning(s)" % len(self.warnings) if self.warnings else "",
            )
        )  # fmt: skip

    ## Relay

    def on_frame(self, sock_in):
        log, args = self.log, self.args
        buf, vlan = recv_frame(sock_in)
        if buf is None:
            return
        from_router = sock_in is self.s_rtr
        ifname = args.router_port if from_router else args.wan_port
        if args.debug_packets:
            log.debug("%s: %d bytes: %s", ifname, len(buf), buf.hex())
        try:
            eap = EAPFrame.from_buf(buf)
        except (ValueError, struct.error) as ex:
            log.warning("%s: unreadable frame (%s)", ifname, ex)
            return
        if vlan is not None:
            log.warning("%s: EAPOL arrived tagged VLAN %d; relaying it untagged", ifname, vlan)

        if from_router:
            sock_out, out_name = self.s_wan, args.wan_port
            mac = strmac(eap.src)
            if mac != self.router_mac:
                self.router_mac = mac
                self.status_dirty = True
                self.check_mac()
            if eap.is_logoff and not args.relay_logoff:
                log.info("%s: %s: dropped (logoff)", ifname, eap)
                return
            if not args.relay_always and self.check_wan():
                log.info("%s: %s: dropped (WAN is up)", ifname, eap)
                return
        else:
            sock_out, out_name = self.s_rtr, args.router_port
            if eap.is_success:
                self.last_success = time.time()

        sock_out.send(buf)
        log.info("%s: %s > %s", ifname, eap, out_name)
        self.last_relay = {"at": time.time(), "from": ifname, "to": out_name,
                           "frame": eap.describe()}  # fmt: skip
        self.status_dirty = True

    def run(self):
        args, systemd = self.args, self.systemd
        poll = select.poll()
        for s in (self.s_wan, self.s_rtr):
            poll.register(s, select.POLLIN)
        socks = {s.fileno(): s for s in (self.s_wan, self.s_rtr)}
        self.log.info(
            "eap_proxy %s: WAN port %s, AT&T gateway port %s",
            __version__, args.wan_port, args.router_port,
        )  # fmt: skip
        self.periodic_checks()
        self.write_status()
        systemd.notify("READY=1")
        next_check = time.monotonic() + args.check_interval
        while True:
            timeout = next_check - time.monotonic()
            if systemd.watchdog_interval:
                timeout = min(timeout, systemd.watchdog_interval)
            for fd, event in poll.poll(max(0, int(timeout * 1000))):
                if event & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    raise OSError("poll event %#x on fd %d" % (event, fd))
                self.on_frame(socks[fd])
            if time.monotonic() >= next_check:
                self.periodic_checks()
                next_check = time.monotonic() + args.check_interval
            self.write_status()
            systemd.watchdog()


### Main


class RemovedFlag(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error("%s was removed in v2: %s" % (option_string, REMOVED_FLAGS[option_string]))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        "eap_proxy",
        description="Relay AT&T's 802.1X between the fiber and the AT&T gateway "
        "on a UniFi OS gateway.",
    )
    p.add_argument("wan_port", metavar="WAN_PORT",
                   help="port with AT&T's fiber or ONT (the UniFi WAN), e.g. eth2")
    p.add_argument("router_port", metavar="ROUTER_PORT",
                   help="port cabled to the AT&T gateway's WAN port, e.g. eth1")
    p.add_argument("--ping-ip", metavar="IP",
                   help="ping IP to decide whether the WAN is up, instead of the "
                   "WAN's gateway")
    p.add_argument("--relay-always", action="store_true",
                   help="relay the AT&T gateway's frames even while the WAN is up")
    p.add_argument("--relay-logoff", action="store_true",
                   help="relay EAPOL-Logoff from the AT&T gateway (it ends the session)")
    p.add_argument("--check-interval", type=float, default=30, metavar="SECONDS",
                   help="how often to check the WAN and the ports (default 30)")
    p.add_argument("--status-file", default=DEFAULT_STATUS_FILE, metavar="PATH",
                   help="where to write the status JSON (default %(default)s); "
                   "empty to skip")
    p.add_argument("--promiscuous", action="store_true",
                   help="put the ports in promiscuous mode instead of joining the "
                   "EAPOL group address")
    p.add_argument("--debug", action="store_true", help="log at debug level")
    p.add_argument("--debug-packets", action="store_true",
                   help="also log each frame in hex; implies --debug")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    for flag in REMOVED_FLAGS:
        p.add_argument(flag, nargs="?", action=RemovedFlag, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.debug_packets:
        args.debug = True
    if args.wan_port == args.router_port:
        p.error("WAN_PORT and ROUTER_PORT must differ")
    for ifname in (args.wan_port, args.router_port):
        if not interface_exists(ifname):
            p.error("no such interface: %s" % ifname)
    return args


def main(argv=None):
    args = parse_args(argv)
    log = make_logger(args.debug)

    # Don't log in the handler: a SIGTERM that lands during another log write
    # makes that handler's write fail as reentrant, logging swallows the error,
    # and the exit is lost with it. SystemExit gets through logging.
    def on_sigterm(signum, frame):  # pylint:disable=unused-argument
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_sigterm)
    systemd = Systemd()
    try:
        EAPProxy(args, log, systemd).run()
    except SystemExit:
        systemd.notify("STOPPING=1")
        log.info("exiting")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())

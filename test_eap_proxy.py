"""Tests for eap_proxy.py. Linux only (it imports fcntl).

Run with ./run-tests.sh, which puts the integration tests in a throwaway
network namespace.
"""

import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import eap_proxy  # noqa: E402

ETH_P_PAE = 0x888E
EAP_GROUP = bytes.fromhex("0180c2000003")
RTR_MAC = bytes.fromhex("021111111111")  # the AT&T gateway's WAN MAC (made up)
NID_MAC = bytes.fromhex("00255c000001")

# Live output of `ip -4 route show table all default dev eth1` on a
# UXG-Pro, 2026-11-14.
UXG_ROUTE = b"default via 203.0.113.1 table 201.eth1 proto dhcp metric 200\n"


def eapol(src, dst, ptype, body=b""):
    frame = dst + src + struct.pack("!HBBH", ETH_P_PAE, 2, ptype, len(body)) + body
    return frame.ljust(60, b"\0")


def eap_start(src=RTR_MAC):
    return eapol(src, EAP_GROUP, 1)


def eap_logoff(src=RTR_MAC):
    return eapol(src, EAP_GROUP, 2)


def eap_request_identity(dst=RTR_MAC, ident=7):
    # EAP-Request (1), id, length 5, type Identity (1)
    return eapol(NID_MAC, dst, 0, struct.pack("!BBHB", 1, ident, 5, 1))


class ParseIpRouteGateway(unittest.TestCase):
    def test_uxg_output(self):
        self.assertEqual(eap_proxy.parse_ip_route_gateway(UXG_ROUTE), "203.0.113.1")

    def test_str_input(self):
        self.assertEqual(
            eap_proxy.parse_ip_route_gateway(UXG_ROUTE.decode()), "203.0.113.1"
        )

    def test_lowest_metric_wins(self):
        out = (
            "default via 10.0.0.1 table 201.eth2 proto dhcp metric 300\n"
            "default via 10.0.0.9 table 202.eth2 proto dhcp metric 100\n"
            "default via 10.0.0.5 table 203.eth2 proto dhcp metric 200\n"
        )
        self.assertEqual(eap_proxy.parse_ip_route_gateway(out), "10.0.0.9")

    def test_no_metric_counts_as_zero(self):
        out = "default via 10.0.0.1 metric 5\ndefault via 10.0.0.2\n"
        self.assertEqual(eap_proxy.parse_ip_route_gateway(out), "10.0.0.2")

    def test_no_via(self):
        self.assertIsNone(eap_proxy.parse_ip_route_gateway(b""))
        self.assertIsNone(
            eap_proxy.parse_ip_route_gateway(b"default dev ppp0 scope link\n")
        )


class VlanArgs(unittest.TestCase):
    def test_vlan_id_arg(self):
        self.assertIsNone(eap_proxy.vlan_id_arg("none"))
        self.assertIsNone(eap_proxy.vlan_id_arg("NONE"))
        self.assertEqual(eap_proxy.vlan_id_arg("0"), 0)
        self.assertEqual(eap_proxy.vlan_id_arg("7"), 7)
        with self.assertRaises(ValueError):
            eap_proxy.vlan_id_arg("eth2")

    def test_wan_ifname(self):
        args = SimpleNamespace(if_wan="eth2", vlan_id=None)
        self.assertEqual(eap_proxy.wan_ifname(args), "eth2")
        args.vlan_id = 0
        self.assertEqual(eap_proxy.wan_ifname(args), "eth2.0")


class Python3Bits(unittest.TestCase):
    def test_ipchecksum_even_and_odd(self):
        for payload in (b"", b"x", b"abc", b"abcd"):
            header = struct.pack("!BBHHH", 8, 0, 0, 0x1234, 0x5678)
            chksum = eap_proxy.ipchecksum(header + payload)
            packed = struct.pack("!BBHHH", 8, 0, chksum, 0x1234, 0x5678)
            # A packet that carries its own checksum sums to zero.
            self.assertEqual(eap_proxy.ipchecksum(packed + payload), 0, payload)

    def test_getifhwaddr_returns_bytes(self):
        mac = eap_proxy.getifhwaddr("lo")
        self.assertIsInstance(mac, bytes)
        self.assertEqual(len(mac), 6)

    def test_eap_frame_parses(self):
        frame = eap_proxy.EAPFrame.from_buf(eap_request_identity())
        self.assertEqual(frame.type, 0)
        self.assertEqual(frame.packet.code, 1)
        self.assertEqual(eap_proxy.strmac(frame.src), "00:25:5c:00:00:01")
        str(frame)  # formats without raising
        self.assertTrue(eap_proxy.EAPFrame.from_buf(eap_start()).is_start)
        self.assertTrue(eap_proxy.EAPFrame.from_buf(eap_logoff()).is_logoff)


def run(*args):
    subprocess.run(args, check=True)


def proxy_cmd(*extra):
    return [sys.executable, "-I", os.path.join(HERE, "eap_proxy.py")] + list(extra)


@unittest.skipUnless(
    os.environ.get("EAP_PROXY_NETNS") == "1",
    "integration tests run only inside run-tests.sh's network namespace",
)
class Integration(unittest.TestCase):
    """The real proxy between two veth pairs, laid out like the UXG:

    wan0 (eth2, AT&T fiber) <-> nid0 (AT&T's side)
    rtr0 (eth1, BGW link)   <-> bgw0 (the BGW320's WAN port)

    The WAN gateway, like UniFi OS, is only in table 201, not in main.
    """

    @classmethod
    def setUpClass(cls):
        run("ip", "link", "set", "lo", "up")
        for a, b in (("wan0", "nid0"), ("rtr0", "bgw0")):
            run("ip", "link", "add", a, "type", "veth", "peer", "name", b)
            run("ip", "link", "set", a, "up")
            run("ip", "link", "set", b, "up")
        # The "gateway" is a local address, so pings to it get answered.
        run("ip", "addr", "add", "192.0.2.1/32", "dev", "lo")
        # Keep table 201 in existence between tests: `ip route show table 201`
        # fails on a table with no routes at all.
        run("ip", "route", "add", "unreachable", "203.0.113.0/24", "table", "201")

    def setUp(self):
        self.lines = []
        self.proc = None

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc.stderr.close()
        run("ip", "addr", "flush", "dev", "wan0")
        if subprocess.check_output(["ip", "route", "show", "table", "201", "default"]):
            run("ip", "route", "del", "default", "table", "201")

    def wan_up(self):
        run("ip", "addr", "add", "192.0.2.10/24", "dev", "wan0")
        run(
            "ip", "route", "add", "default", "via", "192.0.2.1", "dev", "wan0",
            "table", "201", "metric", "200",
        )  # fmt: skip

    def start(self, *extra):
        self.proc = subprocess.Popen(
            proxy_cmd(*extra), stderr=subprocess.PIPE, text=True
        )
        threading.Thread(target=self._read, daemon=True).start()
        self.wait_for("starting proxy_loop")

    def _read(self):
        for line in self.proc.stderr:
            self.lines.append(line.rstrip())

    def wait_for(self, text, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(text in l for l in self.lines):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        self.fail("no %r in proxy output:\n%s" % (text, "\n".join(self.lines)))

    def sock(self, ifname):
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_PAE))
        s.bind((ifname, 0))
        s.settimeout(1.5)
        self.addCleanup(s.close)
        return s

    def assert_relayed(self, send_on, recv_on, frame):
        rx = self.sock(recv_on)
        self.sock(send_on).send(frame)
        self.assertEqual(rx.recv(2048)[:60], frame)

    def assert_not_relayed(self, send_on, recv_on, frame):
        rx = self.sock(recv_on)
        self.sock(send_on).send(frame)
        with self.assertRaises(socket.timeout):
            rx.recv(2048)

    UXG_FLAGS = (
        "--vlan-id", "none", "--ignore-when-wan-up", "--ignore-logoff",
        "--ping-gateway", "--debug", "wan0", "rtr0",
    )  # fmt: skip

    def test_wan_down_relays_both_ways(self):
        self.start(*self.UXG_FLAGS)
        self.assert_relayed("bgw0", "nid0", eap_start())
        self.assert_relayed("nid0", "bgw0", eap_request_identity())

    def test_wan_up_ignores_router(self):
        self.wan_up()
        self.start(*self.UXG_FLAGS)
        self.assert_not_relayed("bgw0", "nid0", eap_start())
        self.wait_for("ping: 192.0.2.1 success")
        # AT&T's side is always relayed to the BGW.
        self.assert_relayed("nid0", "bgw0", eap_request_identity())

    def test_wan_up_needs_gateway_ping(self):
        # An address and a table-201 route, but a gateway that doesn't answer.
        run("ip", "addr", "add", "198.51.100.10/24", "dev", "wan0")
        run(
            "ip", "route", "add", "default", "via", "198.51.100.1", "dev", "wan0",
            "table", "201",
        )  # fmt: skip
        self.start(*self.UXG_FLAGS)
        self.assert_relayed("bgw0", "nid0", eap_start())
        self.wait_for("ping: 198.51.100.1 failed")

    def test_logoff_always_ignored(self):
        self.start(*self.UXG_FLAGS)
        self.assert_not_relayed("bgw0", "nid0", eap_logoff())

    def test_sigterm_while_logging_exits(self):
        # Upstream logged from its SIGTERM handler; a SIGTERM that landed
        # during another log write was lost and the proxy kept running.
        tx = self.sock("nid0")
        for _ in range(20):
            self.lines = []
            self.start(*self.UXG_FLAGS)
            stop = threading.Event()

            def flood():
                while not stop.is_set():
                    tx.send(eap_request_identity())

            t = threading.Thread(target=flood)
            t.start()
            time.sleep(0.05)
            self.proc.terminate()
            try:
                self.assertEqual(self.proc.wait(5), 0)
            finally:
                stop.set()
                t.join()
            self.proc.stderr.close()
            self.proc = None

    def test_vlan_default_refuses_missing_vlan_interface(self):
        # Upstream's default (IF_WAN.0) silently read the WAN as down forever.
        out = subprocess.run(
            proxy_cmd("--ignore-when-wan-up", "wan0", "rtr0"),
            stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        self.assertEqual(out.returncode, 2)
        self.assertIn("no such interface: wan0.0", out.stderr)

    def test_edgeos_flags_refused(self):
        for flag in ("--set-mac", "--restart-dhcp"):
            out = subprocess.run(
                proxy_cmd("--vlan-id", "none", flag, "wan0", "rtr0"),
                stderr=subprocess.PIPE, text=True,
            )  # fmt: skip
            self.assertEqual(out.returncode, 2, flag)
            self.assertIn("need EdgeOS", out.stderr)


if __name__ == "__main__":
    unittest.main()

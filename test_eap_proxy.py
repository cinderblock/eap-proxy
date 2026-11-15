"""Tests for eap_proxy.py. Linux only.

Run with ./run-tests.sh, which puts the integration tests in a throwaway
network namespace. Addresses are documentation ranges and MACs are made up.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import eap_proxy  # noqa: E402

ETH_P_PAE = 0x888E
EAP_GROUP = bytes.fromhex("0180c2000003")
RTR_MAC = bytes.fromhex("021111111111")  # the AT&T gateway's WAN port
NID_MAC = bytes.fromhex("022222222222")  # AT&T's authenticator

# The shape of `ip -j -4 route show table all default` on a UXG-Pro with three
# WANs, one of them on VLAN 2, plus UniFi's blackhole table.
UXG_ROUTES = [
    {"dst": "default", "gateway": "192.168.254.1", "dev": "eth1",
     "table": "178.eth1", "protocol": "static", "metric": 1, "flags": []},
    {"dst": "default", "gateway": "203.0.113.1", "dev": "eth2.2",
     "table": "201.eth2.2", "protocol": "dhcp", "metric": 200, "flags": []},
    {"dst": "default", "gateway": "198.51.100.1", "dev": "eth0",
     "table": "202.eth0", "protocol": "dhcp", "metric": 200, "flags": []},
    {"type": "blackhole", "dst": "default", "table": "251.blackhole",
     "protocol": "PBR", "flags": []},
]  # fmt: skip


def eapol(src, dst, ptype, body=b""):
    frame = dst + src + struct.pack("!HBBH", ETH_P_PAE, 2, ptype, len(body)) + body
    return frame.ljust(60, b"\0")


def eap_start(src=RTR_MAC):
    return eapol(src, EAP_GROUP, 1)


def eap_logoff(src=RTR_MAC):
    return eapol(src, EAP_GROUP, 2)


def eap_request_identity(ident=7):
    return eapol(NID_MAC, RTR_MAC, 0, struct.pack("!BBHB", 1, ident, 5, 1))


def eap_success(ident=7):
    return eapol(NID_MAC, RTR_MAC, 0, struct.pack("!BBH", 3, ident, 4))


class PickWanRoute(unittest.TestCase):
    def test_vlan_wan(self):
        wan = eap_proxy.pick_wan_route(UXG_ROUTES, {"eth2", "eth2.2"})
        self.assertEqual(wan, ("eth2.2", "203.0.113.1", "201.eth2.2"))

    def test_untagged_wan(self):
        wan = eap_proxy.pick_wan_route(UXG_ROUTES, {"eth0"})
        self.assertEqual(wan.dev, "eth0")

    def test_no_route_on_port(self):
        self.assertIsNone(eap_proxy.pick_wan_route(UXG_ROUTES, {"eth3"}))

    def test_ignores_blackhole_and_gatewayless(self):
        routes = [{"dst": "default", "dev": "eth2", "scope": "link"}] + UXG_ROUTES
        self.assertIsNone(eap_proxy.pick_wan_route(routes, {"eth2"}))

    def test_lowest_metric_wins(self):
        routes = [
            {"dst": "default", "gateway": "192.0.2.1", "dev": "eth2", "metric": 300},
            {"dst": "default", "gateway": "192.0.2.9", "dev": "eth2.2", "metric": 100},
        ]
        wan = eap_proxy.pick_wan_route(routes, {"eth2", "eth2.2"})
        self.assertEqual(wan.gateway, "192.0.2.9")
        self.assertEqual(wan.table, "main")


class Frames(unittest.TestCase):
    def test_parse(self):
        frame = eap_proxy.EAPFrame.from_buf(eap_request_identity())
        self.assertEqual((frame.type, frame.packet.code), (0, 1))
        self.assertEqual(eap_proxy.strmac(frame.src), "02:22:22:22:22:22")
        self.assertIn("Request id 7", str(frame))
        self.assertTrue(eap_proxy.EAPFrame.from_buf(eap_success()).is_success)
        self.assertTrue(eap_proxy.EAPFrame.from_buf(eap_logoff()).is_logoff)
        self.assertFalse(eap_proxy.EAPFrame.from_buf(eap_start()).is_success)

    def test_not_eapol(self):
        with self.assertRaises(ValueError):
            eap_proxy.EAPFrame.from_buf(b"\xff" * 12 + b"\x08\x00" + b"\0" * 46)

    def test_ipchecksum_even_and_odd(self):
        for payload in (b"", b"x", b"abc", b"abcd"):
            header = struct.pack("!BBHHH", 8, 0, 0, 0x1234, 0x5678)
            chksum = eap_proxy.ipchecksum(header + payload)
            packed = struct.pack("!BBHHH", 8, 0, chksum, 0x1234, 0x5678)
            self.assertEqual(eap_proxy.ipchecksum(packed + payload), 0, payload)


class Args(unittest.TestCase):
    def test_removed_flags_explain(self):
        for argv in (["--vlan-id", "none", "lo", "lo"], ["--set-mac", "lo", "lo"]):
            out = subprocess.run(proxy_cmd(*argv), stderr=subprocess.PIPE, text=True)
            self.assertEqual(out.returncode, 2, argv)
            self.assertIn("was removed in v2", out.stderr)

    def test_missing_interface(self):
        out = subprocess.run(proxy_cmd("nope0", "lo"), stderr=subprocess.PIPE, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("no such interface: nope0", out.stderr)

    def test_same_port_twice(self):
        out = subprocess.run(proxy_cmd("lo", "lo"), stderr=subprocess.PIPE, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("must differ", out.stderr)


def run(*args):
    subprocess.run(args, check=True)


def proxy_cmd(*extra):
    return [sys.executable, "-I", os.path.join(HERE, "eap_proxy.py")] + list(extra)


@unittest.skipUnless(
    os.environ.get("EAP_PROXY_NETNS") == "1",
    "integration tests run only inside run-tests.sh's network namespace",
)
class Integration(unittest.TestCase):
    """The real proxy between two veth pairs, laid out like a UXG:

    wan0 (fiber port) <-> nid0 (AT&T's side), with VLAN wan0.2 on top
    rtr0 (AT&T gateway port) <-> bgw0 (the AT&T gateway's WAN port)

    Like UniFi OS, the WAN's default route is only in table 201, not in main.
    """

    @classmethod
    def setUpClass(cls):
        run("ip", "link", "set", "lo", "up")
        for a, b in (("wan0", "nid0"), ("rtr0", "bgw0")):
            run("ip", "link", "add", a, "type", "veth", "peer", "name", b)
            run("ip", "link", "set", a, "up")
            run("ip", "link", "set", b, "up")
        run("ip", "link", "add", "link", "wan0", "name", "wan0.2", "type", "vlan", "id", "2")
        run("ip", "link", "set", "wan0.2", "up")
        # The "gateway" is a local address, so pings to it get answered.
        run("ip", "addr", "add", "192.0.2.1/32", "dev", "lo")
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.lines = []
        self.proc = None
        self.status = os.path.join(self.tmp.name, "status-%s.json" % self._testMethodName)

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc.stderr.close()
        for dev in ("wan0", "wan0.2"):
            run("ip", "addr", "flush", "dev", dev)
        subprocess.run(
            ["ip", "route", "flush", "table", "201"],
            check=False, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        subprocess.run(["ip", "link", "del", "br9"], check=False, stderr=subprocess.DEVNULL)

    def wan_up(self, dev="wan0", gateway="192.0.2.1"):
        # The address shares a /24 with the gateway, as a DHCP lease would.
        address = gateway.rsplit(".", 1)[0] + ".10"
        run("ip", "addr", "add", address + "/24", "dev", dev)
        run(
            "ip", "route", "add", "default", "via", gateway, "dev", dev,
            "table", "201", "metric", "200",
        )  # fmt: skip

    def start(self, *extra, env=None):
        args = ("--status-file", self.status, "--check-interval", "0.3") + extra
        self.proc = subprocess.Popen(
            proxy_cmd(*(args + ("wan0", "rtr0"))),
            stderr=subprocess.PIPE, text=True, env=env,
        )  # fmt: skip
        threading.Thread(target=self._read, daemon=True).start()
        self.wait_for("eap_proxy 2")

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

    def read_status(self, timeout=5, until=lambda s: True):
        deadline = time.time() + timeout
        while True:
            try:
                with open(self.status) as f:
                    status = json.load(f)
                if until(status):
                    return status
            except FileNotFoundError:
                pass
            if time.time() > deadline:
                self.fail("status never matched; last lines:\n" + "\n".join(self.lines))
            time.sleep(0.05)

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

    def test_wan_down_relays_both_ways(self):
        self.start()
        self.wait_for("WAN down: no default route on wan0")
        self.assert_relayed("bgw0", "nid0", eap_start())
        self.assert_relayed("nid0", "bgw0", eap_request_identity())

    def test_wan_up_drops_router_frames(self):
        self.wan_up()
        self.start()
        self.wait_for("WAN up: wan0 192.0.2.10 via 192.0.2.1 (table 201)")
        self.assert_not_relayed("bgw0", "nid0", eap_start())
        self.wait_for("dropped (WAN is up)")
        # AT&T's side always reaches the AT&T gateway.
        self.assert_relayed("nid0", "bgw0", eap_request_identity())

    def test_wan_on_a_vlan_is_found(self):
        self.wan_up(dev="wan0.2")
        self.start()
        self.wait_for("WAN up: wan0.2 192.0.2.10 via 192.0.2.1")
        self.assert_not_relayed("bgw0", "nid0", eap_start())

    def test_wan_up_needs_a_gateway_that_answers(self):
        self.wan_up(gateway="198.51.100.1")
        self.start()
        self.wait_for("WAN down: 198.51.100.1 doesn't answer a ping")
        self.assert_relayed("bgw0", "nid0", eap_start())

    def test_wan_goes_up_later(self):
        self.start()
        self.wait_for("WAN down")
        self.wan_up()
        self.wait_for("WAN up: wan0")
        self.wait_for("resolved: no default route")

    def test_relay_always(self):
        self.wan_up()
        self.start("--relay-always")
        self.wait_for("WAN up")
        self.assert_relayed("bgw0", "nid0", eap_start())

    def test_logoff_dropped_unless_asked(self):
        self.start()
        self.assert_not_relayed("bgw0", "nid0", eap_logoff())
        self.wait_for("dropped (logoff)")
        self.proc.terminate()
        self.proc.wait(5)
        self.proc.stderr.close()
        self.lines = []
        self.start("--relay-logoff")
        self.assert_relayed("bgw0", "nid0", eap_logoff())

    def test_router_port_in_a_bridge_warns(self):
        run("ip", "link", "add", "br9", "type", "bridge")
        run("ip", "link", "set", "rtr0", "master", "br9")
        self.start()
        self.wait_for("rtr0 is in bridge br9")
        status = self.read_status(until=lambda s: s["warnings"])
        self.assertTrue(any("bridge br9" in w for w in status["warnings"]))
        run("ip", "link", "set", "rtr0", "nomaster")
        self.wait_for("resolved: rtr0 is in bridge br9")

    def test_mac_clone_mismatch_warns(self):
        run("ip", "link", "set", "wan0", "address", "02:33:33:33:33:33")
        self.start()
        self.assert_relayed("bgw0", "nid0", eap_start())
        self.wait_for("wan0 has MAC 02:33:33:33:33:33 but the AT&T gateway's is 02:11:11:11:11:11")
        run("ip", "link", "set", "wan0", "address", "02:11:11:11:11:11")
        self.assert_relayed("bgw0", "nid0", eap_start())
        self.wait_for("resolved: wan0 has MAC")

    def test_status_file(self):
        self.wan_up()
        self.start()
        self.assert_relayed("nid0", "bgw0", eap_success())
        status = self.read_status(until=lambda s: s["last_success"])
        self.assertEqual(status["version"], eap_proxy.__version__)
        self.assertEqual(status["wan"]["dev"], "wan0")
        self.assertTrue(status["wan"]["up"])
        self.assertEqual(status["last_relay"]["from"], "wan0")
        self.assertEqual(status["last_relay"]["frame"], "EAP packet Success id 7")
        self.assertEqual(status["warnings"], [])

    def test_systemd_ready_and_watchdog(self):
        path = os.path.join(self.tmp.name, "notify")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(path)
        self.addCleanup(listener.close)
        listener.settimeout(3)
        env = dict(os.environ, NOTIFY_SOCKET=path, WATCHDOG_USEC="200000")
        self.start(env=env)
        messages = []
        while sum(m == "WATCHDOG=1" for m in messages) < 3:
            messages.append(listener.recv(1024).decode())
        self.assertIn("READY=1", messages)
        self.assertTrue(any(m.startswith("STATUS=WAN down") for m in messages))

    def test_journal_priorities(self):
        env = dict(os.environ, JOURNAL_STREAM="8:1")
        self.start(env=env)
        self.wait_for("<4>WAN down")
        self.wait_for("<3>no default route on wan0")

    def test_sigterm_while_logging_exits(self):
        # Upstream logged from its SIGTERM handler; a SIGTERM that landed
        # during another log write was lost and the proxy kept running.
        tx = self.sock("nid0")
        for _ in range(20):
            self.lines = []
            self.start()
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


if __name__ == "__main__":
    unittest.main()

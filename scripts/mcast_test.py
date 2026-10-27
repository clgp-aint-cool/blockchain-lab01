#!/usr/bin/env python3
"""Multicast reachability test for Lab01 peer discovery.

Every instance joins a multicast group, sends a small beacon every
--interval seconds, and prints beacons it hears from OTHER instances.
At the end it prints which peers it heard from.

Same machine, two terminals:
    python mcast_test.py --id A
    python mcast_test.py --id B

Same machine, automatic (spawns N processes and checks all heard each other):
    python mcast_test.py --self-test 4

Across machines over ZeroTier, e.g. homelab on the home router and laptop on
a 5G hotspot. --zerotier picks this machine's ZeroTier IP automatically:
    homelab: python mcast_test.py --id homelab --zerotier --duration 30
    laptop : python mcast_test.py --id laptop  --zerotier --duration 30
or give the ZeroTier IP yourself with --iface 10.147.17.x.

Every instance also echoes the beacons it hears, so each side measures the
round-trip time to the other side. Use it to choose consensus timeouts.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import struct
import subprocess
import sys
import time
from statistics import mean

DEFAULT_GROUP = "239.1.1.1"
DEFAULT_PORT = 50000
MAGIC = "lab01-mcast-test"


def make_socket(group: str, port: int, iface: str | None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    # Let several processes on the same machine bind the same UDP port.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    # Bind to all addresses on the port. Binding to the group address does
    # not work on Windows, binding to "" works everywhere.
    sock.bind(("", port))

    # Join the group on the chosen interface (0.0.0.0 = let the OS choose).
    iface_addr = iface or "0.0.0.0"
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface_addr))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    # Outgoing multicast: which interface, stay inside the LAN, and deliver a
    # copy to other processes on this same machine.
    if iface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    sock.settimeout(0.2)
    return sock


def hint_for_send_error(err: OSError) -> str:
    lines = [f"Send failed: {err}"]
    if platform.system() == "Linux":
        lines += [
            "Hint: Linux has no route for multicast when the machine has no network.",
            "  Either connect to a network / start ZeroTier and pass --iface <its IP>, or run:",
            "    sudo ip link set lo multicast on",
            "    sudo ip route add 239.0.0.0/8 dev lo",
            "  and then use --iface 127.0.0.1",
        ]
    else:
        lines.append("Hint: pass --iface <IP of the interface to use>, e.g. your ZeroTier IP.")
    return "\n".join(lines)


def find_zerotier_ip() -> str | None:
    """Return the first IPv4 address assigned by ZeroTier, using zerotier-cli."""
    for cmd in (
        ["zerotier-cli", "-j", "listnetworks"],
        ["sudo", "-n", "zerotier-cli", "-j", "listnetworks"],
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0:
            continue
        try:
            networks = json.loads(out.stdout)
        except json.JSONDecodeError:
            continue
        for net in networks:
            for cidr in net.get("assignedAddresses", []):
                ip = cidr.split("/")[0]
                if ":" not in ip:
                    return ip
    return None


def run(args: argparse.Namespace) -> int:
    try:
        sock = make_socket(args.group, args.port, args.iface)
    except OSError as err:
        print(f"[{args.id}] Could not set up multicast socket: {err}", file=sys.stderr)
        print(
            "Hint: check that --iface is an IP address that belongs to this machine.",
            file=sys.stderr,
        )
        return 2

    if not args.quiet:
        print(
            f"[{args.id}] group={args.group}:{args.port} iface={args.iface or 'OS default'} "
            f"host={socket.gethostname()} pid={os.getpid()} duration={args.duration}s"
        )

    heard: dict[str, dict] = {}
    rtts: dict[str, list[float]] = {}
    seq = 0
    next_send = 0.0
    deadline = time.monotonic() + args.duration
    send_error_shown = False

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_send:
            beacon = {
                "magic": MAGIC,
                "id": args.id,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "seq": seq,
                "t": time.monotonic(),
            }
            try:
                sock.sendto(json.dumps(beacon).encode(), (args.group, args.port))
            except OSError as err:
                if not send_error_shown:
                    print(hint_for_send_error(err), file=sys.stderr)
                    send_error_shown = True
            seq += 1
            next_send = now + args.interval

        try:
            data, addr = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except OSError as err:
            print(f"[{args.id}] receive error: {err}", file=sys.stderr)
            continue

        try:
            msg = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if msg.get("magic") != MAGIC:
            continue
        mine = msg.get("id") == args.id and msg.get("pid") == os.getpid()

        if msg.get("type") == "echo":
            # An echo of one of OUR beacons: round-trip time on our own clock.
            if msg.get("to_id") == args.id and msg.get("to_pid") == os.getpid() and not mine:
                rtt_ms = (time.monotonic() - msg["t"]) * 1000
                rtts.setdefault(msg.get("id", "?"), []).append(rtt_ms)
            continue

        if mine:
            continue  # our own beacon, looped back

        # Echo the beacon back so its sender can measure the round trip.
        echo = {
            "magic": MAGIC,
            "type": "echo",
            "id": args.id,
            "pid": os.getpid(),
            "to_id": msg.get("id"),
            "to_pid": msg.get("pid"),
            "t": msg.get("t", 0),
        }
        try:
            sock.sendto(json.dumps(echo).encode(), (args.group, args.port))
        except OSError:
            pass

        peer = msg.get("id", "?")
        entry = heard.setdefault(peer, {"from": addr[0], "host": msg.get("host"), "count": 0})
        entry["count"] += 1
        if not args.quiet:
            print(
                f"[{args.id}] heard {peer} seq={msg.get('seq')} "
                f"from {addr[0]} host={msg.get('host')}"
            )

    sock.close()

    rtt_stats = {
        peer: {
            "min": round(min(v), 1),
            "avg": round(mean(v), 1),
            "max": round(max(v), 1),
            "n": len(v),
        }
        for peer, v in rtts.items()
    }
    summary = {"id": args.id, "sent": seq, "heard": heard, "rtt_ms": rtt_stats}
    if args.json_summary:
        print(json.dumps(summary))
    else:
        print(f"\n[{args.id}] sent {seq} beacons")
        if heard:
            for peer in sorted(heard):
                e = heard[peer]
                print(
                    f"[{args.id}]   heard {peer}: {e['count']} beacons "
                    f"from {e['from']} host={e['host']}"
                )
                if peer in rtt_stats:
                    s = rtt_stats[peer]
                    print(
                        f"[{args.id}]     round-trip to {peer}: "
                        f"min {s['min']} ms, avg {s['avg']} ms, "
                        f"max {s['max']} ms over {s['n']} echoes"
                    )
            if rtt_stats:
                worst = max(s["max"] for s in rtt_stats.values())
                print(
                    f"[{args.id}]   worst round-trip {worst} ms -> keep consensus timeouts >= "
                    f"{int(worst * 10)} ms (10x), and 'short' injected delays well below that."
                )
        else:
            print(f"[{args.id}]   heard NOBODY. Multicast is not working between these instances.")
    return 0 if heard else 1


def self_test(args: argparse.Namespace) -> int:
    n = args.self_test
    ids = [f"P{i}" for i in range(n)]
    cmd_base = [
        sys.executable,
        os.path.abspath(__file__),
        "--group",
        args.group,
        "--port",
        str(args.port),
        "--duration",
        str(args.duration),
        "--interval",
        str(args.interval),
        "--quiet",
        "--json-summary",
    ]
    if args.iface:
        cmd_base += ["--iface", args.iface]

    print(f"Self-test: starting {n} processes on this machine for {args.duration}s ...")
    procs = [
        subprocess.Popen(
            cmd_base + ["--id", pid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for pid in ids
    ]

    ok = True
    for pid, proc in zip(ids, procs, strict=False):
        out, err = proc.communicate()
        if err.strip():
            print(f"[{pid}] stderr:\n{err.strip()}")
        try:
            summary = json.loads(out.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            print(f"[{pid}] FAILED: no summary (exit code {proc.returncode})")
            ok = False
            continue
        missing = sorted(set(ids) - {pid} - set(summary["heard"]))
        if missing:
            ok = False
            print(f"[{pid}] FAIL: did not hear {', '.join(missing)}")
        else:
            print(f"[{pid}] OK: heard all {n - 1} others")

    print(
        "\nRESULT:", "PASS, multicast works on this machine" if ok else "FAIL, see messages above"
    )
    print(f"Python {platform.python_version()} on {platform.system()} {platform.release()}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Multicast reachability test for Lab01 peer discovery.")
    p.add_argument("--id", default=socket.gethostname(), help="name shown to other instances")
    p.add_argument(
        "--group", default=DEFAULT_GROUP, help=f"multicast group (default {DEFAULT_GROUP})"
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default {DEFAULT_PORT})"
    )
    p.add_argument(
        "--iface", help="IP of the interface to use, e.g. your ZeroTier IP; default: OS choice"
    )
    p.add_argument(
        "--zerotier", action="store_true", help="use this machine's ZeroTier IP as --iface"
    )
    p.add_argument("--duration", type=float, default=15.0, help="seconds to run (default 15)")
    p.add_argument(
        "--interval", type=float, default=1.0, help="seconds between beacons (default 1)"
    )
    p.add_argument(
        "--self-test",
        type=int,
        metavar="N",
        help="spawn N local processes and check they all hear each other",
    )
    p.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--json-summary", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.zerotier and not args.iface:
        args.iface = find_zerotier_ip()
        if not args.iface:
            print(
                "Could not find a ZeroTier IP with zerotier-cli. "
                "Check that ZeroTier is running and joined,\n"
                "or pass it yourself: --iface <ZeroTier IP> "
                "(see 'zerotier-cli listnetworks', 'ip addr' or 'ipconfig').",
                file=sys.stderr,
            )
            return 2
        print(f"Using ZeroTier IP {args.iface}")

    if args.self_test:
        if args.self_test < 2:
            p.error("--self-test needs N >= 2")
        if args.duration == 15.0:
            args.duration = 5.0
        return self_test(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

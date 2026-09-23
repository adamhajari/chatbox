#!/usr/bin/env python3
"""Where does a Talkbox question's time actually go on this machine?

`scripts/measure.py` shows that a question is slow; this shows why. It splits the cost
of talking to the API into the parts that behave differently on a Raspberry Pi:

  dns / tcp / tls   opening a fresh connection (the Pi 3B has no crypto instructions,
                    so the TLS handshake is done in software)
  cold              the first API call on a new client, handshake included
  warm              a later call reusing that connection
  concurrent pair   two calls at once, which is what the pipeline really does
                    (classify and generate are submitted together)

Run it on the Pi and on the Mac and compare. Uses tiny requests (a few tokens each).

    python scripts/probe_net.py            # 5 rounds
    python scripts/probe_net.py -n 10
"""

from __future__ import annotations

import argparse
import socket
import ssl
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HOST = "api.anthropic.com"
PORT = 443


def _ms(since: float) -> float:
    return (time.monotonic() - since) * 1000


def connection_costs() -> dict[str, float]:
    """Time a fresh DNS lookup, TCP connect and TLS handshake, separately."""
    t = time.monotonic()
    info = socket.getaddrinfo(HOST, PORT, socket.AF_UNSPEC, socket.SOCK_STREAM)[0]
    dns = _ms(t)

    family, socktype, proto, _, sockaddr = info
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(30)
    try:
        t = time.monotonic()
        sock.connect(sockaddr)
        tcp = _ms(t)

        ctx = ssl.create_default_context()
        t = time.monotonic()
        with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
            handshake = _ms(t)
            cipher = tls.cipher()[0] if tls.cipher() else "?"
    finally:
        sock.close()
    return {"dns": dns, "tcp": tcp, "tls": handshake, "cipher": cipher}


def api_costs(client) -> float:
    """One minimal API call. Returns milliseconds."""
    t = time.monotonic()
    client.messages.create(model="claude-haiku-4-5", max_tokens=1,
                           messages=[{"role": "user", "content": "hi"}])
    return _ms(t)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--rounds", type=int, default=5)
    args = ap.parse_args()

    import anthropic
    from dotenv import load_dotenv

    load_dotenv()

    print(f"{sys.platform} · python {sys.version.split()[0]} · {HOST} · {args.rounds} rounds\n")
    rows: dict[str, list[float]] = {k: [] for k in
                                    ("dns", "tcp", "tls", "cold", "warm", "pair-a", "pair-b")}
    cipher = "?"

    for i in range(args.rounds):
        c = connection_costs()
        cipher = c.pop("cipher")
        for k, v in c.items():
            rows[k].append(v)

        # A new client each round, so "cold" really is a fresh connection.
        client = anthropic.Anthropic()
        rows["cold"].append(api_costs(client))
        rows["warm"].append(api_costs(client))

        # Two at once on one client, the way the pipeline submits classify and generate.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(api_costs, client) for _ in range(2)]
            a, b = sorted(f.result() for f in futures)
        rows["pair-a"].append(a)
        rows["pair-b"].append(b)

        print(f"round {i + 1}: dns {c['dns']:.0f}  tcp {c['tcp']:.0f}  tls {c['tls']:.0f}  "
              f"cold {rows['cold'][-1]:.0f}  warm {rows['warm'][-1]:.0f}  "
              f"pair {a:.0f}/{b:.0f}  (ms)")

    print(f"\ncipher: {cipher}")
    print("\nsummary (ms)")
    for name, values in rows.items():
        print(f"  {name:<8} median {statistics.median(values):7.0f}   "
              f"min {min(values):7.0f}   max {max(values):7.0f}")
    print("""
How to read it:
  tls much larger than tcp        -> the software TLS handshake is the cost; reusing one
                                     connection instead of opening two would pay off.
  warm close to the Mac's warm    -> the network and the board are fine once connected.
  pair much worse than warm       -> the two concurrent calls are fighting each other.
  everything jittery, max >> median, including tcp and dns
                                  -> the Wi-Fi link is the problem, not the board.""")


if __name__ == "__main__":
    main()

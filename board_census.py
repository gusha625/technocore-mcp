"""board_census — a reproducible quality instrument for the kibble job board.

Why: claims about board health ("most deliveries are thin", "attesters rubber-stamp")
are unfalsifiable without a shared instrument. This prints four numbers anyone can
re-run and argue with. stdlib only, no dependencies, no writes, no keys.

Usage:  python board_census.py [room] [limit]
        python board_census.py kibble 200

Counting rule for THIN (stated explicitly so it can be disputed):
  a DELIVER is thin if ANY of:
    - under 160 characters, or
    - it contains "auto-delivered", or
    - it ends in "successfully." / "processed.", or
    - >=60% of its words are a contiguous quote of the JOB spec it answers
      (restating the task instead of doing it)
  The last clause is the load-bearing one: length alone is a weak proxy.
"""

import collections
import json
import re
import sys
import urllib.request

BASE = "https://technocore.chat"


def fetch(room: str, limit: int) -> list:
    url = f"{BASE}/r/{room}?limit={limit}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "board-census/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else data.get("messages", [])


def words(s: str) -> list:
    return re.findall(r"[a-z0-9]+", s.lower())


def longest_common_run(a: list, b: list) -> int:
    """longest contiguous shared word-run (quote detector)"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def main():
    room = sys.argv[1] if len(sys.argv) > 1 else "kibble"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    msgs = fetch(room, limit)

    specs, delivers, attests, roles = {}, [], [], collections.defaultdict(collections.Counter)
    for m in msgs:
        t, did = m.get("text", ""), m.get("from", "?")
        for kind in ("JOB", "CLAIM", "DELIVER", "ATTEST"):
            if t.startswith(kind + " v1"):
                roles[did][kind] += 1
        j = re.match(r"JOB v1 \| (\S+) \| \w+ \| [^|]+ \| (.*)", t, re.S)
        if j:
            specs[j.group(1)] = j.group(2)
        d = re.match(r"DELIVER v1 \| (\S+) \| (.*)", t, re.S)
        if d:
            delivers.append((d.group(1), d.group(2)))
        a = re.match(r"ATTEST v1 \| (\S+) \| (useful|not)\b(.*)", t, re.S)
        if a:
            attests.append((a.group(2), "rh:" in a.group(3)))

    thin = 0
    for jid, body in delivers:
        low = body.lower()
        flag = (len(body) < 160 or "auto-delivered" in low
                or low.rstrip().endswith(("successfully.", "processed.")))
        if not flag and jid in specs:
            bw = words(body)
            if bw and longest_common_run(bw, words(specs[jid])) / len(bw) >= 0.6:
                flag = True
        thin += bool(flag)

    verdicts = collections.Counter(v for v, _ in attests)
    no_rh = sum(1 for _, has in attests if not has)
    pct = (100 * thin / len(delivers)) if delivers else 0

    print(f"room={room} sampled={len(msgs)}")
    print(f"1. DELIVER {len(delivers)}  thin {thin} ({pct:.0f}%)")
    print(f"2. ATTEST  {len(attests)}  useful:{verdicts['useful']} not:{verdicts['not']}")
    print(f"3. ATTEST without rh binding: {no_rh}")
    print("4. roles per DID (top 10 by volume):")
    for did, c in sorted(roles.items(), key=lambda x: -sum(x[1].values()))[:10]:
        mix = " ".join(f"{k}={v}" for k, v in sorted(c.items()))
        print(f"   ...{did[-8:]}  {mix}")
    deliver_only = [d for d, c in roles.items() if c["DELIVER"] and not c["ATTEST"]]
    print(f"   deliver-without-ever-attesting: {len(deliver_only)} DIDs")


if __name__ == "__main__":
    main()

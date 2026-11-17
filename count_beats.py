#!/usr/bin/env python3
"""
For every track in the given track files, fetch all replays from the Mania Exchange
API and count, per player, on how many tracks their replay beats (or equals) the set
time/score. Use --strict to make equal values not count.

Track file format (tab/space separated):  name  map_uid  track_id  set_value
  set_value is a time (22.08, 1:09.67) or, for stunt tracks, a score (521).
  (map_uid is ignored; blank lines are skipped.)

Each --tracks argument is  FILE[:SITE[:MODE]]
  SITE  tmuf (default) or tmnf  -> tmuf.exchange / tmnf.exchange
  MODE  time (default) or score -> score tracks: a HIGHER ReplayScore beats the set value

Examples:
    python count_beats.py --tracks tracks.txt --json nadeo.json --label "Nadeo tracks"
    python count_beats.py --tracks race.txt:tmnf puzzle.txt stunt.txt:tmuf:score \
        --json extension.json --label Extension

Players are matched by user id when every file uses the same site; when several sites are
mixed (user ids differ between tmuf and tmnf) they are matched by name, case-insensitively.
"""
import argparse
import csv
import json
import sys
import time
from collections import defaultdict

import requests

PAGE = 1000
HEADERS = {"User-Agent": "tmuf-set-time-counter/1.0"}


def parse_time(s: str) -> int:
    """'1:09.67' / '22.08' / '2:24.28' -> milliseconds."""
    parts = s.strip().split(":")
    secs = float(parts[-1])
    mins = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return round(((hours * 60 + mins) * 60 + secs) * 1000)


def fmt_time(ms: int) -> str:
    m, rest = divmod(ms, 60000)
    s = rest / 1000
    return f"{m}:{s:05.2f}" if m else f"{s:.2f}"


def fmt_value(t, v) -> str:
    return str(v) if t["mode"] == "score" else fmt_time(v)


def load_tracks(spec):
    path, *opts = spec.split(":")
    site = opts[0] if len(opts) > 0 and opts[0] else "tmuf"
    mode = opts[1] if len(opts) > 1 and opts[1] else "time"
    if site not in ("tmuf", "tmnf") or mode not in ("time", "score"):
        sys.exit(f"Bad spec {spec!r}: SITE is tmuf|tmnf, MODE is time|score")
    tracks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) < 4:
                continue
            value = int(p[3]) if mode == "score" else parse_time(p[3])
            tracks.append({"name": p[0], "id": int(p[2]), "set": value,
                           "site": site, "mode": mode})
    return tracks


def get_json(session, url, params, retries=5):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_replays(session, track):
    """All replays of a track, paginated with `after` (last ReplayId)."""
    fields = "ReplayId,ReplayTime,User.UserId,User.Name"
    if track["mode"] == "score":
        fields += ",ReplayScore"
    url = f"https://{track['site']}.exchange/api/replays"
    out, after = [], None
    while True:
        params = {"trackid": track["id"], "count": PAGE, "fields": fields}
        if after is not None:
            params["after"] = after
        data = get_json(session, url, params)
        results = data.get("Results", [])
        out.extend(results)
        if not data.get("More") or not results:
            return out
        after = results[-1]["ReplayId"]
        time.sleep(0.2)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", nargs="+", default=["tracks.txt"], metavar="FILE[:SITE[:MODE]]")
    ap.add_argument("--strict", action="store_true",
                    help="require strictly better than the set value (default: equal also counts)")
    ap.add_argument("--csv", help="write the ranking to this CSV file")
    ap.add_argument("--json", help="write full results (tracks + players) to this JSON file")
    ap.add_argument("--label", default="", help="dataset label stored in the JSON")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between requests")
    args = ap.parse_args()

    tracks = [t for spec in args.tracks for t in load_tracks(spec)]
    names_seen = [t["name"] for t in tracks]
    if len(set(names_seen)) != len(names_seen):
        sys.exit("Duplicate track names across the track files")
    by_name = len({t["site"] for t in tracks}) > 1
    print(f"Loaded {len(tracks)} tracks "
          f"(matching players by {'name' if by_name else 'user id'})", file=sys.stderr)

    times = defaultdict(dict)   # player key -> {track name: best qualifying value}
    miss = defaultdict(dict)    # player key -> {track name: best result that did NOT beat the set value}
    names = {}                  # player key -> display name
    track_info = []
    session = requests.Session()

    for i, t in enumerate(tracks, 1):
        replays = fetch_replays(session, t)
        if len({r["ReplayId"] for r in replays}) != len(replays):
            print(f"  WARNING: duplicate replays returned for {t['name']}", file=sys.stderr)
        score = t["mode"] == "score"
        for rp in replays:
            u = rp.get("User") or {}
            if u.get("UserId") is None:
                continue
            key = u.get("Name", "").lower() if by_name else u["UserId"]
            names[key] = u.get("Name", str(u["UserId"]))
            v = rp["ReplayScore"] if score else rp["ReplayTime"]
            if score:
                ok = v > t["set"] or (not args.strict and v == t["set"])
            else:
                ok = v < t["set"] or (not args.strict and v == t["set"])
            if ok:
                old = times[key].get(t["name"])   # keep the player's best, one per track
                better = old is None or (v > old if score else v < old)
                if better:
                    times[key][t["name"]] = v
            else:
                diff = (t["set"] - v) if score else (v - t["set"])   # how far short
                if diff > 0 and not (score and v <= 0):   # ignore 0-score stunt replays
                    old = miss[key].get(t["name"])
                    if old is None or (v > old if score else v < old):
                        miss[key][t["name"]] = v
        beaten = sum(1 for k in times if t["name"] in times[k])
        track_info.append({"name": t["name"], "id": t["id"], "set": t["set"], "site": t["site"],
                           "mode": t["mode"], "replays": len(replays), "beaten_by": beaten})
        print(f"[{i:3}/{len(tracks)}] {t['name']:<14} {t['site']} id={t['id']:<8} "
              f"set={fmt_value(t, t['set']):>8}  replays={len(replays):<4} beaten_by={beaten}",
              file=sys.stderr)
        time.sleep(args.delay)

    for key in list(miss):   # a miss only matters if the player never beat that track
        for name in [n for n in miss[key] if n in times.get(key, {})]:
            del miss[key][name]
    ranking = sorted(times.items(), key=lambda kv: (-len(kv[1]), names[kv[0]].lower()))

    print(f"\n{'#':>3}  {'Player':<28}Beaten")
    print("-" * 44)
    rank, prev = 0, None
    for pos, (key, tr) in enumerate(ranking, 1):
        if len(tr) != prev:
            rank, prev = pos, len(tr)
        print(f"{rank:>3}  {names[key]:<28}{len(tr)}/{len(tracks)}")

    if args.json:
        out = {"label": args.label, "tracks": track_info,
               "players": [{"id": key, "name": names[key], "count": len(tr), "times": times[key],
                            "miss": miss.get(key, {})}
                           for key, tr in ranking]}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"Wrote {args.json}", file=sys.stderr)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Player", "TracksBeaten", "Tracks"])
            for key, tr in ranking:
                w.writerow([names[key], len(tr), " ".join(sorted(tr))])
        print(f"Wrote {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()

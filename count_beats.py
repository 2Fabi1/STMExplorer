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

--source tmx (default) uses the replays uploaded to tmuf/tmnf.exchange.
--source ul uses the official in-game leaderboards via the UnitedLadder API
(api.ul.unitedascenders.xyz), looked up by the map uid in column 2 of the track file.
There players are matched by their game login, and only records up to 0.5% worse than
the set value are fetched (leaderboards hold ~100k records, sorted best first).

Players (tmx) are matched by user id when every file uses the same site; when several sites are
mixed (user ids differ between tmuf and tmnf) they are matched by name, case-insensitively.
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict

import requests

PAGE = 1000
UL_BASE = "https://api.ul.unitedascenders.xyz"
UL_PAGE = 100                    # records per leaderboard page
UL_STUNT_BASE = 4008636142       # UnitedLadder stores a stunt score as UL_STUNT_BASE - score
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
    # Options are parsed from the right so Windows paths like C:/x/tracks.txt still work.
    parts = spec.split(":")
    site, mode = "tmuf", "time"
    while len(parts) > 1 and parts[-1] in ("tmuf", "tmnf", "time", "score", ""):
        opt = parts.pop()
        if opt in ("tmuf", "tmnf"):
            site = opt
        elif opt in ("time", "score"):
            mode = opt
    path = ":".join(parts)
    tracks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) < 4:
                continue
            value = int(p[3]) if mode == "score" else parse_time(p[3])
            tracks.append({"name": p[0], "uid": p[1], "id": int(p[2]), "set": value,
                           "site": site, "mode": mode})
    return tracks


def get_json(session, url, params, retries=5):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=60)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if attempt == retries - 1 or (status is not None and 400 <= status < 500):
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


def strip_tm(s: str) -> str:
    """Remove TrackMania $-formatting from a nickname ($i, $o, $fff, $l[url], $$ ...)."""
    s = s.replace("$$", "\x00")
    s = re.sub(r"\$[lhp]\[[^\]]*\]", "", s)
    s = re.sub(r"\$[0-9a-fA-F]{3}", "", s)
    s = re.sub(r"\$[^\x00]?", "", s)
    return s.replace("\x00", "$").strip()


def fetch_records_ul(session, track, stop_pct, delay):
    """Official leaderboard records as (login, nickname, value), best first.

    Pages are fetched until a page ends more than `stop_pct` percent behind the set value.
    Returns (records, total_record_count, pages_fetched).
    """
    score = track["mode"] == "score"
    recs, total, page = [], 0, 0
    while True:
        data = get_json(session, f"{UL_BASE}/tracks/World/{track['uid']}/{page}", None)
        total = data.get("recordCount", total)
        rows = data.get("records", [])
        too_far = False
        for r in rows:
            v = UL_STUNT_BASE - r["points"] if score else r["points"]
            behind = (track["set"] - v) if score else (v - track["set"])
            if behind * 100 > track["set"] * stop_pct:   # records are sorted, so the rest is further behind
                too_far = True
                break
            recs.append((r["login"], strip_tm(r.get("nickname", "")) or r["login"], v))
        page += 1
        if too_far or len(rows) < UL_PAGE:
            break
        time.sleep(delay)
    return recs, total, page


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", nargs="+", default=["tracks.txt"], metavar="FILE[:SITE[:MODE]]")
    ap.add_argument("--strict", action="store_true",
                    help="require strictly better than the set value (default: equal also counts)")
    ap.add_argument("--source", choices=("tmx", "ul"), default="tmx",
                    help="tmx = replays on tmuf/tmnf.exchange (default); ul = UnitedLadder official leaderboards")
    ap.add_argument("--ul-miss-pct", type=float, default=0.5, metavar="PCT",
                    help="ul only: fetch records up to PCT percent behind the set value (default 0.5)")
    ap.add_argument("--csv", help="write the ranking to this CSV file")
    ap.add_argument("--json", help="write full results (tracks + players) to this JSON file")
    ap.add_argument("--label", default="", help="dataset label stored in the JSON")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between requests")
    args = ap.parse_args()

    tracks = [t for spec in args.tracks for t in load_tracks(spec)]
    names_seen = [t["name"] for t in tracks]
    if len(set(names_seen)) != len(names_seen):
        sys.exit("Duplicate track names across the track files")
    ul = args.source == "ul"
    by_name = not ul and len({t["site"] for t in tracks}) > 1
    print(f"Loaded {len(tracks)} tracks from {'UnitedLadder' if ul else 'exchange replays'} "
          f"(matching players by {'login' if ul else 'name' if by_name else 'user id'})", file=sys.stderr)

    times = defaultdict(dict)   # player key -> {track name: best qualifying value}
    miss = defaultdict(dict)    # player key -> {track name: best result that did NOT beat the set value}
    names = {}                  # player key -> display name (ul: the game login)
    nicks = {}                  # ul only: player key -> nickname (formatting stripped)
    track_info = []
    session = requests.Session()

    for i, t in enumerate(tracks, 1):
        score = t["mode"] == "score"
        if ul:
            try:
                recs, total, pages = fetch_records_ul(session, t, args.ul_miss_pct, args.delay)
            except requests.RequestException as e:
                print(f"  WARNING: {t['name']} ({t['uid']}) failed: {e}", file=sys.stderr)
                recs, total, pages = [], 0, 0
        else:
            replays = fetch_replays(session, t)
            if len({r["ReplayId"] for r in replays}) != len(replays):
                print(f"  WARNING: duplicate replays returned for {t['name']}", file=sys.stderr)
            recs, total, pages = [], len(replays), 0
            for rp in replays:
                u = rp.get("User") or {}
                if u.get("UserId") is None:
                    continue
                key = u.get("Name", "").lower() if by_name else u["UserId"]
                recs.append((key, u.get("Name", str(u["UserId"])),
                             rp["ReplayScore"] if score else rp["ReplayTime"]))
        for key, name, v in recs:
            names[key] = key if ul else name
            if ul:
                nicks[key] = name
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
                           "mode": t["mode"], "replays": total, "beaten_by": beaten})
        print(f"[{i:3}/{len(tracks)}] {t['name']:<14} {t['site']} id={t['id']:<8} "
              f"set={fmt_value(t, t['set']):>8}  {'records' if ul else 'replays'}={total:<6}"
              f"{f' pages={pages}' if ul else ''} beaten_by={beaten}",
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
        out = {"label": args.label, "source": args.source, "tracks": track_info,
               "players": [{"id": key, "name": names[key], "count": len(tr), "times": times[key],
                            "miss": miss.get(key, {}), **({"nick": nicks[key]} if ul else {})}
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

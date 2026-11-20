#!/usr/bin/env python3
"""
Static facts about every STM, kept in stm-info.csv so they are never looked up again.

The STM is the world record of the track as it stood on 31 Dec 2015, taken from the exchange. That never changes, so
for every track the file holds: the STM, the author medal (RTM), and who set the STM (driver, date, replay id and the
replay download link). The scraper and the page read this file; nothing is fetched for a track that is in it.

    python stm_info.py            # look up only the tracks that are not in the CSV yet (normally none) and add them
    python stm_info.py --all      # look up every track again (rarely needed)

The driver is the player of the earliest replay with exactly the STM result on the exchange (replays that are no
longer on the leaderboard count too); the download link is https://<site>.exchange/recordgbx/<replay id>.
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
CSV_PATH = ROOT / "stm-info.csv"
COLUMNS = ["track", "site", "track_id", "mode", "stm", "rtm", "driver", "driver_id", "date", "replay_id", "download_url"]


def _parse(mode: str, s: str) -> int:
    if mode == "score":
        return int(s)
    parts = s.split(":")
    secs = float(parts[-1])
    mins = int(parts[-2]) if len(parts) >= 2 else 0
    return round((mins * 60 + secs) * 1000)


def _fmt(mode: str, v: int) -> str:
    if mode == "score":
        return str(v)
    m, rest = divmod(v, 60000)
    return f"{m}:{rest / 1000:05.2f}" if m else f"{rest / 1000:.2f}"


def download_url(site: str, replay_id: int) -> str:
    return f"https://{site}.exchange/recordgbx/{replay_id}"


def load(path: Path = CSV_PATH) -> dict:
    """track name -> {"set", "rtm", "wr": {"name", "uid", "date", "rid"} or None}"""
    out = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            mode = r["mode"]
            out[r["track"]] = {
                "set": _parse(mode, r["stm"]),
                "rtm": _parse(mode, r["rtm"]) if r["rtm"] else None,
                "wr": ({"name": r["driver"], "uid": int(r["driver_id"]), "date": r["date"], "rid": int(r["replay_id"])}
                       if r["replay_id"] else None),
            }
    return out


def attach(tracks: list, info: dict | None = None) -> list:
    """Fill `rtm` and `wr` on each track dict from the CSV. Returns the tracks the CSV does not cover
    (not in it, or the STM value in the track list has changed since)."""
    info = load() if info is None else info
    missing = []
    for t in tracks:
        row = info.get(t["name"])
        if row and row["set"] == t["set"] and row["rtm"] is not None:
            t["rtm"], t["wr"] = row["rtm"], row["wr"]
        else:
            missing.append(t)
    return missing


def fill(path: Path) -> bool:
    """Add rtm / wr to the tracks of a saved dataset JSON from the CSV (no downloads). True if the file changed."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    info, changed = load(), False
    for t in d["tracks"]:
        row = info.get(t["name"])
        if row and row["set"] == t["set"]:
            for key, value in (("rtm", row["rtm"]), ("wr", row["wr"])):
                if t.get(key) != value:
                    t[key], changed = value, True
    if changed:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    return changed


def _write(rows: dict, path: Path) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:      # BOM so Excel reads the names correctly
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows.values())
    os.replace(tmp, path)


def build(tracks: list, path: Path = CSV_PATH, delay: float = 0.3, everything: bool = False) -> list[str]:
    """Look up the author medal and the STM holder for the tracks missing from the CSV and add them.
    Returns the names added; makes no request at all when nothing is missing."""
    info = load(path)
    todo = list(tracks) if everything else attach(tracks, info)
    if not todo:
        return []
    import requests
    import count_beats as cb
    session = requests.Session()
    print(f"stm_info: looking up {len(todo)} track(s) on the exchange", file=sys.stderr)
    cb.fetch_rtm(session, todo)                      # sets t["rtm"]
    rows: dict = {}                                  # keep the existing rows and their order; new ones follow
    if Path(path).exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = {r[0]: r for r in csv.reader(f) if r and r[0] != "track"}
    for t in todo:
        score = t["mode"] == "score"
        replays = cb.fetch_replays(session, t, delay, ",ReplayAt")
        first = sorted((r for r in replays if (r.get("ReplayScore") if score else r.get("ReplayTime")) == t["set"]
                        and (r.get("User") or {}).get("UserId") is not None and r.get("ReplayAt")),
                       key=lambda r: (r["ReplayAt"], r["ReplayId"]))
        who = first[0] if first else None
        if who is None:
            print(f"  WARNING: no replay with exactly the STM found for {t['name']}", file=sys.stderr)
        rows[t["name"]] = [t["name"], t["site"], t["id"], t["mode"], _fmt(t["mode"], t["set"]),
                           "" if t.get("rtm") is None else _fmt(t["mode"], t["rtm"]),
                           who["User"].get("Name", "") if who else "", who["User"]["UserId"] if who else "",
                           who["ReplayAt"][:10] if who else "", who["ReplayId"] if who else "",
                           download_url(t["site"], who["ReplayId"]) if who else ""]
        time.sleep(delay)
    _write(rows, path)
    return [t["name"] for t in todo]


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Add the STM facts of any new track to stm-info.csv.")
    ap.add_argument("--all", action="store_true", help="look up every track again, not just the missing ones")
    args = ap.parse_args()
    import count_beats as cb
    import update                                    # the list of track files
    tracks = [t for job in update.JOBS for f, opts in job["tracks"] for t in cb.load_tracks(str(ROOT / f) + opts)]
    added = build(tracks, everything=args.all)
    print(f"stm-info.csv: {len(added)} track(s) looked up, {len(tracks) - len(added)} already in the file")


if __name__ == "__main__":
    main()

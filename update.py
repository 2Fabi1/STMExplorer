#!/usr/bin/env python3
"""
Refresh all STM data, detect NEW STMs since the last refresh, and rebuild the site.

What one run does:
  1. For every dataset (Nadeo / Star / Extension) and source (exchange replays / United Ladder)
     it runs count_beats.py into a temp file.
  2. Each successful dataset is compared with the previous snapshot: every (player, track)
     that now beats the STM but did not before is a new STM and is appended to data/events.json.
  3. data/meta.json (timestamps) is updated and data/index.html is rebuilt.

A dataset that fails to download keeps its previous data (so a hiccup never produces fake
"new" or "lost" STMs); it is listed under "failed" in meta.json and retried next run.

Everything lives in ./data (created on first use, seeded from the committed *.json snapshots).

Usage:
    python update.py                      # refresh everything now
    python update.py --only star          # just one dataset (ids: nadeo, star, extension)
    python update.py --source tmx         # just the exchange-replay source (tmx | ul)
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import build_site
import count_beats

ROOT = Path(__file__).parent
DATA = ROOT / "data"
LOGS = DATA / "logs"
TMP = DATA / "tmp"

# (track file, ":site:mode" options) lists per dataset; see count_beats.py for the format.
JOBS = [
    {"id": "nadeo", "label": "Nadeo tracks", "tracks": [("tracks.txt", "")]},
    {"id": "star", "label": "Star tracks", "tracks": [("tracks_star.txt", "")]},
    {"id": "extension", "label": "Extension", "tracks": [("tracks_ext_tmnf.txt", ":tmnf"),
                                                         ("tracks_ext_puzzle.txt", ""),
                                                         ("tracks_ext_stunt.txt", ":tmuf:score")]},
]
SOURCES = {"tmx": "", "ul": "_ul"}      # source -> json file suffix
MAX_PARALLEL = 1                        # one scraper at a time: steady and gentle on the hosts
REQUEST_DELAY = {"ul": 1.0, "tmx": 0.6}  # seconds waited after every request, per source
# UnitedLadder: every day each track is read down to the STM (about 1 page). Medal results (Super Bronze depth)
# need many more pages on busy tracks, so they are read gradually: each run reads the tracks with the oldest
# medal data until about this many extra pages are used, and keeps the earlier results for the rest.
DEEP_BUDGET_PAGES = 1000
DEFAULT_DEEP_PAGES = 25                 # page estimate for a track that has not been read deep yet
LOG_KEEP_DAYS = 14

log = logging.getLogger("stm")
_lock = threading.Lock()
_children: list = []                    # running subprocesses, so a shutdown can stop them


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def json_name(job_id: str, source: str) -> str:
    return f"{job_id}{SOURCES[source]}.json"


def data_files() -> list[Path]:
    """Dataset files in the order the site expects (all tmx first, then ul)."""
    return [DATA / json_name(j["id"], s) for s in SOURCES for j in JOBS if (DATA / json_name(j["id"], s)).exists()]


# ---------------------------------------------------------------- state / seeding

def ensure_state() -> dict:
    """Create ./data on first use from the committed snapshots; returns meta."""
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    seeded = []
    for job in JOBS:
        for source in SOURCES:
            name = json_name(job["id"], source)
            if not (DATA / name).exists() and (ROOT / name).exists():
                shutil.copy2(ROOT / name, DATA / name)
                seeded.append(name)
    meta = read_json(DATA / "meta.json", None)
    if meta is None:
        snap = max((p.stat().st_mtime for p in DATA.glob("*.json")), default=None)
        since = (datetime.fromtimestamp(snap, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                 if snap else None)
        meta = {"since": since, "updated": since, "failed": [], "last_run_events": 0}
        write_json(DATA / "meta.json", meta)
    if not (DATA / "events.json").exists():
        write_json(DATA / "events.json", [])
    if seeded:
        log.info("Seeded %s from the committed snapshots", ", ".join(seeded))
    if data_files() and not (DATA / "index.html").exists():
        rebuild_site()
    return meta


def rebuild_site() -> None:
    build_site.build(data_files(), out=DATA / "index.html",
                     events=read_json(DATA / "events.json", []), meta=read_json(DATA / "meta.json", {}))


# ---------------------------------------------------------------- diffing

def diff_events(old: dict, new: dict, source: str, label: str, ts: str) -> list[dict]:
    """New STMs: (player, track) pairs that beat the STM now but did not in `old`.

    Only tracks present in both snapshots with an unchanged STM value and mode are compared, so
    editing a track list never fakes a wave of achievements.
    """
    old_tracks = {t["name"]: t for t in old["tracks"]}
    new_tracks = {t["name"]: t for t in new["tracks"]}
    comparable = {n for n, t in new_tracks.items()
                  if n in old_tracks and old_tracks[n]["set"] == t["set"] and old_tracks[n].get("mode") == t.get("mode")}
    old_times = {p["id"]: p["times"] for p in old["players"]}
    events = []
    for p in new["players"]:
        before = old_times.get(p["id"], {})
        for track, value in p["times"].items():
            if track in comparable and track not in before:
                t = new_tracks[track]
                events.append({"t": ts, "source": source, "dataset": label, "id": p["id"], "player": p["name"],
                               "nick": p.get("nick"), "track": track, "value": value, "set": t["set"],
                               "mode": t.get("mode", "time"), "site": t.get("site"), "tid": t["id"]})
    return events


def event_key(e: dict) -> tuple:
    return (e["source"], e["dataset"], e["id"], e["track"])


# ---------------------------------------------------------------- running the scrapers

def run_job(job: dict, source: str, deep_names: list[str] | None = None) -> tuple[bool, Path]:
    """Run count_beats.py for one dataset/source into TMP. Returns (ok, output path)."""
    out = TMP / json_name(job["id"], source)
    out.unlink(missing_ok=True)
    specs = [str(ROOT / f) + opts for f, opts in job["tracks"]]
    cmd = [sys.executable, str(ROOT / "count_beats.py"), "--source", source, "--tracks", *specs,
           "--json", str(out), "--label", job["label"], "--fail-on-error", "--delay", str(REQUEST_DELAY[source])]
    if source == "ul":
        cmd += ["--depth", "stm"]
        if deep_names:
            cmd += ["--deep-tracks", *deep_names]
    logfile = LOGS / f"{datetime.now():%Y%m%d-%H%M}-{job['id']}{SOURCES[source]}.log"
    started = time.time()
    with open(logfile, "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=ROOT,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        _children.append(proc)
        try:
            code = proc.wait()
        finally:
            _children.remove(proc)
    ok = code == 0 and out.exists()
    log.info("%s %s: %s in %.0fs (log: %s)", job["id"], source, "ok" if ok else f"FAILED (exit {code})",
             time.time() - started, logfile.name)
    return ok, out


def stop_children() -> None:
    for proc in list(_children):
        proc.terminate()


def prune_logs() -> None:
    cutoff = time.time() - LOG_KEEP_DAYS * 86400
    for f in LOGS.glob("*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def carry_medals(old: dict, new: dict) -> None:
    """Keep what a light United Ladder read cannot see.

    For tracks that were not read down to Super Bronze this run, keep their earlier medal timestamp and each
    player's earlier medal-level results (dropped where the player has meanwhile beaten the STM). Tracks that
    were read deep carry their fresh results already.
    """
    old_tracks = {t["name"]: t for t in old["tracks"]}
    carried = {t["name"] for t in new["tracks"] if not t.get("medals_at") and old_tracks.get(t["name"], {}).get("medals_at")}
    for t in new["tracks"]:
        if t["name"] in carried:
            t["medals_at"] = old_tracks[t["name"]]["medals_at"]
            t["deep_pages"] = old_tracks[t["name"]].get("deep_pages")
    old_miss = {p["id"]: p.get("miss", {}) for p in old["players"]}
    for p in new["players"]:
        keep = {n: v for n, v in old_miss.get(p["id"], {}).items() if n in carried and n not in p["times"]}
        p["miss"] = {**keep, **p["miss"]}


def plan_deep_reads(jobs: list[dict], full: bool | None, budget: int) -> dict[str, list[str]]:
    """United Ladder tracks to read down to Super Bronze this run, per dataset id.

    Oldest (or never read) medal data first, until about `budget` pages are used; `full` forces all (True) or none (False).
    """
    cands = []   # (medals_at or "", order, job id, track, estimated pages)
    for job in jobs:
        old = read_json(DATA / json_name(job["id"], "ul"), None) or {"tracks": []}
        known = {t["name"]: t for t in old["tracks"]}
        for f, opts in job["tracks"]:
            for t in count_beats.load_tracks(str(ROOT / f) + opts):
                k = known.get(t["name"], {})
                cands.append((k.get("medals_at") or "", len(cands), job["id"], t["name"], k.get("deep_pages") or DEFAULT_DEEP_PAGES))
    if full is True:
        chosen = cands
    elif full is False:
        chosen = []
    else:
        chosen, spent = [], 0
        for c in sorted(cands):
            if chosen and spent + c[4] > budget:
                break
            chosen.append(c)
            spent += c[4]
    plan: dict[str, list[str]] = {}
    for _, _, job_id, name, _ in chosen:
        plan.setdefault(job_id, []).append(name)
    return plan


def run_update(only=None, sources=None, full=None, budget=DEEP_BUDGET_PAGES) -> dict | None:
    """One refresh. `full` forces (True) or forbids (False) deep United Ladder reads; None = the daily page budget.

    Returns a summary dict, or None if another update is already running."""
    if not _lock.acquire(blocking=False):
        log.warning("An update is already running; skipping")
        return None
    try:
        ensure_state()
        TMP.mkdir(exist_ok=True)
        prune_logs()
        jobs = [(j, s) for s in (sources or SOURCES) for j in JOBS if not only or j["id"] in only]
        plan = plan_deep_reads([j for j, s in jobs if s == "ul"], full, budget)
        log.info("Update started: %s | medal depth for %d United Ladder track(s) this run",
                 ", ".join(f"{j['id']}/{s}" for j, s in jobs), sum(len(v) for v in plan.values()))
        started = now_iso()
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            results = list(pool.map(lambda js: (js, *run_job(js[0], js[1], plan.get(js[0]["id"]))),
                                    sorted(jobs, key=lambda js: js[1] != "ul")))

        ts = now_iso()
        events = read_json(DATA / "events.json", [])
        seen = {event_key(e) for e in events}
        new_events, failed = [], []
        for (job, source), ok, tmp_path in results:
            name = json_name(job["id"], source)
            if not ok:
                failed.append(name)
                continue
            new = json.loads(tmp_path.read_text(encoding="utf-8"))
            old = read_json(DATA / name, None)
            if source == "ul" and old is not None:
                carry_medals(old, new)
                write_json(tmp_path, new)
            if old is not None:
                for e in diff_events(old, new, source, job["label"], ts):
                    if event_key(e) not in seen:
                        seen.add(event_key(e))
                        new_events.append(e)
            os.replace(tmp_path, DATA / name)

        events.extend(new_events)
        write_json(DATA / "events.json", events)
        meta = read_json(DATA / "meta.json", {})
        if len(failed) < len(results):
            meta["updated"] = ts
        meta.update({"failed": failed, "last_run_events": len(new_events), "last_run_started": started})
        write_json(DATA / "meta.json", meta)
        rebuild_site()
        log.info("Update finished: %d new STM(s), %d dataset(s) failed%s", len(new_events), len(failed),
                 f" ({', '.join(failed)})" if failed else "")
        return {"new_events": len(new_events), "failed": failed, "updated": meta.get("updated")}
    finally:
        _lock.release()


def is_updating() -> bool:
    return _lock.locked()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Refresh STM data and detect new STMs.")
    ap.add_argument("--only", nargs="+", choices=[j["id"] for j in JOBS], help="only these datasets")
    ap.add_argument("--source", nargs="+", choices=list(SOURCES), help="only these sources")
    depth = ap.add_mutually_exclusive_group()
    depth.add_argument("--full", action="store_true", help="read every United Ladder track down to Super Bronze now (many requests)")
    depth.add_argument("--light", action="store_true", help="read United Ladder only down to the STM (no medal reads this run)")
    ap.add_argument("--deep-budget", type=int, default=DEEP_BUDGET_PAGES, metavar="PAGES",
                    help=f"extra United Ladder pages to spend on medal data per run (default {DEEP_BUDGET_PAGES})")
    args = ap.parse_args()
    try:
        summary = run_update(args.only, args.source, True if args.full else False if args.light else None, args.deep_budget)
    except KeyboardInterrupt:
        stop_children()
        raise
    sys.exit(0 if summary and not summary["failed"] else 1)


if __name__ == "__main__":
    main()

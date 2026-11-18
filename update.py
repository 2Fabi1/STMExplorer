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
MAX_PARALLEL = 3                        # concurrent count_beats.py processes
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

def run_job(job: dict, source: str) -> tuple[bool, Path]:
    """Run count_beats.py for one dataset/source into TMP. Returns (ok, output path)."""
    out = TMP / json_name(job["id"], source)
    out.unlink(missing_ok=True)
    specs = [str(ROOT / f) + opts for f, opts in job["tracks"]]
    cmd = [sys.executable, str(ROOT / "count_beats.py"), "--source", source, "--tracks", *specs,
           "--json", str(out), "--label", job["label"], "--fail-on-error",
           "--delay", "0.3" if source == "ul" else "0.2"]
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


def run_update(only=None, sources=None) -> dict | None:
    """One full refresh. Returns a summary dict, or None if another update is already running."""
    if not _lock.acquire(blocking=False):
        log.warning("An update is already running; skipping")
        return None
    try:
        ensure_state()
        TMP.mkdir(exist_ok=True)
        prune_logs()
        jobs = [(j, s) for s in (sources or SOURCES) for j in JOBS if not only or j["id"] in only]
        log.info("Update started: %s", ", ".join(f"{j['id']}/{s}" for j, s in jobs))
        started = now_iso()
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            # long United Ladder jobs first so they overlap with the quick exchange ones
            results = list(pool.map(lambda js: (js, *run_job(*js)), sorted(jobs, key=lambda js: js[1] != "ul")))

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
    args = ap.parse_args()
    try:
        summary = run_update(args.only, args.source)
    except KeyboardInterrupt:
        stop_children()
        raise
    sys.exit(0 if summary and not summary["failed"] else 1)


if __name__ == "__main__":
    main()

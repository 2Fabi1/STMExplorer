#!/usr/bin/env python3
"""Embed the result JSON files (and optionally the new-STM event log) into template.html.

Usage:
    python build_site.py nadeo.json star.json ...            # -> index.html
    python build_site.py a.json b.json --out data/index.html --events data/events.json --meta data/meta.json
"""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent


def _embed(obj) -> str:
    # "</" would let a string close the <script> block early
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def build(files, out=None, events=None, meta=None) -> Path:
    """Render template.html with the given data files; returns the output path."""
    out = Path(out) if out else HERE / "index.html"
    data = [json.loads(Path(f).read_text(encoding="utf-8")) for f in files]
    now = datetime.now()
    html = (HERE / "template.html").read_text(encoding="utf-8")
    html = (html.replace("__GENERATED__", now.strftime("%Y-%m-%d %H:%M"))
                .replace("__BUILD__", now.strftime("%Y%m%d%H%M%S")))      # cache-buster for site-data.json
    # the data lives next to the page in site-data.json, which the page fetches when it opens
    site_data = out.with_name("site-data.json")
    for path, text in ((site_data, _embed({"data": data, "events": events or [], "meta": meta or {}})), (out, html)):
        tmp = path.with_name(path.name + ".tmp")    # write + rename so a server never serves half a file
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return out


def _read_json(path, default):
    if path and Path(path).exists():
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", default=["nadeo.json", "star.json"])
    ap.add_argument("--out", help="output html (default: index.html next to this script)")
    ap.add_argument("--events", help="events.json with detected new STMs")
    ap.add_argument("--meta", help="meta.json with update timestamps")
    args = ap.parse_args()
    out = build([HERE / f if not Path(f).is_absolute() else f for f in args.files], args.out,
                _read_json(args.events, []), _read_json(args.meta, {}))
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

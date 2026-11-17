#!/usr/bin/env python3
"""Embed the result JSON files into template.html -> index.html (self-contained).

Usage: python build_site.py nadeo.json star.json
"""
import json
import sys
from datetime import datetime
from pathlib import Path

here = Path(__file__).parent
files = sys.argv[1:] or ["nadeo.json", "star.json"]
data = [json.loads((here / f).read_text(encoding="utf-8")) for f in files]

blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
html = (here / "template.html").read_text(encoding="utf-8")
html = html.replace("__DATA__", blob).replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
(here / "index.html").write_text(html, encoding="utf-8")
print(f"Wrote {here / 'index.html'} ({len(html) // 1024} KB)")

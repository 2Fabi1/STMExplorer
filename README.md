# STM Stats

How many tracks each player has beaten the STM (set time) on, for TMUF and TMNF — from two sources:
replays uploaded to tmuf.exchange / tmnf.exchange, and the official in-game leaderboards via the
UnitedLadder API. The site also shows **who earned new STMs** (a feed of recent ones), detected by
comparing each daily refresh with the previous one.

## Run the server

```bash
pip install -r requirements.txt
python server.py                      # http://127.0.0.1:8080
```

On start it serves the last snapshot straight away and refreshes the data in the background whenever
it is more than 24 hours old, then every 24 hours after that. A refresh takes about 45 minutes, because it
deliberately goes slowly (see "Load on the hosts"); the site keeps serving the old data until it finishes.

| Option | Meaning |
|---|---|
| `--port 8080`, `--host 127.0.0.1` | where to listen (`--host 0.0.0.0` to allow other machines) |
| `--interval-hours 24` | refresh interval |
| `--no-update-on-start` | wait a full interval before the first refresh |

Served URLs (nothing else on disk is exposed): `/` the site, `/events.json` every detected new STM,
`/status` last/next update. The server speaks plain HTTP with no login; to publish it on the internet
put it behind a reverse proxy (Caddy, nginx) for HTTPS.

Prefer cron / Task Scheduler over a long-running process? Run `python update.py` once a day instead and
serve `data/index.html` any way you like.

## How "new STMs" work

* Each refresh downloads every dataset and compares it with the previous snapshot. A player/track pair
  that beats the STM now but did not before is a **new STM**, stored in `data/events.json` with the
  detection time. (The time is when a refresh noticed it, not when the player achieved it.)
* The **New STMs** tab lists the whole history, newest first.
* The **Merge** tab takes your United Ladder login and your exchange name, combines your times from both, and shows
  your progress towards every tag, which ones are closest, and the tracks you still need (with how far off you are).
* The first refresh compares against the snapshots committed in this repo, so it can report anything
  achieved since they were made.
* If a dataset fails to download it keeps its previous data and is retried (the server retries after
  1 hour), so a hiccup never produces fake "new" or "lost" STMs. Tracks whose STM value changed, and
  newly added tracks, are skipped in the comparison.

## Medals

Besides the STM, results are graded by **Super Gold, Super Silver and Super Bronze**: they lie 12.5%, 25% and 50% of
the way from the STM towards the track's author medal (RTM, read from the exchange API). The same formula works for
stunt scores. The Players breakdown groups results behind the STM by medal, and Achievements can be filtered by
STM / Super Gold / Super Silver / Super Bronze. Only players with at least one STM are listed.

## Load on the hosts

The scraper is built to go easy on tmuf/tmnf.exchange and the UnitedLadder API:

* **One request at a time**, with a pause after every request (0.6 s exchange, 1.0 s UnitedLadder) — about one request a second.
* **Cheap by default.** Each day every United Ladder track is read only down to the STM (about one page of 100 records).
  The much deeper read needed for medal results (some busy tracks need 100+ pages) is spread out: each run spends at
  most `DEEP_BUDGET_PAGES` (1000) extra pages on the tracks whose medal data is oldest and keeps the earlier results for
  the rest, so the daily load has a fixed ceiling. Medal data therefore fills in over the first couple of weeks
  (the page says how much is covered) and is then refreshed continuously.
* **Polite on trouble.** A `429 Too Many Requests` is answered by waiting for the `Retry-After` the host sends, retries
  back off, and a run stops after 3 tracks in a row fail instead of hammering a struggling host.

Knobs: `python update.py --deep-budget 500` (fewer medal pages per run), `--light` (no medal reads this run),
`--full` (read everything down to Super Bronze now — thousands of requests, use sparingly); delays and the budget are
constants at the top of `update.py`.

## Files

* `update.py` — one full refresh (`--only star`, `--source tmx` to limit it); `server.py` — site + scheduler
* `count_beats.py` — the scraper for one dataset; `build_site.py` + `template.html` — the page
* `tracks*.txt` — track lists (`name  map-uid  exchange-id  STM`); edit these to add tracks
* `*.json`, `results*.csv` — committed snapshots (seed data) from the manual runs
* `data/` (git-ignored) — live state: current snapshots, `events.json`, `meta.json`, `index.html`, `logs/`

## No server: GitHub Actions + Pages

`.github/workflows/update.yml` does the same daily refresh on GitHub's runners and publishes the site
to GitHub Pages, so nothing has to stay running. Setup, once:

1. Push the repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: "GitHub Actions".**
3. **Actions → "Update STM data" → Run workflow** to run it right away (otherwise it runs daily at 03:17 UTC).

State (snapshots and the event log) is kept on a single-commit `data` branch that is replaced each run,
so the history of `main` does not grow. If some datasets fail to download, the run still publishes what it
has and is marked failed so GitHub emails you; the next run retries. Runner minutes are free for public
repositories (a private repository's free quota is about 2,000 minutes a month, and a refresh takes about
an hour a day). GitHub may pause scheduled workflows after 60 days without repository activity.

## Keeping it running (own server)

* **Windows:** run `python server.py` in a terminal, or create a Task Scheduler task that starts it at
  logon/boot. The PC must stay awake for the refresh to run.
* **Linux server:** a systemd unit such as

  ```ini
  [Service]
  WorkingDirectory=/opt/stmcounter
  ExecStart=/usr/bin/python3 server.py --host 127.0.0.1 --port 8080
  Restart=always
  ```

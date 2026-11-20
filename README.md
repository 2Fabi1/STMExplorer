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
serve `data/index.html` and `data/site-data.json` (the page loads its data from that file, so it must be opened over http, not as a local file) any way you like.

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
the way from the STM towards the track's author medal (RTM, read from the exchange API), **rounded up** to the next
hundredth of a second (StadiumA1: STM 22.08 → 22.18 / 22.28 / 22.48 → RTM 22.87). The same formula works for
stunt scores (rounded up to a whole point). The Players breakdown groups results behind the STM by medal, and Achievements can be filtered by
STM / Super Gold / Super Silver / Super Bronze. Only players with at least one STM are listed.

## Tags

A player who has beaten **every STM** of a category gets that category's tag plaque (the game's own images from
players.trackmaniaforever.com/tags/71660–71675) next to their name in the Players, New STMs and Improvements views, and
the Merge tab lists the tags your merged account has earned. The 16 tags are United, Nations, Puzzle, Stunt; White, Green,
Blue, Red, Black; Stadium, Island, Desert, Rally, Bay, Coast, Snow. Only STMs count: Super Gold, Silver and Bronze never
earn a tag.

## Static STM info (`stm-info.csv`)

For every track this committed file holds the STM, the author medal (RTM) and **who set the STM, when, and the replay
download link** (`https://<site>.exchange/recordgbx/<replay id>`). The STM is the world record as it stood on
31 Dec 2015 on the exchange, so none of this ever changes; the scraper and the page read the file and **nothing is looked
up again** — not on a refresh, and not on a redeploy (the page gets the info filled into the saved data from the file, with no
network at all). The driver is the player of the earliest replay with exactly the STM result. Click a track in the Tracks tab
to see the medals, the driver, the date and the download.

Only a track that is missing from the file is ever looked up: `python stm_info.py` adds new tracks (the daily refresh does it
automatically), `python stm_info.py --all` redoes everything. Commit the file after adding tracks.

## The CSV

Every refresh also rewrites **`stm-data.csv`** — everything the site shows as one file, one row per player and track:
`source` (exchange / unitedladder), `category` (United / Nations / Puzzle / Stunt), `difficulty` and `environment`
(United only), `track`, `mode`, `stm`, `rtm` (author medal), `player`, `nickname`, `result`, `vs_stm` (how far behind the
STM: negative = better; seconds, or points for stunt) and `medal` (STM / Super Gold / Super Silver / Super Bronze /
Slower than Super Bronze). Times are in seconds; only players with at least one STM are included. It opens in Excel
(UTF-8 with BOM).

It is published next to the site (**Download all data (CSV)** in the footer, `/stm-data.csv`) and kept on the `data`
branch. `python update.py --export-csv` rewrites it from the saved data in a couple of seconds, without downloading anything.

## Speed and load

A refresh runs the datasets 3 at a time with short pauses between requests (0.2 s exchange, 0.3 s UnitedLadder) and reads
every United Ladder track down to Super Bronze each time. The safety nets stay: a `429 Too Many Requests` is answered by
waiting for the `Retry-After` the host sends, retries back off, and a run stops after 3 tracks in a row fail.
`python update.py --deep-budget N` caps the extra medal pages per run (the oldest medal data is refreshed first, the
rest is kept), `--light` skips the medal reads for a run; `MAX_PARALLEL`, `REQUEST_DELAY` and `DEEP_BUDGET_PAGES`
are constants at the top of `update.py`.

## Files

* `update.py` — one full refresh (`--only star`, `--source tmx` to limit it, `--export-csv`); `server.py` — site + scheduler
* `count_beats.py` — the scraper for one dataset; `build_site.py` + `template.html` — the page
* `tracks*.txt` — track lists (`name  map-uid  exchange-id  STM`); edit these to add tracks
* `*.json`, `results*.csv` — committed snapshots (seed data) from the manual runs
* `ducks.csv` (`track,duck`) — a duck time per track, faster than the STM (United tracks only for now). The Tracks view and the track popup show it, and Achievements has a Ducks column counting the tracks where a player is at or under the duck time. Edit the CSV and redeploy to change it.
* `data/` (git-ignored) — live state: current snapshots, `events.json`, `meta.json`, `index.html`, `stm-data.csv`, `logs/`

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

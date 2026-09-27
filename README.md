# ArmaHqStats

Hourly statistics on where Project Outbreak and every other Arma Reforger mod with
"zombie" in its name are running, rendered as a static page for GitHub Pages.

**Credit.** Server counts, player exposure and mod rankings come from the
[ReforgerMods.net](https://reforgermods.net) public API, an independent, community-run
Arma Reforger stats service. Per-mod version and server-list detail for the tracked
Project Outbreak add-ons comes from [ArmaHQ](https://www.armahq.com). This project is
not affiliated with ReforgerMods.net, ArmaHQ, or Bohemia Interactive. The generated page
credits both in its header and footer; keep that credit if you fork this.

## What it measures

ReforgerMods.net's public API (`api.reforgermods.net/v2`) reports, for every mod
currently deployed on any tracked server: how many servers run it and how many players
are on those servers. Paginating its `/v2/analytics/mods` endpoint once an hour gives a
complete, accurate picture across the whole fleet - no server-by-server enumeration
needed, and no risk of only seeing part of the population.

(An earlier version of this project scraped ArmaHQ's `/servers` page instead. ArmaHQ
redesigned that page to server-render only the first 50 of roughly 5,100 servers, so a
full aggregate could no longer be computed from it. ReforgerMods.net has no such limit
for mod-level aggregates, and its terms explicitly invite this kind of API use in place
of scraping.)

From that crawl the collector derives your tracked mods' numbers, a ranking of every mod
whose name contains the configured keywords (default `zombie`), and a global rank among
all mods in use. It then reads ArmaHQ's `/mods/{id}` page - once per tracked mod, a small
fixed set - for the concrete server list behind those numbers: which specific servers are
running it, which versions are in use, and (for the flagship mod) total slot capacity and
the busiest servers table. ReforgerMods.net has no bulk equivalent of that server-list
detail at the free tier, only single-server lookups, which don't scale to the whole fleet.

Both sources are used within their published rate limits and page-fetch norms; nothing
under either site's private `/api/` path is requested outside of ReforgerMods.net's own
documented, publicly-invited API.

## Layout

| path | purpose |
|---|---|
| `config.json` | tracked mod ids, keywords, request settings, page titles |
| `scraper/reforgermods_scrape.py` | fetch, parse, aggregate; writes `data/` |
| `site/build_site.py` | renders `docs/index.html` from `data/` |
| `run.py` | one scheduled run: scrape then build, log under `logs/` |
| `data/latest.json` | the current snapshot (tracked mods, keyword ranking, global top 50) |
| `data/history.jsonl` | one compact row per run (tracked and keyword mods), append-only |
| `data/mods-all-latest.json.gz` | every currently-deployed mod in the current snapshot |
| `docs/` | the GitHub Pages site (`index.html`, `latest.json`, `.nojekyll`) |

Standard library only; Python 3.9 or newer.

## Run it

```
python run.py            # scrape + build, exit code non-zero on failure
python scraper/reforgermods_scrape.py
python site/build_site.py
```

Open `docs/index.html` in a browser. The chart appears once two or more hourly snapshots
exist; sparklines and deltas grow with the history.

## Hourly on Windows

The scheduled task `ArmaHqStats hourly` runs `python run.py` every hour. Inspect or remove it:

```
schtasks /query /tn "ArmaHqStats hourly" /v
schtasks /delete /tn "ArmaHqStats hourly" /f
```

## Hourly on GitHub (when hosted on Pages)

`.github/workflows/scrape.yml` runs the same `run.py` on a cron every hour and commits
`data/` and `docs/`. Enable Pages for the repository with source "Deploy from a branch",
branch `main`, folder `/docs`. Give the workflow write permission for contents
(Settings, Actions, General, Workflow permissions). Stop the Windows task once the
workflow is running, or the two will interleave snapshots.

## Configuration

- `source.apiBase`: the ReforgerMods.net API base URL used for rankings and totals.
- `armahq`: ArmaHQ's site, used only for the tracked mods' per-server detail.
- `tracked`: Workshop ids with a label, add-on and channel; `flagship: true` marks the
  mod the hero section and the server table follow; `reserved: true` marks ids that are
  reserved on the Workshop but not published (they stay at zero).
- `keywords`: case-insensitive substrings matched against mod names for the ranking.
- `userAgent`: identifies the collector to both sites; put your repository URL in it.
- `requestGapSeconds`: pause between paginated ReforgerMods.net requests and between
  per-mod ArmaHQ requests, to stay comfortably inside published rate limits.
- `historyRowsOnPage`: how many hourly rows the page embeds (336 = two weeks).

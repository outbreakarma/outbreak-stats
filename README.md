# ArmaHqStats

Hourly statistics on where every Project Outbreak add-on and every other Arma Reforger mod
with "zombie" in its name are running, and how their Workshop downloads grow, rendered as a
static page for GitHub Pages: https://outbreakarma.github.io/outbreak-stats/

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

Both sources are used within their published rate limits. ReforgerMods.net is read through
its documented public API and its public `/mods/{id}/` pages. ArmaHQ is read through its
public `/mods/{id}` pages and, once per run for the flagship, its Workshop dependents index
(`/api/workshop/dependents/{id}`) for the "Built on" list.

Workshop download totals, ratings and versions for every tracked add-on and every keyword
mod come from ReforgerMods.net's per-mod endpoint (`/v2/mods/{id}`), one call each per run.
Only totals that changed are appended to `data/downloads.jsonl`. When the source serves a
record without its total, the previous snapshot's value is kept and flagged `carried`.

## Missed hours

GitHub's scheduler does not start a scheduled job reliably; for days at a time it has run
the hourly job only every four to eight hours. The workflow therefore has four cron slots
an hour, and `run.py` skips a run when the last snapshot is under 40 minutes old. Each run
also reads ReforgerMods.net's own hourly series for the tracked add-ons and the most-used
keyword mods (the 30-day charts on its `/mods/{id}/` pages) and fills every hour of the
last 29 days that has no snapshot. Each reconstructed day must reproduce that page's daily
table (last, peak and low) or the series is dropped. Filled rows carry `"backfilled": true`.
An add-on added to `tracked` later gets its earlier hours from the same series; those rows
list it under `"patched"`.

## Layout

| path | purpose |
|---|---|
| `config.json` | tracked mod ids, keywords, request settings, page titles |
| `scraper/reforgermods_scrape.py` | fetch, parse, aggregate; writes `data/` |
| `site/build_site.py` | renders `docs/index.html` from `data/` |
| `run.py` | one scheduled run: scrape then build, log under `logs/` |
| `data/latest.json` | the current snapshot (tracked mods, keyword ranking, global top 50) |
| `data/history.jsonl` | one compact row per hour (tracked and keyword mods), filled rows flagged |
| `data/downloads.jsonl` | Workshop download totals: one row per change, seeded from the main site's listing history since 2026-09-05 |
| `data/mods-all-latest.json.gz` | every currently-deployed mod in the current snapshot |
| `docs/` | the GitHub Pages site (`index.html`, `latest.json`, `trend.json`, `.nojekyll`) |
| `docs/trend.json` | the last 14 days, hourly, per tracked add-on, plus download history; the main site reads it |

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

`.github/workflows/scrape.yml` runs the same `run.py` four times an hour (the freshness
check turns three of them into no-ops) and commits `data/` and `docs/`. A manual run
(workflow_dispatch) passes `--force` and always scrapes. Enable Pages for the repository with source "Deploy from a branch",
branch `main`, folder `/docs`. Give the workflow write permission for contents
(Settings, Actions, General, Workflow permissions). Stop the Windows task once the
workflow is running, or the two will interleave snapshots.

## Configuration

- `source.apiBase`: the ReforgerMods.net API base URL used for rankings and totals.
- `armahq`: ArmaHQ's site, used only for the tracked mods' per-server detail.
- `tracked`: Workshop ids with a label, add-on, channel and display group; `flagship: true`
  marks the mod the hero section and the server table follow; `reserved: true` marks ids
  that are reserved on the Workshop but not published (they stay at zero). Their order is
  the order on the page.
- `groups`: the display groups of the tracked add-ons (id, label, one-line blurb).
- `keywords`: case-insensitive substrings matched against mod names for the ranking.
- `userAgent`: identifies the collector to both sites; put your repository URL in it.
- `requestGapSeconds`: pause between paginated ReforgerMods.net requests and between
  per-mod ArmaHQ requests, to stay comfortably inside published rate limits.
- `historyDaysOnPage`: how many days of hourly history the page embeds (30).
- `backfill`: `enabled`, `maxDays` (29, inside the source's 30-day window) and
  `keywordTop` (how many keyword mods to fill besides the tracked add-ons).
- `site`: page title and subtitle, chart and ranking sizes, and the main site, Discord
  and GitHub links shown in the header and the links section.

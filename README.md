# ArmaHqStats

Hourly statistics on where Project Outbreak and every other Arma Reforger mod with
"zombie" in its name are running, collected from **[ArmaHQ](https://www.armahq.com)**,
the Arma Reforger live server browser and statistics site, and rendered as a static page
for GitHub Pages.

**Credit.** All data comes from ArmaHQ (https://www.armahq.com). This project is not
affiliated with ArmaHQ or Bohemia Interactive. The generated page credits ArmaHQ in its
header and footer; keep that credit if you fork this.

## What it measures

ArmaHQ renders its live server list server-side, with every online server's full mod
list. One request per hour to that public page gives, for every mod:

- servers running it, and how many of those have players,
- players online on those servers (not subscribers), and slot capacity,
- the versions, regions and platforms in use,
- the busiest servers running it.

From that the collector derives your tracked mods' numbers, a ranking of every mod whose
name contains the configured keywords (default `zombie`), and a global rank among all
mods in use. Nothing under the site's `/api/` path is requested (its robots.txt disallows
that for automated clients), and no page is fetched more than once per run.

## Layout

| path | purpose |
|---|---|
| `config.json` | tracked mod ids, keywords, request settings, page titles |
| `scraper/armahq_scrape.py` | fetch, parse, aggregate; writes `data/` |
| `site/build_site.py` | renders `docs/index.html` from `data/` |
| `run.py` | one scheduled run: scrape then build, log under `logs/` |
| `data/latest.json` | the current snapshot (tracked mods, keyword ranking, global top 50) |
| `data/history.jsonl` | one compact row per run (tracked and keyword mods), append-only |
| `data/mods-all-latest.json.gz` | every mod seen in the current snapshot |
| `docs/` | the GitHub Pages site (`index.html`, `latest.json`, `.nojekyll`) |

Standard library only; Python 3.9 or newer.

## Run it

```
python run.py            # scrape + build, exit code non-zero on failure
python scraper/armahq_scrape.py --offline-html saved.html   # parse a saved page instead of fetching
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

- `tracked`: Workshop ids with a label, add-on and channel; `flagship: true` marks the
  mod the hero section and the server table follow; `reserved: true` marks ids that are
  reserved on the Workshop but not published (they stay at zero).
- `keywords`: case-insensitive substrings matched against mod names for the ranking.
- `userAgent`: identifies the collector to the site; put your repository URL in it.
- `historyRowsOnPage`: how many hourly rows the page embeds (336 = two weeks).

#!/usr/bin/env python3
"""Post a celebration card to Discord when the flagship passes a milestone.

Three numbers are watched, each against the thresholds in config.json "milestones":
Workshop downloads, servers running the mod, and players on those servers at once. When a
snapshot passes a threshold that was never announced, one message with a rendered card goes to
the DISCORD_MILESTONE_WEBHOOK channel. Passing several thresholds at once announces only the
highest; dropping below and rising again never repeats one.

data/milestones.json keeps the highest threshold announced per number. A number without an
entry starts at the highest threshold it already reached, without posting.

  python discord/milestones.py --check                 # writes due=true|false to $GITHUB_OUTPUT
  python discord/milestones.py                         # announce whatever is new
  python discord/milestones.py --preview downloads 30000 --out card.png   # render only
  python discord/milestones.py --backfill --dry-run    # list every milestone already reached
  python discord/milestones.py --backfill              # post a card for each, oldest first, once

A backfill card carries the day the threshold was actually first reached (read from
data/history.jsonl and data/downloads.jsonl) and the 30 days before it. It runs once:
data/milestones.json records "backfilled" afterwards, and the normal run never repeats it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import time
from pathlib import Path

from post_card import CONFIG_PATH, LATEST_PATH, ROOT, load_json, send

STATE_PATH = ROOT / "data" / "milestones.json"
HISTORY_PATH = ROOT / "data" / "history.jsonl"
DOWNLOADS_PATH = ROOT / "data" / "downloads.jsonl"
SPARK_DAYS = 30
MAX_T = dt.datetime.max.replace(tzinfo=dt.timezone.utc)

METRICS = {
    "downloads": {
        "label": "Workshop downloads",
        "headline": "{n} downloads!",
        "text": "Project Outbreak Zombies just passed {n} Workshop downloads. "
                "Thank you all for playing, hosting and sharing it!",
        "thanks": "Thank you for playing, hosting and sharing it.",
    },
    "servers": {
        "label": "Servers running it",
        "headline": "{n} servers!",
        "text": "Project Outbreak Zombies is now running on {n} Arma Reforger servers. "
                "Thanks to every server owner keeping the outbreak alive!",
        "thanks": "Thanks to every server owner keeping the outbreak alive.",
    },
    "players": {
        "label": "Players at once",
        "headline": "{n} players at once!",
        "text": "{n} survivors were on servers running Project Outbreak at the same time!",
        "thanks": "Survivors on servers running it, all at the same time.",
    },
}

CARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Barlow:wght@400;500;600&family=Share+Tech+Mono&display=swap">
<style>
:root{--ground:#080b09;--panel:#111812;--ink:#dbe4d3;--muted:#84957c;--line:#233126;--hazard:#f2c200;--hazard-ink:#ffe680;--radar:#6fd38d}
*{box-sizing:border-box}
body{margin:0;background:var(--ground)}
.card{width:1200px;padding:0 0 34px;color:var(--ink);font-family:'Barlow',system-ui,sans-serif;overflow:hidden;
  background:repeating-linear-gradient(0deg,rgba(255,255,255,.018) 0 1px,transparent 1px 3px),
    linear-gradient(rgba(111,211,141,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(111,211,141,.045) 1px,transparent 1px),
    radial-gradient(900px 500px at 15% -10%,#1c2a10 0%,transparent 60%),var(--ground);
  background-size:auto,48px 48px,48px 48px,auto,auto;border:2px solid var(--hazard)}
.bar{height:16px;background:repeating-linear-gradient(135deg,var(--hazard) 0 26px,#111 26px 52px);opacity:.9}
.top{display:flex;justify-content:space-between;padding:30px 48px 0;font-family:'Share Tech Mono',monospace;font-size:20px;letter-spacing:.22em;text-transform:uppercase;color:var(--hazard)}
.top span:last-child{color:var(--muted)}
.row{display:flex;justify-content:space-between;align-items:flex-end;margin-right:48px}
.big{font-family:'Bebas Neue',Impact,sans-serif;font-size:250px;line-height:.9;margin:28px 44px 0;color:var(--hazard-ink);text-shadow:0 0 50px rgba(242,194,0,.25)}
.rank{border:1px solid var(--line);background:rgba(17,24,18,.85);padding:18px 26px;margin-bottom:12px;text-align:right}
.rank b{display:block;font-family:'Bebas Neue',Impact,sans-serif;font-weight:400;font-size:96px;line-height:1;color:#eef3e8}
.rank span{font-family:'Share Tech Mono',monospace;font-size:20px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.what{font-family:'Share Tech Mono',monospace;font-size:30px;letter-spacing:.3em;text-transform:uppercase;margin:8px 48px 0;color:#eef3e8}
svg{display:block;margin:26px 48px 0}
.foot{display:flex;justify-content:space-between;align-items:baseline;margin:20px 48px 0;font-size:26px;color:var(--ink)}
.foot b{font-family:'Bebas Neue',Impact,sans-serif;font-weight:400;font-size:34px;letter-spacing:.06em;color:#eef3e8}
</style></head><body>
<div class="card">
  <div class="bar"></div>
  <div class="top"><span>&#9763; Project Outbreak Zombies &middot; milestone</span><span>__DATE__</span></div>
  <div class="row"><div class="big">__NUMBER__</div>__RANK__</div>
  <div class="what">__LABEL__</div>
  __SPARK__
  <div class="foot"><span>__THANKS__</span><b>THANK YOU</b></div>
</div></body></html>"""


def fmt(n: int) -> str:
    return f"{n:,}"


def flagship(latest: dict) -> dict:
    tracked = latest.get("tracked", {})
    return next((t for t in tracked.values() if t.get("flagship")), next(iter(tracked.values()), {}))


def current_values(latest: dict) -> dict:
    f = flagship(latest)
    values = {
        "downloads": (f.get("workshop") or {}).get("downloads"),
        "servers": f.get("servers"),
        "players": f.get("players"),
    }
    return {k: v for k, v in values.items() if isinstance(v, (int, float))}


def thresholds() -> dict:
    return {k: sorted(v) for k, v in load_json(CONFIG_PATH).get("milestones", {}).items() if k in METRICS}


def plan(state: dict, values: dict) -> tuple[dict, dict]:
    """Returns (new announcements {metric: threshold}, baselines for metrics without an entry)."""
    announced = state.get("announced", {})
    new, baselines = {}, {}
    for metric, steps in thresholds().items():
        value = values.get(metric)
        if value is None:
            continue
        reached = [t for t in steps if t <= value]
        if metric not in announced:
            baselines[metric] = reached[-1] if reached else 0
        elif reached and reached[-1] > announced[metric]:
            new[metric] = reached[-1]
    return new, baselines


def series(metric: str, mod_id: str, since: dt.datetime, until: dt.datetime | None = None) -> list[float]:
    points = []
    if metric == "downloads":
        last = None
        for line in DOWNLOADS_PATH.read_text(encoding="utf-8").splitlines() if DOWNLOADS_PATH.exists() else []:
            row = json.loads(line)
            last = row.get("d", {}).get(mod_id, last)
            if last is not None and since <= parse_t(row["t"]) <= (until or MAX_T):
                points.append(last)
    else:
        key = "s" if metric == "servers" else "p"
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines() if HISTORY_PATH.exists() else []:
            row = json.loads(line)
            v = row.get("tracked", {}).get(mod_id, {}).get(key)
            if v is not None and since <= parse_t(row["t"]) <= (until or MAX_T):
                points.append(v)
    return points


def parse_t(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def spark_svg(points: list[float], threshold: int, value: float) -> str:
    w, h = 1104, 150
    if points and points[-1] != value:
        points = points + [value]
    if len(points) < 2:
        return ""
    lo = min(min(points), threshold) * 0.97
    hi = max(max(points), threshold) * 1.02
    x = lambda i: i * w / (len(points) - 1)
    y = lambda v: h - (v - lo) / (hi - lo) * h if hi > lo else h / 2
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(points))
    ty = y(threshold)
    return (
        f'<svg width="{w}" height="{h + 6}" viewBox="0 -3 {w} {h + 6}">'
        f'<polygon points="0,{h} {line} {w},{h}" fill="rgba(242,194,0,.16)"/>'
        f'<polyline points="{line}" fill="none" stroke="#f2c200" stroke-width="4" stroke-linejoin="round"/>'
        f'<line x1="0" x2="{w}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="#6fd38d" stroke-width="2" stroke-dasharray="10 9"/>'
        f'<circle cx="{x(len(points) - 1):.1f}" cy="{y(points[-1]):.1f}" r="9" fill="#ffe680"/></svg>'
    )


def rank_html(latest: dict) -> str:
    rank, kw = flagship(latest).get("keywordRank"), latest.get("keyword", {})
    if not rank or not kw.get("modCount"):
        return ""
    label = html.escape(f"of {kw['modCount']} {kw.get('label', 'mods').lower()}")
    return f'<div class="rank"><b>#{rank}</b><span>{label}</span></div>'


def card_html(metric: str, threshold: int, value: float, when: dt.datetime, mod_id: str, latest: dict) -> str:
    m = METRICS[metric]
    spark = spark_svg(series(metric, mod_id, when - dt.timedelta(days=SPARK_DAYS), when), threshold, value)
    return (CARD_HTML
            .replace("__DATE__", when.strftime("%d %b %Y").lstrip("0"))
            .replace("__NUMBER__", fmt(threshold))
            .replace("__RANK__", rank_html(latest))
            .replace("__LABEL__", html.escape(m["label"]))
            .replace("__SPARK__", spark)
            .replace("__THANKS__", html.escape(m["thanks"])))


def render(card: str) -> bytes:
    from playwright.sync_api import sync_playwright

    channel = os.environ.get("CARD_BROWSER_CHANNEL", "chrome") or None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=channel)
        try:
            page = browser.new_page(viewport={"width": 1240, "height": 800}, device_scale_factor=1.5)
            page.set_content(card, wait_until="networkidle", timeout=60_000)
            page.evaluate("document.fonts.ready")
            return page.locator(".card").screenshot(type="png")
        finally:
            browser.close()


def message_text(metric: str, threshold: int, reached: dt.datetime | None = None) -> str:
    m = METRICS[metric]
    page_url = load_json(CONFIG_PATH).get("site", {}).get("statsPage", "https://outbreakarma.github.io/outbreak-stats/")
    n = fmt(threshold)
    since = f"Reached <t:{int(reached.timestamp())}:D> \u00b7 " if reached else ""
    return (
        f"\U0001f389 **{m['headline'].format(n=n)}** {m['text'].format(n=n)} \U0001f9df\n"
        f"-# {since}[Live stats](<{page_url}>)"
    )


def announce(webhook: str, metric: str, threshold: int, value: float, when: dt.datetime, mod_id: str, latest: dict,
             backfilled: bool = False) -> str:
    png = render(card_html(metric, threshold, value, when, mod_id, latest))
    filename = f"milestone-{metric}-{threshold}.png"
    payload = {
        "content": message_text(metric, threshold, when if backfilled else None),
        "allowed_mentions": {"parse": []},
        "attachments": [{"id": 0, "filename": filename}],
    }
    url = webhook.strip().split("?")[0].rstrip("/") + "?wait=true"
    status, msg = send("POST", url, payload, png, filename)
    if status != 200 or "id" not in msg:
        raise RuntimeError(f"Discord refused the {metric} milestone (HTTP {status}: {msg.get('message', '')})")
    return msg["id"]


def crossings(mod_id: str) -> list[tuple[dt.datetime, str, int, float]]:
    """(when, metric, threshold, value then) for the first snapshot at or above each threshold."""
    first: dict[tuple[str, int], tuple[dt.datetime, float]] = {}
    steps = thresholds()
    rows = [("downloads", "d", DOWNLOADS_PATH), ("servers", "s", HISTORY_PATH), ("players", "p", HISTORY_PATH)]
    for metric, key, path in rows:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            v = row.get("d", {}).get(mod_id) if metric == "downloads" else row.get("tracked", {}).get(mod_id, {}).get(key)
            if v is None:
                continue
            for th in steps.get(metric, []):
                if v >= th and (metric, th) not in first:
                    first[(metric, th)] = (parse_t(row["t"]), v)
    return sorted((w, m, th, v) for (m, th), (w, v) in first.items())


def backfill(webhook: str, latest: dict, state: dict, mod_id: str, dry_run: bool, out_dir: Path | None) -> int:
    """Post (or list, or render) a card for every threshold the flagship has already passed."""
    values = current_values(latest)
    found = [c for c in crossings(mod_id) if c[2] <= max(values.get(c[1], 0), c[3])]
    for when, metric, threshold, value in found:
        print(f"{when:%Y-%m-%d %H:%M} UTC  {METRICS[metric]['label']}: {fmt(threshold)}")
    if dry_run or out_dir:
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            for when, metric, threshold, value in found:
                (out_dir / f"backfill-{metric}-{threshold}.png").write_bytes(
                    render(card_html(metric, threshold, value, when, mod_id, {})))
            print(f"wrote {len(found)} cards to {out_dir}")
        return 0
    if state.get("backfilled"):
        print("the backfill already ran; nothing to do")
        return 0
    if not webhook:
        print("DISCORD_MILESTONE_WEBHOOK is not set; nothing to post")
        return 0
    announced, posts = dict(state.get("announced", {})), list(state.get("posts", []))
    done = {(p["metric"], p["threshold"]) for p in posts}
    failed = 0
    for when, metric, threshold, value in found:
        if (metric, threshold) in done:
            continue
        try:
            message_id = announce(webhook, metric, threshold, value, when, mod_id, {}, backfilled=True)
        except Exception as exc:  # keep going; a rerun posts only what is still missing
            print(f"{metric} {threshold}: {exc}")
            failed += 1
            continue
        announced[metric] = max(announced.get(metric, 0), threshold)
        posts.append({"metric": metric, "threshold": threshold, "value": value,
                      "snapshotUtc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "messageId": message_id, "backfilled": True})
        done.add((metric, threshold))
        print(f"posted {metric} {fmt(threshold)} (message {message_id})")
        time.sleep(1.5)
    STATE_PATH.write_text(json.dumps(dict(state, announced=announced, posts=posts, backfilled=not failed), indent=2) + "\n",
                          encoding="utf-8")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only report whether anything is due")
    ap.add_argument("--preview", nargs=2, metavar=("METRIC", "THRESHOLD"), help="render one card, no Discord")
    ap.add_argument("--out", type=Path, help="where --preview writes the PNG")
    ap.add_argument("--backfill", action="store_true", help="post a card for every milestone already reached, once")
    ap.add_argument("--dry-run", action="store_true", help="with --backfill: list them, post nothing")
    ap.add_argument("--out-dir", type=Path, help="with --backfill: render every card here, post nothing")
    args = ap.parse_args()

    latest = load_json(LATEST_PATH)
    values = current_values(latest)
    when = parse_t(latest["generatedUtc"]) if latest.get("generatedUtc") else dt.datetime.now(dt.timezone.utc)
    mod_id = flagship(latest).get("modId", "")
    webhook = os.environ.get("DISCORD_MILESTONE_WEBHOOK", "")
    state = load_json(STATE_PATH)
    new, baselines = plan(state, values)

    if args.backfill:
        return backfill(webhook, latest, state, mod_id, args.dry_run, args.out_dir)

    if args.preview:
        metric, threshold = args.preview[0], int(args.preview[1])
        out = args.out or Path(f"milestone-{metric}-{threshold}.png")
        out.write_bytes(render(card_html(metric, threshold, values.get(metric, threshold), when, mod_id, latest)))
        print(f"wrote {out}")
        print(message_text(metric, threshold))
        return 0

    if args.check:
        due = bool(webhook) and bool(new or baselines)
        print(f"milestones due: {new or 'none'}" + (f"; baselines to record: {baselines}" if baselines else ""))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"due={'true' if due else 'false'}\n")
        return 0

    if not webhook:
        print("DISCORD_MILESTONE_WEBHOOK is not set; nothing to post")
        return 0
    announced = dict(state.get("announced", {}), **baselines)
    posts = list(state.get("posts", []))
    failed = 0
    for metric, threshold in new.items():
        try:
            message_id = announce(webhook, metric, threshold, values[metric], when, mod_id, latest)
        except Exception as exc:  # one failed post must not block the others or lose the state
            print(f"{metric} {threshold}: {exc}")
            failed += 1
            continue
        announced[metric] = threshold
        posts.append({"metric": metric, "threshold": threshold, "value": values[metric],
                      "snapshotUtc": latest.get("generatedUtc"), "messageId": message_id})
        print(f"announced {metric} {fmt(threshold)} (message {message_id})")
    for metric, base in baselines.items():
        print(f"recorded {metric} baseline {fmt(base)} without posting")
    if announced != state.get("announced") or posts != state.get("posts", []):
        STATE_PATH.write_text(json.dumps(dict(state, announced=announced, posts=posts), indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

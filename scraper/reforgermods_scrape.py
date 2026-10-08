#!/usr/bin/env python3
"""One scheduled run: pull Reforger mod/server adoption stats from the reforgermods.net
public API, enrich the tracked mods with ArmaHQ's per-mod server list, then write
data/latest.json + data/history.jsonl. Exit code is non-zero on any failure.

reforgermods.net's /v2/analytics/mods (paginated, sort=deployments) gives every
currently-deployed mod's server count and player exposure across the whole tracked
fleet in one clean crawl - no server-by-server enumeration needed, and no risk of the
partial-page problem that broke the old ArmaHQ /servers scraper: ArmaHQ redesigned
/servers to server-render only the first 50 of ~5,100 servers, so a full aggregate
could no longer be computed from that page alone (see README history).

ArmaHQ's /mods/{id} page is still used, for the tracked mods only, because it is the
only source that gives a concrete list of servers running one specific mod (needed
for per-mod versions-in-use and the flagship's "busiest servers" table); reforgermods.net
has no bulk equivalent of that at the free tier (only single-server detail calls expose
a server's mod list, which does not scale to the whole fleet). Keeping that enrichment
to the ~12 tracked mods (rather than every keyword/global-ranking row) keeps ArmaHQ
traffic to a small, fixed set of ordinary page fetches each run.

GitHub's scheduler does not keep an hourly cron hourly: since early October it has fired
this job every four to eight hours. Each run therefore repairs the history it missed.
reforgermods.net's public mod page draws every mod's own hourly server and player series
for the last 30 days; the run reads that series for the tracked mods and the most-used
keyword mods, fills each missing hour as a row flagged "backfilled", and checks every
reconstructed day against the page's own daily table before it trusts a single value.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import html as htmllib
import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)
FIGURE_RE = re.compile(r'<figure class="TimeSeriesChart[^"]*">(.*?)</figure>', re.S)
DAY_ROW_RE = re.compile(
    r'<tr><th scope="row">([^<]+)</th><td>([^<]+)</td><td>([^<]+)</td><td>([^<]+)</td><td>([^<]+)</td></tr>'
)
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
HOUR = dt.timedelta(hours=1)


def log(msg: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def fetch_json(url: str, user_agent: str, timeout: int, retries: int) -> dict:
    """GET a JSON API response. Retries with backoff, honoring Retry-After on 429."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                log(f"fetched {url} status={resp.status} attempt={attempt}")
                return body
        except urllib.error.HTTPError as exc:
            last_error = exc
            wait = int(exc.headers.get("Retry-After") or 15 * attempt) if exc.code == 429 else 15 * attempt
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            wait = 15 * attempt
        log(f"fetch failed attempt={attempt} error={last_error!r}; waiting {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"could not fetch {url}: {last_error!r}")


def fetch_html(url: str, user_agent: str, timeout: int, retries: int) -> str:
    """GET an ArmaHQ page as text (used only for the tracked mods' server lists)."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                charset = resp.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                log(f"fetched {url} status={resp.status} bytes={len(raw)} attempt={attempt}")
                return text
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            wait = 15 * attempt
            log(f"fetch failed attempt={attempt} error={exc!r}; waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"could not fetch {url}: {last_error!r}")


def crawl_mod_analytics(api_base: str, user_agent: str, timeout: int, retries: int, request_gap: float) -> dict:
    """Every currently-deployed mod's server count and player exposure, across every
    page of /v2/analytics/mods (sort=deployments). ~30 requests at the current mod
    count; comfortably inside the published free-tier limits (60/min, 5,000/day)."""
    mods: dict[str, dict] = {}
    page = 1
    total_pages = 1
    while page <= total_pages:
        url = f"{api_base}/analytics/mods?sort=deployments&limit=500&page={page}"
        body = fetch_json(url, user_agent, timeout, retries)
        total_pages = body["meta"]["totalPages"]
        for row in body["data"]:
            mods[row["modId"]] = {
                "modId": row["modId"],
                "name": row["name"],
                "servers": int(row.get("totalDeployments") or 0),
                "players": int(row.get("currentPlayerExposure") or 0),
            }
        log(f"analytics/mods page {page}/{total_pages}: {len(body['data'])} rows")
        page += 1
        if page <= total_pages:
            time.sleep(request_gap)
    if not mods:
        raise RuntimeError("analytics/mods returned no rows; API shape may have changed")
    return mods


def fetch_armahq_mod_detail(mod_id: str, armahq_cfg: dict, user_agent: str, timeout: int, retries: int, top_n_servers: int) -> dict:
    """One mod's concrete server list, versions-in-use and capacity, from ArmaHQ's
    (unpaginated) /mods/{id} page."""
    url = armahq_cfg["modPage"].replace("{modId}", mod_id)
    html = fetch_html(url, user_agent, timeout, retries)
    chunks = CHUNK_RE.findall(html)
    decoder = json.JSONDecoder()
    for chunk in sorted(chunks, key=len, reverse=True):
        try:
            text = json.loads('"' + chunk + '"')
        except json.JSONDecodeError:
            continue
        start = text.find('{"modId"')
        if start < 0:
            continue
        obj, _end = decoder.raw_decode(text, start)
        if obj.get("modId") != mod_id or not isinstance(obj.get("servers"), list):
            continue
        servers = obj["servers"]
        servers_sorted = sorted(
            servers,
            key=lambda s: (-(s.get("playerCount") or 0), -(s.get("playerCountLimit") or 0), s.get("name") or ""),
        )
        top_servers = [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "players": s.get("playerCount") or 0,
                "limit": s.get("playerCountLimit") or 0,
                "region": s.get("region"),
                "version": s.get("modVersion"),
                "scenario": s.get("scenarioName"),
                "battlEye": bool(s.get("battlEye")),
                "password": bool(s.get("passwordProtected")),
                "official": bool(s.get("official")),
                "modCount": s.get("modCount"),
            }
            for s in servers_sorted[:top_n_servers]
        ]
        versions = collections.Counter()
        for v in obj.get("versions") or []:
            versions[str(v.get("version") or "?")] = v.get("servers") or 0
        servers_with_players = sum(1 for s in servers if (s.get("playerCount") or 0) > 0)
        return {
            "capacity": obj.get("totalCapacity") or 0,
            "serversWithPlayers": servers_with_players,
            "versions": dict(versions.most_common()),
            "topServers": top_servers,
        }
    raise ValueError(f"modId not found in ArmaHQ flight payload for {url}; page layout changed?")


def fetch_mod_detail(mod_id: str, api_base: str, user_agent: str, timeout: int, retries: int) -> dict:
    """One mod's own metadata from reforgermods.net's per-mod endpoint: downloads,
    subscribers and its declared dependency list. Used to refresh each "built on X"
    dependent's own numbers, not for the main per-run crawl."""
    body = fetch_json(f"{api_base}/mods/{mod_id}", user_agent, timeout, retries)
    return body["mod"]


def fetch_armahq_dependents(mod_id: str, armahq_cfg: dict, user_agent: str, timeout: int, retries: int) -> list[dict]:
    """Every mod ArmaHQ currently sees declaring mod_id as a Workshop dependency,
    from ArmaHQ's own dependents index (armahq.com/api/workshop/dependents/{id}).
    This is ArmaHQ's computed reverse-dependency answer, not a keyword search, so it
    finds a dependent regardless of what its own name happens to say."""
    url = armahq_cfg["dependentsUrl"].replace("{modId}", mod_id)
    body = fetch_json(url, user_agent, timeout, retries)
    return body["dependents"]


def _axis_number(text: str) -> float:
    """'250', '1,200', '1.5k' or '2M' from a chart axis label."""
    text = htmllib.unescape(text).strip().replace(",", "").replace(" ", "").replace(" ", "")
    scale = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}.get(text[-1:], 1)
    return float(text[:-1] if scale != 1 else text) * scale


def _day_label(label: str, today: dt.date) -> dt.date:
    """'18 Sept' -> the most recent such date on or before today (the page omits the year)."""
    day, month = htmllib.unescape(label).split()[:2]
    candidate = dt.date(today.year, MONTHS[month[:3].lower()], int(day))
    return candidate if candidate <= today else candidate.replace(year=today.year - 1)


def parse_reforgermods_series(page: str, today: dt.date) -> dict[str, dict[dt.datetime, int]]:
    """Hourly {'servers': {hour: n}, 'players': {hour: n}} from a reforgermods.net mod page.

    The page draws each series as an SVG path (one vertex per hourly observation, x spread over
    the window, y on a 0-200 box scaled to the first y-axis label) and lists a daily table under
    it (last, peak, low and observation count per UTC day). The table is what makes this safe:
    observation counts assign each vertex to its day, full 24-observation days fix the x-to-hour
    scale, and every reconstructed day must reproduce the table's last, peak and low values.
    Any mismatch raises, so a redesign of the page drops the backfill instead of corrupting it.
    """
    out: dict[str, dict[dt.datetime, int]] = {}
    for fig in FIGURE_RE.findall(page):
        cap = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", fig, re.S)
        caption = htmllib.unescape(cap.group(1)).lower() if cap else ""
        key = "servers" if caption.startswith("servers") else "players" if "players" in caption else None
        if key is None or key in out:
            continue
        yaxis = re.search(r'__yaxis"[^>]*>(.*?)</div>', fig, re.S)
        line = re.search(r'<path d="([^"]+)" class="[^"]*__line', fig)
        days = DAY_ROW_RE.findall(fig)
        if not (yaxis and line and days):
            raise ValueError(f"{key}: chart, axis or daily table not found")
        ymax = _axis_number(re.findall(r"<span>([^<]*)</span>", yaxis.group(1))[0])
        verts = [(float(x), float(y)) for x, y in re.findall(r"[ML]\s*(-?[\d.]+)[ ,](-?[\d.]+)", line.group(1))]
        counts = [int(d[4]) for d in days]
        if sum(counts) != len(verts):
            raise ValueError(f"{key}: {len(verts)} vertices but the daily table counts {sum(counts)}")
        dates = [_day_label(d[0], today) for d in days]
        vert_dates = [dates[i] for i, n in enumerate(counts) for _ in range(n)]

        # Fit x = a + b*hours on full days, where the k-th observation of the day is hour k.
        epoch = dt.datetime.combine(dates[0], dt.time(), dt.timezone.utc)
        known = []
        start = 0
        for day, n in zip(dates, counts):
            if n == 24:
                base = (dt.datetime.combine(day, dt.time(), dt.timezone.utc) - epoch) / HOUR
                known.extend((base + k, verts[start + k][0]) for k in range(24))
            start += n
        # One full day fixes the scale; the per-day check below still rejects any misfit,
        # so add-ons younger than two days are covered too.
        if len(known) < 24:
            raise ValueError(f"{key}: no full day to fix the time scale")
        mh = sum(h for h, _ in known) / len(known)
        mx = sum(x for _, x in known) / len(known)
        b = sum((h - mh) * (x - mx) for h, x in known) / sum((h - mh) ** 2 for h, _ in known)
        a = mx - b * mh

        series: dict[dt.datetime, int] = {}
        tol = max(0, math.ceil(ymax / 4000))
        last_hour = None
        for (x, y), day in zip(verts, vert_dates):
            hour = epoch + round((x - a) / b) * HOUR
            if hour.date() != day or (last_hour is not None and hour <= last_hour):
                raise ValueError(f"{key}: vertex at x={x} does not land in its own day ({hour} vs {day})")
            last_hour = hour
            series[hour] = max(0, round((200 - y) / 200 * ymax))
        start = 0
        for d, day in zip(days, dates):
            vals = [series[h] for h in sorted(series) if h.date() == day]
            want_last, want_peak, want_low = (int(_axis_number(v)) for v in d[1:4])
            if abs(vals[-1] - want_last) > tol or abs(max(vals) - want_peak) > tol or abs(min(vals) - want_low) > tol:
                raise ValueError(f"{key}: {day} reconstructs {vals[-1]}/{max(vals)}/{min(vals)}, table says {want_last}/{want_peak}/{want_low}")
        out[key] = series
    if "servers" not in out:
        raise ValueError("no servers series on the page")
    return out


def _hour_floor(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _parse_t(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def backfill_history(history_path: Path, cfg: dict, keyword_ids: list[str], names: dict[str, str], now: dt.datetime) -> int:
    """Fill hours the scheduler skipped, from reforgermods.net's own hourly series.

    Only hours inside the source's 30-day window, after the first stored row and with no row of
    their own are filled, and only when the flagship has a value for that hour, so a filled row
    never shows the flagship falling to zero. Rows are flagged "backfilled" and the file is
    rewritten in time order. Existing rows from before an add-on joined the tracked list get that
    add-on's value for their hour too, and list it under "patched" (with or without a value) so it
    is looked up only once. Returns the number of rows added or patched; failures are logged,
    never fatal.
    """
    bf = cfg.get("backfill") or {}
    if not bf.get("enabled", True) or not history_path.exists():
        return 0
    rows = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        return 0
    have = {_hour_floor(_parse_t(r["t"])) for r in rows}
    first = _hour_floor(min(_parse_t(r["t"]) for r in rows))
    window_start = max(first, _hour_floor(now) - dt.timedelta(days=int(bf.get("maxDays", 29))))
    missing = []
    hour = window_start
    while hour < _hour_floor(now):
        if hour not in have:
            missing.append(hour)
        hour += HOUR

    tracked = [item["modId"] for item in cfg.get("tracked", [])]
    flagship = next((item["modId"] for item in cfg.get("tracked", []) if item.get("flagship")), tracked[0] if tracked else None)
    kw_top = keyword_ids[: int(bf.get("keywordTop", 10))]
    in_window = [r for r in rows if _parse_t(r["t"]) >= window_start]
    unpatched = {m for r in in_window for m in tracked if m not in r.get("tracked", {}) and m not in r.get("patched", [])}
    if not missing and not unpatched:
        log("backfill: no missing hours")
        return 0
    wanted = list(dict.fromkeys(([flagship] if flagship else []) + tracked + kw_top)) if missing else sorted(unpatched)
    ua, timeout, retries = cfg["userAgent"], int(cfg["requestTimeoutSeconds"]), int(cfg["retries"])
    gap = float(cfg.get("requestGapSeconds", 1.1))
    page_url = cfg["source"]["modPage"]
    series: dict[str, dict[str, dict[dt.datetime, int]]] = {}
    for mod_id in wanted:
        try:
            series[mod_id] = parse_reforgermods_series(fetch_html(page_url.replace("{modId}", mod_id), ua, timeout, retries), now.date())
        except Exception as exc:  # noqa: BLE001 - one unreadable page only narrows the backfill
            log(f"backfill: series for {mod_id} unusable, skipped: {exc!r}")
            if mod_id == flagship and missing:
                log("backfill: no flagship series, no hours filled this run")
                missing = []
        time.sleep(gap)

    patched = 0
    for r in in_window:
        hour = _hour_floor(_parse_t(r["t"]))
        for mod_id in sorted(unpatched):
            if mod_id in r.get("tracked", {}) or mod_id in r.get("patched", []) or mod_id not in series:
                continue
            s = series[mod_id]
            if hour in s["servers"]:
                r.setdefault("tracked", {})[mod_id] = {"s": s["servers"][hour], "p": s.get("players", {}).get(hour, 0)}
            r.setdefault("patched", []).append(mod_id)
            patched += 1

    added = []
    for hour in missing:
        if hour not in series.get(flagship, {}).get("servers", {}):
            continue
        row = {"t": hour.isoformat().replace("+00:00", "Z"), "backfilled": True,
               "totals": {"servers": None, "players": None, "uniqueMods": None}, "tracked": {}, "keyword": {}}
        for mod_id, s in series.items():
            if hour not in s["servers"]:
                continue
            point = {"s": s["servers"][hour], "p": s.get("players", {}).get(hour, 0)}
            if mod_id in tracked:
                row["tracked"][mod_id] = point
            if mod_id in kw_top:
                row["keyword"][mod_id] = {**point, "n": names.get(mod_id, mod_id)}
        added.append(row)
    if added or patched:
        rows.extend(added)
        rows.sort(key=lambda r: _parse_t(r["t"]))
        tmp = history_path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(history_path)
    log(f"backfill: {len(missing)} missing hours, {len(added)} filled, {patched} add-on hours patched, from {len(series)} reforgermods.net series")
    return len(added) + patched


def append_downloads(path: Path, now: dt.datetime, downloads: dict[str, int]) -> None:
    """Append the Workshop download totals that changed since the stored state.

    Each row holds only the mods whose total moved (the Workshop updates totals about once a
    day, at a different time for each mod), so readers carry every value forward from the
    previous rows. The first rows, seeded from the Project Outbreak site's listing history,
    hold the full set of its add-ons.
    """
    if not downloads:
        return
    state: dict[str, int] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                state.update(json.loads(line)["d"])
    changed = {k: v for k, v in downloads.items() if state.get(k) != v}
    if not changed:
        return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": now.isoformat().replace("+00:00", "Z"), "d": changed}) + "\n")


def run(config_path: Path, data_dir: Path) -> int:
    cfg = load_config(config_path)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    data_dir.mkdir(parents=True, exist_ok=True)

    api_base = cfg["source"]["apiBase"]
    ua = cfg["userAgent"]
    timeout = int(cfg["requestTimeoutSeconds"])
    retries = int(cfg["retries"])
    request_gap = float(cfg.get("requestGapSeconds", 1.1))
    top_n_servers = int(cfg["site"].get("topServersPerMod", 12))
    keywords = [k.lower() for k in cfg.get("keywords", ["zombie"])]

    site_totals = fetch_json(f"{api_base}/analytics", ua, timeout, retries)["data"]
    totals = {
        "servers": site_totals["onlineServers"],
        "players": site_totals["totalPlayers"],
        "uniqueMods": site_totals["uniqueModsDeployed"],
    }
    log(f"totals: servers={totals['servers']} players={totals['players']} uniqueMods={totals['uniqueMods']}")
    time.sleep(request_gap)

    mods = crawl_mod_analytics(api_base, ua, timeout, retries, request_gap)

    ranked_ids = sorted(mods, key=lambda m: (-mods[m]["servers"], -mods[m]["players"], mods[m]["name"].lower()))
    global_rank = {mod_id: i + 1 for i, mod_id in enumerate(ranked_ids)}

    keyword_ids = [m for m in ranked_ids if any(k in mods[m]["name"].lower() for k in keywords)]
    keyword_servers_total = sum(mods[m]["servers"] for m in keyword_ids)
    keyword_players_total = sum(mods[m]["players"] for m in keyword_ids)
    keyword_rank = {mod_id: i + 1 for i, mod_id in enumerate(keyword_ids)}

    def describe(mod_id: str) -> dict:
        m = mods[mod_id]
        return {
            "modId": mod_id,
            "name": m["name"],
            "servers": m["servers"],
            "players": m["players"],
            "versions": {},
            "globalRank": global_rank[mod_id],
            "keywordRank": keyword_rank.get(mod_id),
            "shareOfKeywordServers": round(m["servers"] / keyword_servers_total, 4) if keyword_servers_total else None,
        }

    tracked_out = {}
    for item in cfg.get("tracked", []):
        mod_id = item["modId"]
        base = {k: v for k, v in item.items()}
        if mod_id in mods:
            base.update(describe(mod_id))
            base["seen"] = True
        else:
            base.update(
                {"seen": False, "servers": 0, "players": 0, "versions": {}, "globalRank": None, "keywordRank": None, "shareOfKeywordServers": None}
            )
        tracked_out[mod_id] = base

    # Enrich the tracked mods (a small, fixed set) with ArmaHQ's per-mod server list:
    # versions-in-use for every tracked card, plus capacity/serversWithPlayers/topServers
    # for the flagship's dashboard section. Best-effort: a failure here does not fail the run.
    for entry in tracked_out.values():
        if not entry["seen"]:
            continue
        try:
            detail = fetch_armahq_mod_detail(entry["modId"], cfg["armahq"], ua, timeout, retries, top_n_servers)
            entry["versions"] = detail["versions"]
            if entry.get("flagship"):
                entry["capacity"] = detail["capacity"]
                entry["serversWithPlayers"] = detail["serversWithPlayers"]
                entry["topServers"] = detail["topServers"]
            log(f"ArmaHQ detail for {entry['modId']} ({entry.get('label')}): {len(detail['topServers'])} servers in list")
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort, never fatal
            log(f"ArmaHQ detail failed for {entry['modId']} (non-fatal, keeping reforgermods numbers only): {exc!r}")
            if entry.get("flagship"):
                entry.setdefault("capacity", 0)
                entry.setdefault("serversWithPlayers", 0)
                entry.setdefault("topServers", [])
        time.sleep(request_gap)

    # Workshop numbers (downloads, subscribers, rating, current version) for every tracked mod and
    # every keyword mod, from reforgermods.net's cached copy of each Workshop listing. One detail
    # call per mod (~55 at the current keyword count, well inside the 60/min and 5,000/day limits).
    # Best-effort, like ArmaHQ above: a failed call only leaves that mod without Workshop numbers.
    # reforgermods.net sometimes serves a mod record without its download total or analytics
    # while it refreshes that mod. Such a gap keeps the previous snapshot's value, flagged
    # "carried", so a chart never shows a total dropping to nothing for one hour.
    try:
        prev = json.loads((data_dir / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    prev_workshop = {m: e.get("workshop") or {} for m, e in prev.get("tracked", {}).items()}
    for m in prev.get("keyword", {}).get("mods", []):
        prev_workshop.setdefault(m["modId"], {"downloads": m.get("downloads")})
    workshop: dict[str, dict] = {}
    for mod_id in dict.fromkeys(list(tracked_out) + keyword_ids):
        try:
            m = fetch_mod_detail(mod_id, api_base, ua, timeout, retries)
            workshop[mod_id] = {
                "downloads": m.get("downloadCount"),
                "subscribers": m.get("subscriberCount"),
                "rating": m.get("rating"),
                "ratingCount": m.get("ratingCount"),
                "version": m.get("version"),
                "updatedAt": m.get("updatedAt"),
            }
            if mod_id in tracked_out:
                a = m.get("analytics") or {}
                workshop[mod_id].update({
                    "summary": m.get("summary"),
                    "createdAt": m.get("createdAt"),
                    "sizeBytes": m.get("totalSize") or m.get("size"),
                    "dependencies": [{"id": d.get("id"), "name": d.get("name")} for d in (m.get("dependencies") or [])],
                    "rootDeployments": a.get("confirmedRootDeployments"),
                    "dependencyDeployments": a.get("dependencyDeployments"),
                    "playerHours7d": round(a["playerHoursExposure7d"]) if isinstance(a.get("playerHoursExposure7d"), (int, float)) else None,
                })
        except Exception as exc:  # noqa: BLE001 - Workshop numbers are best-effort, never fatal
            log(f"Workshop detail failed for {mod_id} (non-fatal): {exc!r}")
        time.sleep(request_gap)
    carried = 0
    for mod_id, w in workshop.items():
        old = prev_workshop.get(mod_id, {})
        for key in ("downloads", "rootDeployments", "dependencyDeployments", "playerHours7d"):
            if key in w and w[key] is None and old.get(key) is not None:
                w[key] = old[key]
                w.setdefault("carried", []).append(key)
                carried += key == "downloads"
    if carried:
        log(f"Workshop detail: {carried} download totals missing from the source, kept from the previous snapshot")
    for mod_id, entry in tracked_out.items():
        if mod_id in workshop:
            entry["workshop"] = workshop[mod_id]
    downloads = {m: w["downloads"] for m, w in workshop.items() if isinstance(w.get("downloads"), int)}
    log(f"Workshop detail: {len(workshop)} mods, {len(downloads)} with a download total")

    # "Built on X" list: ArmaHQ's own Workshop dependency index names every mod that
    # currently declares the flagship as a dependency - a real reverse-dependency
    # answer, not a keyword guess. First-party Project Outbreak add-ons are dropped
    # since they already have their own section above; each remaining community
    # dependent's own downloads/servers/players is then refreshed from reforgermods.net.
    dependents_cfg = cfg.get("dependents")
    dependents_out = None
    if dependents_cfg and dependents_cfg.get("targetModId"):
        target_id = dependents_cfg["targetModId"]
        try:
            all_dependents = fetch_armahq_dependents(target_id, cfg["armahq"], ua, timeout, retries)
        except Exception as exc:  # noqa: BLE001 - dependents section is best-effort, never fatal
            log(f"dependents: ArmaHQ dependents lookup failed for {target_id}, skipping section: {exc!r}")
            all_dependents = []
        time.sleep(request_gap)
        community = [d for d in all_dependents if d["modId"] not in tracked_out]

        items = []
        for cand in community:
            try:
                m = fetch_mod_detail(cand["modId"], api_base, ua, timeout, retries)
                analytics = m.get("analytics") or {}
                items.append(
                    {
                        "modId": m["id"],
                        "name": m.get("name") or cand["name"],
                        "author": m.get("author"),
                        "downloads": int(m.get("downloadCount") or 0),
                        "subscribers": int(m.get("subscriberCount") or 0),
                        "servers": int(analytics.get("totalDeployments") or 0),
                        "players": int(analytics.get("currentPlayerExposure") or 0),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one bad dependent should not fail the run
                log(f"dependents: fetch failed for {cand['modId']} ({cand.get('name')}), skipping: {exc!r}")
            time.sleep(request_gap)
        items.sort(key=lambda r: -r["downloads"])
        dependents_out = {
            "targetModId": target_id,
            "heading": dependents_cfg.get("heading"),
            "top": int(dependents_cfg.get("top", 5)),
            "discoveryMethod": dependents_cfg.get("discoveryMethod"),
            "totalDependents": len(all_dependents),
            "communityDependents": len(community),
            "items": items,
        }
        log(
            f"dependents: {len(items)}/{len(community)} community dependents refreshed "
            f"({len(all_dependents)} total from ArmaHQ, {len(all_dependents) - len(community)} first-party excluded)"
        )

    latest = {
        "generatedUtc": now.isoformat().replace("+00:00", "Z"),
        "source": cfg["source"],
        "totals": totals,
        "keyword": {
            "terms": keywords,
            "label": cfg.get("keywordLabel", "Keyword mods"),
            "modCount": len(keyword_ids),
            "serversTotal": keyword_servers_total,
            "playersTotal": keyword_players_total,
            "mods": [{**describe(m), "downloads": (workshop.get(m) or {}).get("downloads")} for m in keyword_ids],
        },
        "tracked": tracked_out,
        "globalTop": [describe(m) for m in ranked_ids[:50]],
        "dependents": dependents_out,
    }

    latest_path = data_dir / "latest.json"
    tmp = latest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(latest_path)

    # Compact per-run history row: tracked mods and the keyword set only.
    row = {
        "t": latest["generatedUtc"],
        "totals": totals,
        "tracked": {m: {"s": v["servers"], "p": v["players"]} for m, v in tracked_out.items()},
        "keyword": {m: {"s": mods[m]["servers"], "p": mods[m]["players"], "n": mods[m]["name"]} for m in keyword_ids},
    }
    with (data_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    append_downloads(data_dir / "downloads.jsonl", now, downloads)
    try:
        backfill_history(data_dir / "history.jsonl", cfg, keyword_ids, {m: mods[m]["name"] for m in keyword_ids}, now)
    except Exception as exc:  # noqa: BLE001 - a failed repair leaves the native rows as they are
        log(f"backfill failed (non-fatal): {exc!r}")

    # Every currently-deployed mod, compact, gzipped, for later analysis.
    all_mods = [
        {"modId": mods[m]["modId"], "name": mods[m]["name"], "servers": mods[m]["servers"], "players": mods[m]["players"]}
        for m in ranked_ids
    ]
    with gzip.open(data_dir / "mods-all-latest.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"generatedUtc": latest["generatedUtc"], "mods": all_mods}, fh, ensure_ascii=False)

    flagship = next((v for v in tracked_out.values() if v.get("flagship")), None)
    if flagship:
        log(f"flagship {flagship['label']}: servers={flagship['servers']} players={flagship['players']} keywordRank={flagship.get('keywordRank')} globalRank={flagship.get('globalRank')}")
    log(f"{latest['keyword']['label']}: {len(keyword_ids)} mods on {keyword_servers_total} servers with {keyword_players_total} players")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args(argv)
    try:
        return run(args.config, args.data_dir)
    except Exception as exc:  # noqa: BLE001 - the scheduler needs a non-zero exit and a reason
        log(f"FAILED: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

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
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.S)


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
            "mods": [describe(m) for m in keyword_ids],
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

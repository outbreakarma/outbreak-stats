#!/usr/bin/env python3
"""Collect Arma Reforger mod adoption statistics from ArmaHQ's public server list.

ArmaHQ (https://www.armahq.com) renders its live server browser server-side: the
/servers page embeds every online server with its full mod list. One polite request
per run gives, for every mod, how many servers run it, how many players are on
those servers, and which versions are in use. Nothing under /api/ is requested
(the site's robots.txt disallows it for automated clients), no page is fetched more
than once per run, and the site is credited on every page this project generates.

Standard library only, so it runs on a bare Python 3.9+ and inside GitHub Actions.
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


def fetch(url: str, user_agent: str, timeout: int, retries: int) -> str:
    """GET a page as text. Retries with backoff; raises on final failure."""
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


def extract_servers(html: str) -> list[dict]:
    """Pull the initialServers array out of the Next.js flight payload."""
    chunks = CHUNK_RE.findall(html)
    if not chunks:
        raise ValueError("no Next.js flight chunks found in page; site layout changed?")
    decoder = json.JSONDecoder()
    for chunk in sorted(chunks, key=len, reverse=True):
        try:
            text = json.loads('"' + chunk + '"')
        except json.JSONDecodeError:
            continue
        start = text.find('{"initialServers"')
        if start < 0:
            continue
        obj, _end = decoder.raw_decode(text, start)
        servers = obj.get("initialServers")
        if isinstance(servers, list):
            return servers
    raise ValueError("initialServers not found in any flight chunk; site layout changed?")


def aggregate(servers: list[dict]) -> tuple[dict, dict]:
    """Per-mod aggregates plus site-wide totals."""
    mods: dict[str, dict] = {}
    totals = {"servers": len(servers), "players": 0, "capacity": 0, "serversWithPlayers": 0, "uniqueMods": 0}
    for server in servers:
        players = int(server.get("playerCount") or 0)
        limit = int(server.get("playerCountLimit") or 0)
        totals["players"] += players
        totals["capacity"] += limit
        if players > 0:
            totals["serversWithPlayers"] += 1
        seen_in_server: set[str] = set()
        for mod in server.get("mods") or []:
            mod_id = mod.get("modId")
            if not mod_id or mod_id in seen_in_server:
                continue
            seen_in_server.add(mod_id)
            entry = mods.get(mod_id)
            if entry is None:
                entry = mods[mod_id] = {
                    "modId": mod_id,
                    "names": collections.Counter(),
                    "servers": 0,
                    "players": 0,
                    "capacity": 0,
                    "serversWithPlayers": 0,
                    "versions": collections.Counter(),
                    "regions": collections.Counter(),
                    "platforms": collections.Counter(),
                    "official": 0,
                    "passworded": 0,
                    "topServers": [],
                }
            entry["names"][mod.get("name") or ""] += 1
            entry["servers"] += 1
            entry["players"] += players
            entry["capacity"] += limit
            if players > 0:
                entry["serversWithPlayers"] += 1
            entry["versions"][str(mod.get("version") or "?")] += 1
            entry["regions"][str(server.get("region") or "?")] += 1
            entry["platforms"][str(server.get("platformName") or "?")] += 1
            if server.get("official"):
                entry["official"] += 1
            if server.get("passwordProtected"):
                entry["passworded"] += 1
            entry["topServers"].append(
                {
                    "id": server.get("id"),
                    "name": server.get("name"),
                    "players": players,
                    "limit": limit,
                    "region": server.get("region"),
                    "version": mod.get("version"),
                    "scenario": server.get("scenarioName"),
                    "gameVersion": server.get("gameVersion"),
                    "battlEye": bool(server.get("battlEye")),
                    "password": bool(server.get("passwordProtected")),
                    "official": bool(server.get("official")),
                    "modCount": len(server.get("mods") or []),
                }
            )
    totals["uniqueMods"] = len(mods)
    for entry in mods.values():
        entry["name"] = entry["names"].most_common(1)[0][0]
        entry["topServers"].sort(key=lambda s: (-s["players"], -s["limit"], s["name"] or ""))
    return mods, totals


def finalize(entry: dict, top_servers: int) -> dict:
    """Turn Counters into plain JSON and trim the server list."""
    return {
        "modId": entry["modId"],
        "name": entry["name"],
        "servers": entry["servers"],
        "players": entry["players"],
        "capacity": entry["capacity"],
        "serversWithPlayers": entry["serversWithPlayers"],
        "versions": dict(entry["versions"].most_common()),
        "regions": dict(entry["regions"].most_common()),
        "platforms": dict(entry["platforms"].most_common()),
        "official": entry["official"],
        "passworded": entry["passworded"],
        "topServers": entry["topServers"][:top_servers],
    }


def run(config_path: Path, data_dir: Path, offline_html: Path | None = None) -> int:
    cfg = load_config(config_path)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    data_dir.mkdir(parents=True, exist_ok=True)

    if offline_html:
        html = offline_html.read_text(encoding="utf-8", errors="replace")
        log(f"using offline page {offline_html} ({len(html)} chars)")
    else:
        html = fetch(cfg["source"]["serversPage"], cfg["userAgent"], int(cfg["requestTimeoutSeconds"]), int(cfg["retries"]))

    servers = extract_servers(html)
    if len(servers) < 100:
        raise RuntimeError(f"only {len(servers)} servers parsed; refusing to overwrite data with a partial page")
    mods, totals = aggregate(servers)
    log(f"servers={totals['servers']} players={totals['players']} uniqueMods={totals['uniqueMods']}")

    top_n_servers = int(cfg["site"].get("topServersPerMod", 12))
    keywords = [k.lower() for k in cfg.get("keywords", ["zombie"])]

    # Ranking of every mod, by servers then players, for global ranks.
    ranked_ids = sorted(mods, key=lambda m: (-mods[m]["servers"], -mods[m]["players"], mods[m]["name"].lower()))
    global_rank = {mod_id: i + 1 for i, mod_id in enumerate(ranked_ids)}

    keyword_ids = [m for m in ranked_ids if any(k in mods[m]["name"].lower() for k in keywords)]
    keyword_servers_total = sum(mods[m]["servers"] for m in keyword_ids)
    keyword_players_total = sum(mods[m]["players"] for m in keyword_ids)
    keyword_rank = {mod_id: i + 1 for i, mod_id in enumerate(keyword_ids)}

    def describe(mod_id: str) -> dict:
        out = finalize(mods[mod_id], top_n_servers)
        out["globalRank"] = global_rank[mod_id]
        out["keywordRank"] = keyword_rank.get(mod_id)
        out["shareOfKeywordServers"] = round(out["servers"] / keyword_servers_total, 4) if keyword_servers_total else None
        return out

    tracked_out = {}
    for item in cfg.get("tracked", []):
        mod_id = item["modId"]
        base = {k: v for k, v in item.items()}
        if mod_id in mods:
            base.update(describe(mod_id))
            base["seen"] = True
        else:
            base.update({"seen": False, "servers": 0, "players": 0, "capacity": 0, "versions": {}, "regions": {}, "platforms": {}, "topServers": [], "globalRank": None, "keywordRank": None})
        tracked_out[mod_id] = base

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
    }

    latest_path = data_dir / "latest.json"
    tmp = latest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(latest_path)

    # Compact per-run history row: tracked mods and the keyword set only.
    row = {
        "t": latest["generatedUtc"],
        "totals": {"servers": totals["servers"], "players": totals["players"], "uniqueMods": totals["uniqueMods"]},
        "tracked": {m: {"s": v["servers"], "p": v["players"], "c": v["capacity"]} for m, v in tracked_out.items()},
        "keyword": {m: {"s": mods[m]["servers"], "p": mods[m]["players"], "n": mods[m]["name"]} for m in keyword_ids},
    }
    with (data_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Every mod, compact, gzipped, for later analysis (about 300 KB).
    all_mods = [
        {"modId": m, "name": mods[m]["name"], "servers": mods[m]["servers"], "players": mods[m]["players"], "versions": dict(mods[m]["versions"].most_common(6))}
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
    parser.add_argument("--offline-html", type=Path, default=None, help="parse a saved /servers page instead of fetching")
    args = parser.parse_args(argv)
    try:
        return run(args.config, args.data_dir, args.offline_html)
    except Exception as exc:  # noqa: BLE001 - the scheduler needs a non-zero exit and a reason
        log(f"FAILED: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

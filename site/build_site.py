#!/usr/bin/env python3
"""Build docs/index.html (GitHub Pages) from data/latest.json, history.jsonl and downloads.jsonl.

Also writes docs/latest.json (the snapshot) and docs/trend.json (the flagship's and tracked
add-ons' last 14 days plus their download totals), which the Project Outbreak site reads.

The page is fully static: the data is embedded as JSON and rendered by a small inline
script, so it works from GitHub Pages, from a local file, or inside an artifact preview.
Chart.js is loaded from cdnjs for the time-series chart; everything else is inline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def parse_t(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def history_columns(rows: list[dict], ids: list[str], keyword_chart: set[str]) -> dict:
    """Columnar history: one epoch-second list, a backfilled flag list and per-mod value lists.

    A tracked mod missing from a row was not tracked yet (None). A charted keyword mod missing
    from a native row was on no server (0); missing from a backfilled row, it was not filled (None).
    """
    out = {"t": [], "b": [], "s": {i: [] for i in ids}, "p": {i: [] for i in ids}}
    for r in rows:
        out["t"].append(int(parse_t(r["t"]).timestamp()))
        out["b"].append(1 if r.get("backfilled") else 0)
        for i in ids:
            v = (r.get("tracked") or {}).get(i) or (r.get("keyword") or {}).get(i)
            if v is None and i in keyword_chart and not r.get("backfilled") and r.get("keyword"):
                v = {"s": 0, "p": 0}
            out["s"][i].append(None if v is None else v.get("s"))
            out["p"][i].append(None if v is None else v.get("p"))
    return out


def downloads_columns(rows: list[dict], latest: dict, ids: list[str]) -> dict:
    """Workshop download totals of `ids` over time, carried forward between changes.

    Rows hold only the totals that changed, so every value is carried forward from earlier rows;
    a mod is None until its first reading. The snapshot's own totals close the series.
    """
    points = [(parse_t(r["t"]), r["d"]) for r in rows]
    now_d = {i: ((e.get("workshop") or {}).get("downloads")) for i, e in latest.get("tracked", {}).items()}
    now_d = {i: v for i, v in now_d.items() if isinstance(v, int)}
    if now_d:
        points.append((parse_t(latest["generatedUtc"]), now_d))
    points.sort(key=lambda p: p[0])
    out = {"t": [], "d": {i: [] for i in ids}}
    carry: dict[str, int | None] = {i: None for i in ids}
    for t, d in points:
        if not any(k in carry for k in d):
            continue
        carry.update({k: v for k, v in d.items() if k in carry})
        out["t"].append(int(t.timestamp()))
        for i in ids:
            out["d"][i].append(carry[i])
    return out


def keyword_downloads(rows: list[dict], latest: dict) -> dict:
    """Each keyword mod's current Workshop total and its gain over the last 1 and 7 days.

    A gain is given only when the stored history reaches back that far for that mod; the
    keyword mods have been recorded since 8 October 2026, so their gains appear as it grows.
    """
    gen = parse_t(latest["generatedUtc"])
    points = sorted(((parse_t(r["t"]), r["d"]) for r in rows), key=lambda p: p[0])
    first_seen: dict[str, dt.datetime] = {}
    for t, d in points:
        for k in d:
            first_seen.setdefault(k, t)

    def value_at(mod_id: str, when: dt.datetime) -> int | None:
        v = None
        for t, d in points:
            if t > when:
                break
            if mod_id in d:
                v = d[mod_id]
        return v

    out = {}
    for m in latest["keyword"]["mods"]:
        now = m.get("downloads")
        if not isinstance(now, int):
            continue
        entry = {"now": now}
        for key, days in (("d1", 1), ("d7", 7)):
            when = gen - dt.timedelta(days=days)
            then = value_at(m["modId"], when) if m["modId"] in first_seen and first_seen[m["modId"]] <= when + dt.timedelta(hours=6) else None
            # A Workshop total can fall when an item is re-published; that is a reset, not a loss.
            entry[key] = None if then is None or now < then else now - then
        out[m["modId"]] = entry
    return out


def keyword_deltas(rows: list[dict], latest: dict, hours: int = 24) -> dict:
    """Each keyword mod's server change against the native snapshot nearest to `hours` ago."""
    gen = parse_t(latest["generatedUtc"])
    target = gen - dt.timedelta(hours=hours)
    native = [r for r in rows if not r.get("backfilled") and r.get("keyword")]
    best = min(native, key=lambda r: abs(parse_t(r["t"]) - target), default=None)
    if best is None or abs(parse_t(best["t"]) - target) > dt.timedelta(hours=6):
        return {"hours": None, "d": {}}
    ref = best["keyword"]
    return {
        "hours": round((gen - parse_t(best["t"])).total_seconds() / 3600),
        "d": {m["modId"]: m["servers"] - (ref.get(m["modId"]) or {}).get("s", 0) for m in latest["keyword"]["mods"]},
    }


def assert_inline_scripts_parse(html: str) -> None:
    """Refuse to publish a page whose inline JavaScript cannot parse.

    The whole page is rendered by one inline script from embedded JSON, so a single syntax error empties
    every table and chart while the HTML still looks fine: right size, data present, no missing file. The
    scraper keeps succeeding, the workflow keeps going green, and Pages keeps deploying a dead page.

    That is exactly what happened. An apostrophe in "the server's page" closed a single-quoted string early
    (template.html:290), and the site served no data for a day across ~24 successful hourly runs, because
    nothing between the scraper and the deploy ever asked whether the page WORKS.

    This is a string-literal scanner, not a JS parser: it walks quotes, template literals, comments and
    REGEX LITERALS well enough to catch an unterminated literal, which is the failure this build can actually
    introduce - the data is JSON-encoded, so the only hand-written JS is the template's own.

    Regex literals have to be understood or the scanner is worse than nothing: `/[&<>"]/g` in the template's
    own esc() contains a double quote, and a scanner that reads it as a string start reports a false failure
    on a healthy build. A guard that cries wolf gets switched off, and then the real one ships.
    """
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    for index, body in enumerate(scripts):
        if body.lstrip().startswith("{"):
            continue  # the embedded JSON payload, validated by json.dumps having produced it
        line = 1
        i = 0
        n = len(body)
        # A '/' is division when the previous meaningful character could end a value, and starts a regex
        # otherwise. This is the standard heuristic and it is sufficient here.
        prev_significant = ""
        while i < n:
            ch = body[i]
            if ch == "\n":
                line += 1
                i += 1
            elif ch in " \t\r":
                i += 1
            elif ch == "/" and i + 1 < n and body[i + 1] == "/":
                while i < n and body[i] != "\n":
                    i += 1
            elif ch == "/" and i + 1 < n and body[i + 1] == "*":
                end = body.find("*/", i + 2)
                if end < 0:
                    raise SystemExit(f"inline script {index}: unterminated block comment at line {line}")
                line += body.count("\n", i, end)
                i = end + 2
            elif ch == "/" and not (prev_significant.isalnum() or prev_significant in ")]}_$"):
                start_line = line
                i += 1
                closed = False
                in_class = False
                while i < n:
                    c = body[i]
                    if c == "\\":
                        i += 2
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        i += 1
                        closed = True
                        break
                    elif c == "\n":
                        break
                    i += 1
                if not closed:
                    raise SystemExit(f"inline script {index}: unterminated regex literal at line {start_line}")
                prev_significant = "/"
                continue
            elif ch in "\"'`":
                quote = ch
                start_line = line
                i += 1
                while i < n:
                    c = body[i]
                    if c == "\\":
                        i += 2
                        continue
                    if c == quote:
                        i += 1
                        prev_significant = quote
                        break
                    if c == "\n":
                        line += 1
                        if quote != "`":
                            raise SystemExit(
                                f"inline script {index}: unterminated {quote}-quoted string starting at line "
                                f"{start_line}. An unescaped {quote} inside the text is the usual cause - the "
                                f"whole script fails to parse and the page renders empty."
                            )
                    i += 1
                else:
                    raise SystemExit(f"inline script {index}: unterminated {quote}-quoted string at line {start_line}")
            else:
                prev_significant = ch
                i += 1


def build(config_path: Path, data_dir: Path, out_path: Path, artifact_out: Path | None = None) -> int:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    latest_path = data_dir / "latest.json"
    if not latest_path.exists():
        print("no data/latest.json yet; run the scraper first")
        return 1
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    all_rows = sorted(load_jsonl(data_dir / "history.jsonl"), key=lambda r: parse_t(r["t"]))
    gen = parse_t(latest["generatedUtc"])
    cutoff = gen - dt.timedelta(days=int(cfg.get("historyDaysOnPage", 30)))
    rows = [r for r in all_rows if parse_t(r["t"]) >= cutoff]
    # Labels and flags come from the CURRENT config, so an edit takes effect at the next build
    # without waiting for a scrape; the snapshot keeps the numbers.
    meta_keys = ("label", "addon", "channel", "flagship", "reserved", "note", "group")
    config_items = {item["modId"]: item for item in cfg.get("tracked", [])}
    for mod_id, entry in list(latest.get("tracked", {}).items()):
        item = config_items.get(mod_id)
        if item is None:
            continue
        for key in meta_keys:
            if key in item:
                entry[key] = item[key]
            else:
                entry.pop(key, None)
    # Config order is display order (groups, then the order inside each group).
    order = {mod_id: i for i, mod_id in enumerate(config_items)}
    latest["tracked"] = dict(sorted(latest.get("tracked", {}).items(), key=lambda kv: order.get(kv[0], len(order))))
    site = cfg["site"]
    tracked_ids = list(latest.get("tracked", {}))
    kw_ids = [m["modId"] for m in latest["keyword"]["mods"]]
    chart_ids = kw_ids[: int(site.get("chartSeries", 8))]
    series_ids = list(dict.fromkeys(tracked_ids + chart_ids))
    history = history_columns(rows, series_ids, set(chart_ids))
    download_rows = load_jsonl(data_dir / "downloads.jsonl")
    downloads = downloads_columns(download_rows, latest, tracked_ids)
    payload = {
        "latest": latest,
        "history": history,
        "chartIds": chart_ids,
        "kwDelta": keyword_deltas(all_rows, latest),
        "kwDownloads": keyword_downloads(download_rows, latest),
        "downloads": downloads,
        "config": {"source": cfg["source"], "armahq": cfg["armahq"], "site": site, "groups": cfg.get("groups", [])},
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    words = site["title"].split()
    title_html = html.escape(" ".join(words[:-1])) + (" <span>" + html.escape(words[-1]) + "</span>" if len(words) > 1 else html.escape(site["title"]))
    fragment = (
        template.replace("__TITLE_HTML__", title_html)
        .replace("__TITLE__", html.escape(site["title"]))
        .replace("__SUBTITLE_ATTR__", html.escape(site["subtitle"], quote=True))
        .replace("__SUBTITLE__", html.escape(site["subtitle"]))
        .replace("__SOURCE_URL__", html.escape(cfg["source"]["url"], quote=True))
        .replace("__SOURCE_NAME__", html.escape(cfg["source"]["name"]))
        .replace("__CREDIT_LINE__", html.escape(cfg["source"]["creditLine"]))
        .replace("__KW_LABEL_LC__", html.escape(cfg.get("keywordLabel", "keyword mods").lower()))
        .replace("__MAIN_SITE__", html.escape(site.get("mainSite", "https://outbreakarma.github.io/"), quote=True))
        .replace("__DISCORD__", html.escape(site.get("discord", ""), quote=True))
        .replace("__GITHUB__", html.escape(site.get("github", ""), quote=True))
        .replace("__DATA_JSON__", data_json)
    )
    # The template is a head+body fragment (title, style, then content). Split it into a
    # full document for GitHub Pages; artifact previews take the fragment as is.
    marker = "</style>\n"
    head_part, body_part = fragment.split(marker, 1)
    full = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        + head_part + marker + "</head>\n<body>\n" + body_part + "\n</body>\n</html>\n"
    )
    assert_inline_scripts_parse(full)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(full, encoding="utf-8")
    (out_path.parent / ".nojekyll").touch()
    # A copy of the data beside the page, for anyone who wants the numbers rather than the view.
    (out_path.parent / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    flag = next((i for i, e in latest.get("tracked", {}).items() if e.get("flagship")), tracked_ids[0] if tracked_ids else None)
    recent = [i for i, t in enumerate(history["t"]) if t >= int((gen - dt.timedelta(days=14)).timestamp())]
    keyword = latest.get("keyword", {})
    trend = {
        "generatedUtc": latest["generatedUtc"],
        "flagship": flag,
        "keyword": {"label": keyword.get("label"), "modCount": keyword.get("modCount")},
        "uniqueMods": latest.get("totals", {}).get("uniqueMods"),
        "tracked": {
            m: {
                "label": e.get("label") or e.get("name"),
                "servers": e.get("servers"),
                "players": e.get("players"),
                "keywordRank": e.get("keywordRank"),
                "globalRank": e.get("globalRank"),
                "versions": e.get("versions"),
                "capacity": e.get("capacity"),
                "serversWithPlayers": e.get("serversWithPlayers"),
                "downloads": (e.get("workshop") or {}).get("downloads"),
                "version": (e.get("workshop") or {}).get("version"),
                "playerHours7d": (e.get("workshop") or {}).get("playerHours7d"),
                "rootDeployments": (e.get("workshop") or {}).get("rootDeployments"),
                "dependencyDeployments": (e.get("workshop") or {}).get("dependencyDeployments"),
            }
            for m, e in latest.get("tracked", {}).items()
        },
        "hours": {
            "t": [history["t"][i] for i in recent],
            "s": {m: [history["s"][m][i] for i in recent] for m in tracked_ids},
            "p": {m: [history["p"][m][i] for i in recent] for m in tracked_ids},
        },
        "downloads": downloads,
    }
    (out_path.parent / "trend.json").write_text(json.dumps(trend, separators=(",", ":")), encoding="utf-8")
    if artifact_out:
        artifact_out.parent.mkdir(parents=True, exist_ok=True)
        artifact_out.write_text(fragment, encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes) with {len(history['t'])} history rows; generated {latest['generatedUtc']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static ArmaHqStats page.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "index.html")
    parser.add_argument("--artifact-out", type=Path, default=None, help="also write the page as a head+body fragment for an artifact preview")
    args = parser.parse_args(argv)
    try:
        return build(args.config, args.data_dir, args.out, args.artifact_out)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

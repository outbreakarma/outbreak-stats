#!/usr/bin/env python3
"""Build docs/index.html (GitHub Pages) from data/latest.json and data/history.jsonl.

The page is fully static: the data is embedded as JSON and rendered by a small inline
script, so it works from GitHub Pages, from a local file, or inside an artifact preview.
Chart.js is loaded from cdnjs for the time-series chart; everything else is inline.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def load_history(path: Path, limit: int) -> list[dict]:
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
    return rows[-limit:]


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
    history = load_history(data_dir / "history.jsonl", int(cfg.get("historyRowsOnPage", 336)))
    # Labels and flags come from the CURRENT config, so an edit takes effect at the next build
    # without waiting for a scrape; the snapshot keeps the numbers.
    meta_keys = ("label", "addon", "channel", "flagship", "reserved", "note")
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
    site = cfg["site"]
    payload = {"latest": latest, "history": history, "config": {"source": cfg["source"], "site": site}}
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
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
    if artifact_out:
        artifact_out.parent.mkdir(parents=True, exist_ok=True)
        artifact_out.write_text(fragment, encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes) with {len(history)} history rows; generated {latest['generatedUtc']}")
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

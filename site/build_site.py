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


def build(config_path: Path, data_dir: Path, out_path: Path, artifact_out: Path | None = None) -> int:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    latest_path = data_dir / "latest.json"
    if not latest_path.exists():
        print("no data/latest.json yet; run the scraper first")
        return 1
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    history = load_history(data_dir / "history.jsonl", int(cfg.get("historyRowsOnPage", 336)))
    site = cfg["site"]
    payload = {"latest": latest, "history": history, "config": {"source": cfg["source"], "site": site}}
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    fragment = (
        template.replace("__TITLE__", html.escape(site["title"]))
        .replace("__SUBTITLE_ATTR__", html.escape(site["subtitle"], quote=True))
        .replace("__SUBTITLE__", html.escape(site["subtitle"]))
        .replace("__SOURCE_URL__", html.escape(cfg["source"]["url"], quote=True))
        .replace("__SOURCE_NAME__", html.escape(cfg["source"]["name"]))
        .replace("__CREDIT_LINE__", html.escape(cfg["source"]["creditLine"]))
        .replace("__KW_LABEL_LC__", html.escape(cfg.get("keywordLabel", "keyword mods").lower()))
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

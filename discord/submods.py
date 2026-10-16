#!/usr/bin/env python3
"""Announce new Project Outbreak submods in Discord: one message per new Workshop mod.

A submod is a Workshop mod that declares one of the tracked Project Outbreak add-ons as a
Workshop dependency. Each run asks ArmaHQ's dependency index (the same data behind the
flagship's "Built on" list) for every tracked add-on, drops the add-ons themselves, and
compares what is left with data/submods.json.

The first run, or any run without that file, records the current submods as seen and posts
nothing, so the channel does not fill up with mods that were already there. After that, each
new submod gets one message in the webhook named by DISCORD_SUBMODS_WEBHOOK, at most MAX_POSTS
per run. A submod is marked seen only once Discord accepts its message, so a failed post is
tried again on the next run. With no webhook set, the script does nothing and exits 0.

Summary, author and image come from ReforgerMods.net when it has them; a missing value just
leaves that part out. The webhook URL is read from the environment and is never printed or
written to disk.

  python discord/submods.py --dry-run   # print the messages that would go out; no Discord, no writes
  python discord/submods.py             # post new submods and update data/submods.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "submods.json"
WEBHOOK_ENV = "DISCORD_SUBMODS_WEBHOOK"
HEADLINE = "\U0001f9df **A new submod appears!**"
WORKSHOP_URL = "https://reforger.armaplatform.com/workshop/{modId}"
EMBED_COLOR = 0x7FB069
MAX_POSTS = 5  # new submods announced per run; the rest wait for a later run
MAX_FAILURES = 3  # failed posts tolerated per run before the run stops posting
MIN_SUMMARY_CHARS = 8  # shorter ReforgerMods summaries are placeholders such as "1"
RETRY_CAP_SECONDS = 120


def log(msg: str) -> None:
    print(msg, flush=True)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tidy(text: str, limit: int) -> str:
    """Plain text for Discord: no em or en dashes, single spaces, cut to the limit with an ellipsis."""
    text = " ".join(text.replace("\u2014", "-").replace("\u2013", "-").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "\u2026"


def escape_md(text: str) -> str:
    for ch in "\\*_~|`":
        text = text.replace(ch, "\\" + ch)
    return text


def fetch_json(url: str, user_agent: str, timeout: int, retries: int) -> dict:
    """GET a JSON response the way the scraper does: honor Retry-After on 429, back off otherwise."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = exc.headers.get("Retry-After") or ""
            wait = int(retry_after) if exc.code == 429 and retry_after.isdigit() else 15 * attempt
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            wait = 15 * attempt
        if attempt < retries:
            time.sleep(min(wait, RETRY_CAP_SECONDS))
    raise RuntimeError(f"could not fetch {url}: {last_error!r}")


def current_submods(cfg: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Every submod ArmaHQ lists under a tracked add-on: its name, and the add-on labels it builds on."""
    tracked = cfg["tracked"]
    tracked_ids = {entry["modId"] for entry in tracked}
    names: dict[str, str] = {}
    builds_on: dict[str, list[str]] = {}
    for entry in tracked:
        url = cfg["armahq"]["dependentsUrl"].replace("{modId}", entry["modId"])
        body = fetch_json(url, cfg["userAgent"], int(cfg["requestTimeoutSeconds"]), int(cfg["retries"]))
        for dep in body["dependents"]:
            if dep["modId"] in tracked_ids:
                continue
            names.setdefault(dep["modId"], dep.get("name") or dep["modId"])
            builds_on.setdefault(dep["modId"], []).append(entry["label"])
        time.sleep(float(cfg.get("requestGapSeconds", 1.1)))
    return names, builds_on


def fetch_detail(mod_id: str, cfg: dict) -> dict:
    """Summary, author and image from ReforgerMods.net for one submod. Best effort: {} when unavailable."""
    try:
        body = fetch_json(
            f"{cfg['source']['apiBase']}/mods/{mod_id}",
            cfg["userAgent"],
            int(cfg["requestTimeoutSeconds"]),
            int(cfg["retries"]),
        )
        return body.get("mod") or {}
    except RuntimeError as exc:
        log(f"no ReforgerMods details for {mod_id}; the post leaves them out ({exc})")
        return {}


def build_message(mod_id: str, name: str, builds_on: list[str], footer: str, detail: dict) -> dict:
    embed: dict = {
        "title": escape_md(tidy(name, 200)),
        "url": WORKSHOP_URL.format(modId=mod_id),
        "color": EMBED_COLOR,
        "fields": [{"name": "Builds on", "value": escape_md(tidy(", ".join(builds_on), 1000)), "inline": False}],
        "footer": {"text": footer},
    }
    summary = tidy(str(detail.get("summary") or ""), 280)
    if len(summary) >= MIN_SUMMARY_CHARS:
        embed["description"] = escape_md(summary)
    author = tidy(str(detail.get("author") or ""), 100)
    if author:
        embed["fields"].append({"name": "Author", "value": escape_md(author), "inline": True})
    image = str(detail.get("imageUrl") or "")
    if image.startswith("https://"):
        embed["image"] = {"url": image}
    return {"content": HEADLINE, "embeds": [embed], "allowed_mentions": {"parse": []}}


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Seconds Discord asks us to wait on a 429, from the JSON body; a short default otherwise."""
    try:
        return float(json.loads(exc.read() or b"{}").get("retry_after", 2)) + 0.5
    except (ValueError, AttributeError, TypeError):
        return 2.5


def post_message(webhook: str, payload: dict, cfg: dict) -> str:
    """POST one message with ?wait=true and return its id. Errors name the HTTP status, never the URL."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, 4):
        req = urllib.request.Request(
            f"{webhook}?wait=true",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": cfg["userAgent"]},
        )
        try:
            with urllib.request.urlopen(req, timeout=int(cfg["requestTimeoutSeconds"])) as resp:
                reply = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(min(_retry_after(exc), RETRY_CAP_SECONDS))
                continue
            raise RuntimeError(f"Discord refused the post (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 3:
                time.sleep(5)
                continue
            raise RuntimeError(f"could not reach Discord ({type(exc).__name__})") from None
        if not reply.get("id"):
            raise RuntimeError("Discord accepted the post without a message id")
        return str(reply["id"])
    raise RuntimeError("Discord kept rate limiting the post")


def webhook_base(raw: str) -> str:
    """The webhook without its query string or trailing slash; ?wait=true is added per post."""
    return raw.split("?", 1)[0].rstrip("/")


def load_state() -> dict | None:
    """The state file, or None when it does not exist yet (the first run)."""
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the messages that would be posted; no Discord call and no state write",
    )
    args = ap.parse_args(argv)

    raw_webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if not args.dry_run and not raw_webhook:
        log(f"{WEBHOOK_ENV} is not set; nothing to do")
        return 0
    webhook = webhook_base(raw_webhook)
    if not args.dry_run and "/api/webhooks/" not in webhook:
        log(f"{WEBHOOK_ENV} is not a Discord webhook URL; nothing posted")
        return 1

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    footer = tidy(f"Data: {cfg['armahq']['name']} & {cfg['source']['name']}", 200)
    gap = float(cfg.get("requestGapSeconds", 1.1))

    try:
        names, builds_on = current_submods(cfg)
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        log(f"ArmaHQ lookup failed; nothing posted or recorded ({exc})")
        return 1
    try:
        state = load_state()
    except ValueError:
        log(f"{STATE_PATH.relative_to(ROOT)} is not valid JSON; fix or remove it. Nothing posted.")
        return 1

    if state is None:
        if args.dry_run:
            log(f"no {STATE_PATH.name} yet: would record {len(names)} submods as seen and post none")
            return 0
        state = {
            "submods": {
                mod_id: {"name": names[mod_id], "firstSeen": utc_now(), "announced": False} for mod_id in names
            }
        }
        save_state(state)
        log(f"first run: recorded {len(names)} existing submods as seen; posted none")
        return 0

    seen = state.setdefault("submods", {})
    new = sorted((mod_id for mod_id in names if mod_id not in seen), key=lambda mod_id: names[mod_id].lower())
    log(f"{len(names)} submods on ArmaHQ, {len(new)} new")

    sent = failures = attempts = 0
    for mod_id in new:
        if sent >= MAX_POSTS or failures >= MAX_FAILURES:
            break
        detail = fetch_detail(mod_id, cfg)
        payload = build_message(mod_id, names[mod_id], builds_on[mod_id], footer, detail)
        if args.dry_run:
            log(json.dumps(payload, indent=2, ensure_ascii=False))
            sent += 1
            continue
        if attempts:
            time.sleep(gap)
        attempts += 1
        try:
            message_id = post_message(webhook, payload, cfg)
        except (RuntimeError, ValueError) as exc:
            failures += 1
            log(f"post failed for {mod_id} ({names[mod_id]}); it stays unseen and is retried next run: {exc}")
            continue
        seen[mod_id] = {"name": names[mod_id], "firstSeen": utc_now(), "announced": True}
        save_state(state)
        sent += 1
        log(f"posted {names[mod_id]} ({mod_id}) as message {message_id}")

    if args.dry_run:
        log(f"dry run: {sent} of {len(new)} new submods shown; nothing posted or recorded")
        return 0
    log(f"done: {sent} posted, {failures} failed, {len(new) - sent - failures} waiting for a later run")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

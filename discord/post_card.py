#!/usr/bin/env python3
"""Keep one Discord message showing the flagship's stats card, edited in place after each snapshot.

Renders the page's Project Outbreak section (docs/index.html, #flagship) to a PNG with a
headless browser, then edits the message a Discord webhook posted earlier. The first run, or a
run after the message was deleted or the webhook replaced, posts a new message instead. Edits do
not notify anyone, so the channel holds a single card that is never reposted.

The webhook URL comes from the DISCORD_STATS_WEBHOOK environment variable (a repository secret)
and is never printed or written to disk. data/discord_card.json keeps the message id and the
snapshot it shows, so a run without a new snapshot does nothing.

  python discord/post_card.py --check     # writes due=true|false to $GITHUB_OUTPUT
  python discord/post_card.py             # render and post or edit
  python discord/post_card.py --out card.png   # render only, no Discord
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
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = ROOT / "docs" / "index.html"
LATEST_PATH = ROOT / "data" / "latest.json"
STATE_PATH = ROOT / "data" / "discord_card.json"
CONFIG_PATH = ROOT / "config.json"

VIEWPORT_WIDTH = 1040
SCALE = 2
PAD = 18
# Everything on the page except the flagship section is hidden before the shot.
ISOLATE_CSS = (
    "header.mast,nav.jump,.hazard-bar,footer,section:not(#flagship){display:none!important}"
    ".wrap{padding-top:0!important;padding-bottom:0!important}"
    "#flagship{margin-top:0!important;padding-top:4px}"
)
# The "how servers get it" note points at sections further down the page; in Discord only
# its first sentence makes sense. Returns the section's box, bounded by its last child.
PREPARE_JS = """() => {
  const note = document.querySelector('#insight .split .d');
  if (note) note.textContent = note.textContent.split('. ')[0].replace(/\\.$/, '') + '.';
  const section = document.querySelector('#flagship');
  if (!section) return null;
  const top = section.getBoundingClientRect();
  let bottom = top.top;
  for (const child of section.children) bottom = Math.max(bottom, child.getBoundingClientRect().bottom);
  return {x: top.left + scrollX, y: top.top + scrollY, width: top.width, height: bottom - top.top};
}"""


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def snapshot_utc() -> str | None:
    return load_json(LATEST_PATH).get("generatedUtc")


def is_due(state: dict, force: bool) -> bool:
    if not os.environ.get("DISCORD_STATS_WEBHOOK"):
        return False
    return force or state.get("snapshotUtc") != snapshot_utc()


def render_card() -> bytes:
    from playwright.sync_api import sync_playwright

    channel = os.environ.get("CARD_BROWSER_CHANNEL", "chrome") or None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=channel)
        try:
            page = browser.new_page(
                viewport={"width": VIEWPORT_WIDTH, "height": 900},
                device_scale_factor=SCALE,
                color_scheme="dark",
                reduced_motion="reduce",
            )
            page.goto(PAGE_PATH.as_uri(), wait_until="networkidle", timeout=60_000)
            page.add_style_tag(content=ISOLATE_CSS)
            page.evaluate("document.fonts.ready")
            page.wait_for_selector("#hero-main .stat", timeout=30_000)
            box = page.evaluate(PREPARE_JS)
            if not box:
                raise RuntimeError("the #flagship section did not render")
            clip = {
                "x": max(box["x"] - PAD, 0),
                "y": max(box["y"] - PAD, 0),
                "width": box["width"] + 2 * PAD,
                "height": box["height"] + 2 * PAD,
            }
            return page.screenshot(clip=clip, full_page=True, type="png")
        finally:
            browser.close()


def message_text(snapshot: str | None) -> str:
    site = load_json(CONFIG_PATH).get("site", {})
    page_url = site.get("statsPage", "https://outbreakarma.github.io/outbreak-stats/")
    taken = dt.datetime.fromisoformat(snapshot.replace("Z", "+00:00")) if snapshot else dt.datetime.now(dt.timezone.utc)
    # <t:...:R> renders in each reader's client as "5 minutes ago" and keeps counting.
    return (
        "\U0001f9df **Project Outbreak Zombies: live stats**\n"
        f"Updated <t:{int(taken.timestamp())}:R> · [Full stats page](<{page_url}>)\n"
        "-# Data: ReforgerMods.net & ArmaHQ · refreshed hourly"
    )


def multipart(payload: dict, png: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        "Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"outbreak-stats.png\"\r\n"
        "Content-Type: image/png\r\n\r\n".encode() + png + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send(method: str, url: str, payload: dict, png: bytes) -> tuple[int, dict]:
    body, ctype = multipart(payload, png)
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Content-Type": ctype,
            "User-Agent": "OutbreakStats-DiscordCard/1.0 (+https://github.com/outbreakarma/outbreak-stats)",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = load_body(exc)
            if exc.code == 429 and attempt < 2:
                time.sleep(float(detail.get("retry_after", 2)) + 0.5)
                continue
            return exc.code, detail
    return 429, {}


def load_body(exc: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(exc.read() or b"{}")
    except ValueError:
        return {}


def publish(webhook: str, png: bytes, state: dict, snapshot: str | None) -> dict:
    webhook = webhook.strip().split("?")[0].rstrip("/")
    webhook_id = webhook.split("/webhooks/")[-1].split("/")[0]
    payload = {
        "content": message_text(snapshot),
        "allowed_mentions": {"parse": []},
        "attachments": [{"id": 0, "filename": "outbreak-stats.png"}],
    }
    message_id = state.get("messageId") if state.get("webhookId") == webhook_id else None
    if message_id:
        status, _ = send("PATCH", f"{webhook}/messages/{message_id}", payload, png)
        if status == 200:
            print(f"edited message {message_id}")
            return {"webhookId": webhook_id, "messageId": message_id, "snapshotUtc": snapshot}
        if status != 404:
            raise RuntimeError(f"Discord refused the edit (HTTP {status})")
        print("the old message is gone; posting a new one")
    status, msg = send("POST", f"{webhook}?wait=true", payload, png)
    if status != 200 or "id" not in msg:
        raise RuntimeError(f"Discord refused the post (HTTP {status}: {msg.get('message', '')})")
    print(f"posted message {msg['id']}; pin it in the channel")
    return {"webhookId": webhook_id, "messageId": msg["id"], "snapshotUtc": snapshot}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only report whether a refresh is due")
    ap.add_argument("--force", action="store_true", help="refresh even if the snapshot is unchanged")
    ap.add_argument("--out", type=Path, help="render the card to this file and stop")
    args = ap.parse_args()

    state = load_json(STATE_PATH)
    if args.check:
        due = is_due(state, args.force)
        print("Discord card refresh due" if due else "Discord card is current, or no webhook is set")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"due={'true' if due else 'false'}\n")
        return 0

    if args.out:
        args.out.write_bytes(render_card())
        print(f"wrote {args.out}")
        return 0

    webhook = os.environ.get("DISCORD_STATS_WEBHOOK", "")
    if not webhook:
        print("DISCORD_STATS_WEBHOOK is not set; nothing to post")
        return 0
    if not is_due(state, args.force):
        print("the card already shows this snapshot")
        return 0
    snapshot = snapshot_utc()
    new_state = publish(webhook, render_card(), state, snapshot)
    STATE_PATH.write_text(json.dumps(new_state, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

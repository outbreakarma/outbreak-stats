#!/usr/bin/env python3
"""One scheduled run: scrape, then rebuild the static site. Exit code is non-zero on any failure.

The workflow fires several times an hour because GitHub drops most scheduled runs under load;
a run that finds a snapshot younger than MIN_AGE_MINUTES exits at once, so the extra slots
only raise the chance of one run per hour and never add near-duplicate rows. --force skips that.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_AGE_MINUTES = 40


def snapshot_age_minutes() -> float | None:
    try:
        stamp = json.loads((ROOT / "data" / "latest.json").read_text(encoding="utf-8"))["generatedUtc"]
    except (OSError, ValueError, KeyError):
        return None
    taken = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - taken).total_seconds() / 60


def main() -> int:
    age = snapshot_age_minutes()
    if "--force" not in sys.argv[1:] and age is not None and age < MIN_AGE_MINUTES:
        print(f"snapshot is {age:.0f} min old (< {MIN_AGE_MINUTES}); nothing to do")
        return 0
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = logs / f"run-{stamp}.log"
    with log_path.open("w", encoding="utf-8") as fh:
        for step in (["scraper/reforgermods_scrape.py"], ["site/build_site.py"]):
            cmd = [sys.executable, str(ROOT / step[0])]
            fh.write(f"== {' '.join(cmd)}\n")
            fh.flush()
            proc = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                fh.write(f"== step failed with exit code {proc.returncode}\n")
                print(f"run failed at {step[0]}; see {log_path}")
                return proc.returncode
    # Keep the newest 200 logs.
    for old in sorted(logs.glob("run-*.log"))[:-200]:
        old.unlink(missing_ok=True)
    print(f"ok; log {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

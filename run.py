#!/usr/bin/env python3
"""One scheduled run: scrape ArmaHQ, then rebuild the static site. Exit code is non-zero on any failure."""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = logs / f"run-{stamp}.log"
    with log_path.open("w", encoding="utf-8") as fh:
        for step in (["scraper/armahq_scrape.py"], ["site/build_site.py"]):
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

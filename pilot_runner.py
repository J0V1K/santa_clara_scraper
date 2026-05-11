#!/usr/bin/env python3
"""Run a bounded Santa Clara pilot and persist logs/metadata."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRAPER = ROOT / "scraper.py"
RUNS_ROOT = ROOT / "runs"


def utc_now_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded Santa Clara pilot")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--case-type", default="Civil")
    parser.add_argument("--case-prefix", action="append", default=["CV"])
    parser.add_argument("--case-type-include-regex", action="append", default=[])
    parser.add_argument("--case-type-exclude-regex", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=1)
    parser.add_argument("--offset-cases", type=int, default=0)
    parser.add_argument("--max-docs-per-case", type=int, default=4)
    parser.add_argument("--day-retries", type=int, default=1)
    parser.add_argument("--day-backoff-s", type=float, default=25.0)
    parser.add_argument("--min-delay-s", type=float, default=0.6)
    parser.add_argument("--max-delay-s", type=float, default=1.4)
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--label", default="pilot")
    # The scraper is headful by default; pilot runs typically want to
    # opt into headless for unattended automation. Captcha gates will
    # then fail with a hard error rather than waiting for a solver.
    parser.add_argument("--headless", action="store_true",
                        help="Run the scraper headless (unattended). Captcha gates will fail.")
    parser.add_argument("--human-verify-on-start", action="store_true",
                        help="Forward --human-verify-on-start to the scraper.")
    parser.add_argument("--human-verify-on-deny", action="store_true",
                        help="Forward --human-verify-on-deny to the scraper.")
    parser.add_argument("--human-verify-timeout-s", type=float, default=None,
                        help="Forward --human-verify-timeout-s to the scraper.")
    parser.add_argument("--stream", action="store_true",
                        help="Tee scraper stdout to this terminal as well as the run log, "
                             "so HUMAN VERIFICATION REQUIRED prompts surface when running headful.")
    return parser.parse_args()


def build_command(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, str(SCRAPER)]
    cmd.extend(["--start-date", args.start_date, "--end-date", args.end_date])
    cmd.extend(["--case-type", args.case_type])
    for prefix in args.case_prefix:
        cmd.extend(["--case-prefix", prefix])
    for pattern in args.case_type_include_regex:
        cmd.extend(["--case-type-include-regex", pattern])
    for pattern in args.case_type_exclude_regex:
        cmd.extend(["--case-type-exclude-regex", pattern])
    cmd.extend(["--max-cases", str(args.max_cases)])
    cmd.extend(["--offset-cases", str(args.offset_cases)])
    cmd.extend(["--max-docs-per-case", str(args.max_docs_per_case)])
    cmd.extend(["--day-retries", str(args.day_retries)])
    cmd.extend(["--day-backoff-s", str(args.day_backoff_s)])
    cmd.extend(["--min-delay-s", str(args.min_delay_s)])
    cmd.extend(["--max-delay-s", str(args.max_delay_s)])
    if args.calibrate:
        cmd.append("--calibrate")
    if args.headless:
        cmd.append("--headless")
    if args.human_verify_on_start:
        cmd.append("--human-verify-on-start")
    if args.human_verify_on_deny:
        cmd.append("--human-verify-on-deny")
    if args.human_verify_timeout_s is not None:
        cmd.extend(["--human-verify-timeout-s", str(args.human_verify_timeout_s)])
    return cmd


def main() -> int:
    args = parse_args()
    run_dir = RUNS_ROOT / f"{args.label}_{utc_now_stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    meta_path = run_dir / "meta.json"
    summary_path = run_dir / "summary.json"

    cmd = build_command(args)
    meta = {
        "started_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "command": cmd,
        "run_dir": str(run_dir),
        "data_root": str(ROOT / "data"),
        "args": vars(args),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if args.stream:
        # Tee child stdout to terminal + log so a human can see
        # "HUMAN VERIFICATION REQUIRED" prompts in real time when the
        # scraper is running headful and a CAPTCHA fires.
        with log_path.open("w", encoding="utf-8") as log:
            proc_obj = subprocess.Popen(
                cmd, cwd=ROOT.parent, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            assert proc_obj.stdout is not None
            for line in proc_obj.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
            returncode = proc_obj.wait()
        class _R:
            pass
        proc = _R()
        proc.returncode = returncode
    else:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(cmd, cwd=ROOT.parent, stdout=log, stderr=subprocess.STDOUT, text=True)

    finished = datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary = {
        **meta,
        "finished_at": finished,
        "returncode": proc.returncode,
        "log_path": str(log_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())

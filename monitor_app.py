#!/usr/bin/env python3
"""Local monitor for the Santa Clara scraper corpus."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "monitor"
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def classify_day(day_dir: Path) -> dict:
    summary_path = day_dir / "day_summary.json"
    failed_path = day_dir / "failed_cases.json"

    total = 0
    scraped = 0
    failed_count = 0
    run_error = None

    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text())
            total = int(data.get("total_cases") or 0)
            scraped = int(data.get("scraped_cases") or 0)
            failed_count = int(data.get("failed_cases") or 0)
            run_error = data.get("run_error")
        except Exception:
            pass

    if failed_path.exists():
        try:
            payload = json.loads(failed_path.read_text())
            if isinstance(payload, list):
                failed_count = max(failed_count, len(payload))
        except Exception:
            pass

    if run_error:
        status = "run_error"
    elif total == 0 and scraped == 0:
        status = "no_cases"
    elif failed_count > 0:
        status = "has_failures"
    elif scraped >= total and total > 0:
        status = "complete"
    elif scraped > 0:
        status = "in_progress"
    else:
        status = "pending"

    return {
        "date": day_dir.name,
        "total": total,
        "scraped": scraped,
        "failed": failed_count,
        "status": status,
        "run_error": run_error,
    }


def gather_days(data_root: Path) -> list[dict]:
    if not data_root.exists():
        return []
    days = []
    for child in data_root.iterdir():
        if child.is_dir() and DAY_DIR_RE.match(child.name):
            days.append(classify_day(child))
    days.sort(key=lambda d: d["date"])
    return days


def gather_rate(data_root: Path, now_ts: float) -> dict:
    hour_ago = now_ts - 3600
    day_ago = now_ts - 86400
    week_ago = now_ts - 7 * 86400

    in_hour = in_day = in_week = 0
    pdfs_last_24h = 0
    most_recent_ts = 0.0

    if data_root.exists():
        for roa in data_root.glob("*/*/register_of_actions.json"):
            try:
                mtime = roa.stat().st_mtime
            except OSError:
                continue
            most_recent_ts = max(most_recent_ts, mtime)
            if mtime >= hour_ago:
                in_hour += 1
            if mtime >= day_ago:
                in_day += 1
            if mtime >= week_ago:
                in_week += 1
        # Count both .pdf and .txt — successful OCR deletes the PDF and
        # keeps a .txt, so a PDF-only count silently hides scraping
        # activity once the OCR pipeline is enabled.
        for doc_path in data_root.glob("*/*/*.pdf"):
            try:
                mtime = doc_path.stat().st_mtime
            except OSError:
                continue
            if mtime >= day_ago:
                pdfs_last_24h += 1
        for doc_path in data_root.glob("*/*/*.txt"):
            try:
                mtime = doc_path.stat().st_mtime
            except OSError:
                continue
            if mtime >= day_ago:
                pdfs_last_24h += 1

    last_activity_iso = None
    if most_recent_ts > 0:
        last_activity_iso = (
            datetime.fromtimestamp(most_recent_ts, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    return {
        "cases_last_hour": in_hour,
        "cases_last_24h": in_day,
        "cases_last_7d": in_week,
        "pdfs_last_24h": pdfs_last_24h,
        "last_activity_at": last_activity_iso,
    }


_STATUS_CACHE = {"data": None, "timestamp": 0.0}
CACHE_TTL_SECONDS = 30.0


def build_status(data_root: Path) -> dict:
    global _STATUS_CACHE
    now = datetime.now(tz=timezone.utc).timestamp()
    if _STATUS_CACHE["data"] and (now - _STATUS_CACHE["timestamp"]) < CACHE_TTL_SECONDS:
        return _STATUS_CACHE["data"]

    days = gather_days(data_root)
    rate = gather_rate(data_root, now)
    totals = {
        "days_tracked": len(days),
        "days_complete": sum(1 for d in days if d["status"] == "complete"),
        "days_in_progress": sum(1 for d in days if d["status"] == "in_progress"),
        "days_with_failures": sum(1 for d in days if d["status"] == "has_failures"),
        "days_with_run_error": sum(1 for d in days if d["status"] == "run_error"),
        "cases_total": sum(d["total"] for d in days),
        "cases_scraped": sum(d["scraped"] for d in days),
    }
    status = {
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "data_root": str(data_root),
        "totals": totals,
        "rate": rate,
        "days": days,
    }
    _STATUS_CACHE["data"] = status
    _STATUS_CACHE["timestamp"] = now
    return status


class MonitorHandler(BaseHTTPRequestHandler):
    data_root: Path = ROOT / "data"

    def log_message(self, *args, **kwargs):
        return

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        target = (STATIC_ROOT / rel).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype, _ = mimetypes.guess_type(target.name)
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json(build_status(self.data_root))
            return
        if path in {"", "/"}:
            self._send_static("index.html")
            return
        self._send_static(path.lstrip("/"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Santa Clara scraper monitor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    MonitorHandler.data_root = args.data_root.resolve()
    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    print(f"Monitor serving http://{args.host}:{args.port} (data: {MonitorHandler.data_root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

import argparse
import asyncio
import base64
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "Camoufox is required for santa_clara_scraper. "
        "Install it in detection_pilot/.venv before running."
    ) from exc

# Reuse the OK scraper's OCR helper so SC, OK, and SF all produce the
# same `.txt` + telemetry shape, and detection_pilot scripts can iterate
# uniformly over any county's data tree.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ok_scraper"))
from ocr import ocr_pdf as _ocr_pdf  # noqa: E402

# Cross-scraper heartbeat helper.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from monitor.heartbeat import Heartbeat  # noqa: E402

# Module-level toggles set by main(); same defaults as OK.
RUN_OCR = True
KEEP_PDFS = False
HEARTBEAT: Heartbeat | None = None


def _probe_public_ip() -> str:
    """Best-effort fetch of the current public IPv4 — surfaced in the
    monitor so the live-runs panel shows which VPN exit we're on."""
    import subprocess
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "5", "https://ipv4.icanhazip.com"],
            capture_output=True, text=True, check=False, timeout=8,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def case_prefixes_for_intent(args):
    return [p.upper() for p in args.case_prefix]


PORTAL_URL = "https://portal.scscourt.org"
SEARCH_URL = f"{PORTAL_URL}/search"
SEARCH_API = f"{PORTAL_URL}/api/cases/byfilingdates"
TOKEN_API = f"{PORTAL_URL}/api/case/token"
CASE_API_TEMPLATE = f"{PORTAL_URL}/api/case/{{case_id}}"
DOC_API_TEMPLATE = f"{PORTAL_URL}/api/doc/base64/doc?docId={{doc_id}}"

SCRAPER_ROOT = Path(__file__).resolve().parent
DATA_ROOT = SCRAPER_ROOT / "data"
CALIBRATION_ROOT = DATA_ROOT / "_calibration"

CASE_NUMBER_PREFIX_RE = re.compile(r"^\d{2}([A-Z]+)\d+$")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def daterange(start_iso: str, end_iso: str) -> list[date]:
    start = parse_iso_date(start_iso)
    end = parse_iso_date(end_iso)
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def normalize_date(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = raw.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def case_url(case_id: str) -> str:
    encoded = base64.b64encode(case_id.encode("utf-8")).decode("ascii")
    return f"{PORTAL_URL}/case/{quote(encoded)}"


def slugify(value: str, max_len: int = 80) -> str:
    lowered = (value or "").strip().lower().replace("/", "-")
    lowered = SAFE_FILENAME_RE.sub("-", lowered).strip("-")
    lowered = re.sub(r"-{2,}", "-", lowered)
    return lowered[:max_len] or "document"


def stable_doc_basename(action_date: str, document_name: str, doc_id: str) -> str:
    prefix = action_date or "undated"
    document_name = re.sub(r"\.pdf$", "", document_name or "", flags=re.I)
    doc_hash = hashlib.sha1(doc_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{slugify(document_name)}_{doc_hash}"


def normalize_case_prefix(case_number: str) -> str:
    match = CASE_NUMBER_PREFIX_RE.match(case_number or "")
    return match.group(1) if match else ""


def day_dir(filing_iso: str) -> Path:
    return DATA_ROOT / filing_iso


def case_dir(filing_iso: str, case_number: str) -> Path:
    return day_dir(filing_iso) / case_number


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def update_day_summary(filing_iso: str, **fields: Any) -> dict[str, Any]:
    root = day_dir(filing_iso)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "day_summary.json"
    summary = load_json(path, {})
    summary.update(fields)
    summary.setdefault("filing_date", filing_iso)
    summary["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def write_day_level_failure(
    filing_iso: str,
    *,
    case_type: str,
    case_prefixes: set[str],
    include_patterns: list[re.Pattern[str]],
    exclude_patterns: list[re.Pattern[str]],
    error: str,
) -> dict[str, Any]:
    return update_day_summary(
        filing_iso,
        portal_case_type=case_type,
        case_prefixes=sorted(case_prefixes),
        case_type_include_patterns=[r.pattern for r in include_patterns],
        case_type_exclude_patterns=[r.pattern for r in exclude_patterns],
        run_error=error,
        failed_cases=0,
        scraped_cases=0,
        newly_scraped_cases=0,
        finished_at=utc_now_iso(),
    )


def write_failed_cases(filing_iso: str, failed_cases: list[dict[str, Any]]) -> None:
    path = day_dir(filing_iso) / "failed_cases.json"
    if failed_cases:
        path.write_text(json.dumps(failed_cases, indent=2), encoding="utf-8")
    elif path.exists():
        path.unlink()


def case_complete(filing_iso: str, case_number: str) -> bool:
    return (case_dir(filing_iso, case_number) / "register_of_actions.json").exists()


def normalize_name(value: str) -> str:
    if not value:
        return ""
    cleaned = value.replace("\u00a0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
    return cleaned.upper()


def build_party_name(party: dict[str, Any]) -> str:
    full_name = (party.get("fullName") or "").strip()
    if full_name:
        return full_name
    pieces = [party.get("firstName") or "", party.get("middleName") or "", party.get("lastName") or ""]
    joined = " ".join(piece.strip() for piece in pieces if piece and piece.strip()).strip()
    if joined:
        return joined
    return (party.get("businessName") or "").strip()


def build_attorney_name(attorney: dict[str, Any]) -> str:
    pieces = [attorney.get("firstName") or "", attorney.get("middleName") or "", attorney.get("lastName") or ""]
    return " ".join(piece.strip() for piece in pieces if piece and piece.strip()).strip()


def normalize_parties(raw_parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parties = []
    for party in raw_parties or []:
        name = build_party_name(party)
        parties.append(
            {
                "name": name,
                "type": party.get("type"),
                "is_defendant": bool(party.get("isDefendant")),
                "case_party_id": party.get("casePartyId"),
                "raw": party,
            }
        )
    return parties


def normalize_attorneys(raw_attorneys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attorneys = []
    for attorney in raw_attorneys or []:
        attorneys.append(
            {
                "name": build_attorney_name(attorney),
                "bar_number": attorney.get("barNumber"),
                "represented_party_text": (attorney.get("representing") or "").strip(),
                "is_lead": bool(attorney.get("isLead")),
                "raw": attorney,
            }
        )
    return attorneys


def build_attorney_party_links(
    parties: list[dict[str, Any]], attorneys: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    party_map = {normalize_name(party["name"]): party for party in parties}
    links = []
    represented_keys: dict[str, bool] = {}
    for attorney in attorneys:
        represented_raw = attorney.get("represented_party_text") or ""
        candidate_names = []
        if represented_raw.strip():
            candidate_names.append(represented_raw.strip())
        if ";" in represented_raw:
            candidate_names.extend(part.strip() for part in represented_raw.split(";") if part.strip())
        matched_any = False
        for candidate in candidate_names:
            party_key = normalize_name(candidate)
            party = party_map.get(party_key)
            if party:
                links.append(
                    {
                        "attorney_name": attorney["name"],
                        "bar_number": attorney.get("bar_number"),
                        "party_name": party["name"],
                        "party_role": party.get("type"),
                    }
                )
                represented_keys[party_key] = True
                matched_any = True
        if not matched_any and represented_raw:
            links.append(
                {
                    "attorney_name": attorney["name"],
                    "bar_number": attorney.get("bar_number"),
                    "party_name": None,
                    "party_role": None,
                    "represented_party_text": represented_raw,
                }
            )
    return links, represented_keys


def annotate_pro_se(
    parties: list[dict[str, Any]], represented_keys: dict[str, bool]
) -> list[dict[str, Any]]:
    out = []
    for party in parties:
        copy = dict(party)
        copy["pro_se"] = not represented_keys.get(normalize_name(party["name"]), False)
        out.append(copy)
    return out


def counsel_side_flags(parties: list[dict[str, Any]]) -> tuple[bool, bool]:
    plaintiff_has_counsel = False
    defendant_has_counsel = False
    for party in parties:
        role = (party.get("type") or "").lower()
        has_counsel = not party.get("pro_se", True)
        if "plaintiff" in role or "petitioner" in role:
            plaintiff_has_counsel = plaintiff_has_counsel or has_counsel
        if "defendant" in role or "respondent" in role:
            defendant_has_counsel = defendant_has_counsel or has_counsel
    return plaintiff_has_counsel, defendant_has_counsel


def unique_departments(raw_hearings: list[dict[str, Any]]) -> list[str]:
    seen = []
    for hearing in raw_hearings or []:
        calendar = (hearing.get("calendar") or "").strip()
        if calendar and calendar not in seen:
            seen.append(calendar)
    return seen


class AccessDeniedError(RuntimeError):
    pass


class PortalSession:
    def __init__(
        self,
        *,
        headless: bool,
        min_delay_s: float,
        max_delay_s: float,
        human_verify_on_start: bool,
        human_verify_on_deny: bool,
        human_verify_timeout_s: float,
    ) -> None:
        self.headless = headless
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s
        self.human_verify_on_start = human_verify_on_start
        self.human_verify_on_deny = human_verify_on_deny
        self.human_verify_timeout_s = human_verify_timeout_s
        self.browser_cm = None
        self.browser = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.case_token: str | None = None

    async def __aenter__(self) -> "PortalSession":
        self.browser_cm = AsyncCamoufox(headless=self.headless, humanize=True)
        self.browser = await self.browser_cm.__aenter__()
        self.context = await self.browser.new_context(ignore_https_errors=True, accept_downloads=True)
        self.page = await self.context.new_page()
        await self.page.goto(SEARCH_URL, wait_until="networkidle", timeout=90_000)
        if self.human_verify_on_start:
            await self.wait_for_human_verification("startup")
        try:
            await self.refresh_case_token()
        except AccessDeniedError as exc:
            # The portal gated us before we could grab a session token.
            # Fall back to the manual-verify flow when a window is visible
            # so the user can solve the reCAPTCHA once; subsequent token
            # refreshes inside the session reuse the cleared CF cookies.
            if self.headless:
                raise RuntimeError(
                    f"Portal gated startup token fetch and the browser is headless: {exc}. "
                    "Re-run without --headless so the reCAPTCHA can be solved manually."
                ) from exc
            print(f"\nStartup token fetch was blocked ({exc}); falling back to manual verification.\n")
            await self.wait_for_human_verification("startup-token-block")
            await self.refresh_case_token()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.context is not None:
            await self.context.close()
        if self.browser_cm is not None:
            await self.browser_cm.__aexit__(exc_type, exc, tb)

    async def sleep(self, floor: float | None = None, ceil: float | None = None) -> None:
        await asyncio.sleep(random.uniform(floor or self.min_delay_s, ceil or self.max_delay_s))

    async def refresh_case_token(self) -> str:
        assert self.context is not None
        response = await self.context.request.get(TOKEN_API)
        text = await response.text()
        parsed = self._parse_json(text)
        token = (parsed or {}).get("token")
        if response.status != 200 or not token:
            raise AccessDeniedError(f"Unable to refresh case token: status={response.status} body={text[:200]!r}")
        self.case_token = token
        if self.page is not None:
            await self.page.evaluate(
                """token => {
                    sessionStorage.setItem('ctoken', token);
                    return sessionStorage.getItem('ctoken');
                }""",
                token,
            )
        return token

    async def wait_for_human_verification(self, reason: str) -> str:
        if self.headless:
            raise RuntimeError("Human verification requires a visible browser. Re-run with --headful.")
        assert self.page is not None
        page = self.page
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=90_000)
        print(
            f"\nHUMAN VERIFICATION REQUIRED ({reason}).\n"
            "A browser window is on the Santa Clara search page.\n"
            "Solve the visible reCAPTCHA if it appears, then leave the page open.\n"
            "The scraper will resume automatically once sessionStorage.ctoken is present.\n"
        )
        deadline = time.monotonic() + self.human_verify_timeout_s
        last_notice = 0.0
        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError("Verification page was closed before a token was granted.")
            token = await page.evaluate("sessionStorage.getItem('ctoken') || ''")
            if token:
                self.case_token = token
                print("Human verification detected a valid ctoken; resuming.\n")
                return token
            now = time.monotonic()
            if now - last_notice > 15:
                last_notice = now
                try:
                    recaptcha_count = await page.locator(".recaptcha iframe, iframe[src*='recaptcha']").count()
                except Exception:
                    recaptcha_count = 0
                print(f"Waiting for manual verification... recaptcha_iframes={recaptcha_count}")
            await asyncio.sleep(2.0)
        raise RuntimeError(
            f"Timed out after {self.human_verify_timeout_s:.0f}s waiting for manual verification."
        )

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        attempts: int = 3,
    ) -> Any:
        assert self.context is not None
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if not self.case_token:
                await self.refresh_case_token()
            headers = {"case-token": self.case_token or ""}
            if json_payload is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)
            await self.sleep()
            try:
                response = await self.context.request.fetch(
                    url,
                    method=method,
                    headers=headers,
                    data=(json.dumps(json_payload) if json_payload is not None else None),
                )
            except Exception as exc:
                # Playwright raises TimeoutError when the portal stops
                # responding (throttle / IP cooldown). Treat as a soft
                # block so the day-level retry loop can apply backoff
                # instead of crashing the whole run.
                last_error = AccessDeniedError(
                    f"{method} {url} request error on attempt {attempt}: {type(exc).__name__}: {str(exc)[:200]}"
                )
                # Back off and refresh the token before retrying.
                await self.sleep(self.max_delay_s, self.max_delay_s + 2.0)
                try:
                    await self.refresh_case_token()
                except Exception:
                    pass
                continue
            text = await response.text()
            parsed = self._parse_json(text)
            if self._looks_blocked(response.status, text, parsed):
                last_error = AccessDeniedError(f"{method} {url} blocked on attempt {attempt}: {text[:200]!r}")
                if self.human_verify_on_deny:
                    try:
                        await self.wait_for_human_verification(f"deny on {method} {url}")
                        continue
                    except Exception as exc:
                        last_error = AccessDeniedError(
                            f"{method} {url} blocked and manual verification failed: {exc}"
                        )
                await self.refresh_case_token()
                await self.sleep(self.max_delay_s, self.max_delay_s + 1.5)
                continue
            if response.status >= 400:
                raise RuntimeError(f"{method} {url} failed: {response.status} {text[:300]!r}")
            if parsed is None:
                raise RuntimeError(f"{method} {url} returned non-JSON: {text[:300]!r}")
            return parsed
        raise last_error or AccessDeniedError(f"{method} {url} was blocked repeatedly")

    async def search_by_filing_dates(self, date_from: str, date_to: str, case_type: str) -> list[dict[str, Any]]:
        payload = {"dateFrom": date_from, "dateTo": date_to, "caseType": case_type}
        response = await self.request_json("POST", SEARCH_API, json_payload=payload)
        return list((response or {}).get("data") or [])

    async def fetch_case_payload(self, case_id: str) -> dict[str, Any]:
        response = await self.request_json("GET", CASE_API_TEMPLATE.format(case_id=quote(case_id, safe="")))
        data = (response or {}).get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"Case {case_id} response missing data payload")
        return data

    async def fetch_document_bytes(self, doc_id: str) -> tuple[bytes, str, dict[str, Any]]:
        response = await self.request_json("GET", DOC_API_TEMPLATE.format(doc_id=doc_id))
        data = (response or {}).get("data") or {}
        contents = data.get("contents")
        if not contents:
            raise RuntimeError(f"Document {doc_id} missing base64 payload")
        raw_bytes = base64.b64decode(contents)
        doc_type = (data.get("docType") or "pdf").lower()
        extension = "pdf" if doc_type == "pdf" else doc_type
        return raw_bytes, extension, data

    async def capture_search_page_artifacts(self, out_dir: Path, *, date_from: str, date_to: str, case_type: str) -> None:
        if self.page is None:
            return
        network: list[dict[str, Any]] = []
        page = self.page
        page.on(
            "request",
            lambda req: network.append(
                {
                    "kind": "request",
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                }
            ),
        )
        page.on(
            "response",
            lambda resp: network.append(
                {
                    "kind": "response",
                    "url": resp.url,
                    "status": resp.status,
                }
            ),
        )
        await page.goto(
            f"{PORTAL_URL}/search/filedate?dateFrom={quote(date_from)}&dateTo={quote(date_to)}&caseType={quote(case_type)}",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        await page.wait_for_selector("#tblFileDateSearchResults", state="attached", timeout=30_000)
        await asyncio.sleep(4)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "search_results_page.html").write_text(await page.content(), encoding="utf-8")
        (out_dir / "search_results_network.json").write_text(json.dumps(network, indent=2), encoding="utf-8")
        await page.screenshot(path=str(out_dir / "search_results_page.png"), full_page=True)

    async def capture_case_page_artifacts(self, out_dir: Path, *, case_id: str, case_number: str) -> None:
        assert self.context is not None
        page = await self.context.new_page()
        network: list[dict[str, Any]] = []
        page.on(
            "request",
            lambda req: network.append(
                {
                    "kind": "request",
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                }
            ),
        )
        page.on(
            "response",
            lambda resp: network.append(
                {
                    "kind": "response",
                    "url": resp.url,
                    "status": resp.status,
                }
            ),
        )
        if self.case_token:
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=90_000)
            await page.evaluate("token => sessionStorage.setItem('ctoken', token)", self.case_token)
        await page.goto(case_url(case_id), wait_until="domcontentloaded", timeout=90_000)
        case_out_dir = out_dir / case_number
        case_out_dir.mkdir(parents=True, exist_ok=True)
        capture_note = {"status": "ok"}
        try:
            await page.wait_for_selector("#caseDetails, .big-danger-text", timeout=20_000)
            await asyncio.sleep(3)
        except Exception as exc:
            capture_note = {
                "status": "timeout",
                "reason": str(exc),
                "url": page.url,
                "title": await page.title(),
            }
        (case_out_dir / "case_page.html").write_text(await page.content(), encoding="utf-8")
        (case_out_dir / "case_page_network.json").write_text(json.dumps(network, indent=2), encoding="utf-8")
        (case_out_dir / "case_page_capture.json").write_text(json.dumps(capture_note, indent=2), encoding="utf-8")
        await page.screenshot(path=str(case_out_dir / "case_page.png"), full_page=True)
        await page.close()

    @staticmethod
    def _parse_json(text: str) -> Any | None:
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _looks_blocked(status: int, text: str, parsed: Any | None) -> bool:
        lowered = text.lower()
        if status == 403:
            return True
        if "access denied" in lowered:
            return True
        if "case search session expired" in lowered:
            return True
        if parsed is None and text.strip().startswith("<!doctype html"):
            return True
        return False


def filter_search_rows(rows: list[dict[str, Any]], case_prefixes: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept = []
    skipped = []
    for row in rows:
        prefix = normalize_case_prefix(row.get("caseNumber") or "")
        if prefix in case_prefixes:
            kept.append(row)
        else:
            skipped.append(row)
    return kept, skipped


def filter_rows_by_case_type_regex(
    rows: list[dict[str, Any]],
    *,
    include_regexes: list[re.Pattern[str]],
    exclude_regexes: list[re.Pattern[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not include_regexes and not exclude_regexes:
        return rows, []
    kept = []
    skipped = []
    for row in rows:
        case_type = row.get("caseType") or ""
        if include_regexes and not any(regex.search(case_type) for regex in include_regexes):
            skipped.append(row)
            continue
        if exclude_regexes and any(regex.search(case_type) for regex in exclude_regexes):
            skipped.append(row)
            continue
        kept.append(row)
    return kept, skipped


def expand_event_like_rows(
    rows: list[dict[str, Any]],
    *,
    source_section: str,
) -> list[dict[str, Any]]:
    actions = []
    for row in rows or []:
        date_iso = normalize_date(row.get("date"))
        base_action = {
            "date": date_iso,
            "date_text": row.get("date"),
            "proceedings": row.get("type") or row.get("documentName") or "",
            "source_section": source_section,
            "source_row_id": row.get("eventId") or row.get("hearingId"),
            "filed_by": row.get("filedBy"),
            "comment": row.get("comment"),
            "department": row.get("calendar"),
            "time": row.get("time"),
            "hearing_result": row.get("hearingResult"),
            "hearing_flag": row.get("hearingFlag"),
            "doc_id": None,
            "doc_name": None,
            "doc_url": None,
            "doc_filename": None,
            "raw": row,
        }
        documents = list(row.get("documents") or [])
        if not documents:
            actions.append(base_action)
            continue
        for document in documents:
            action = dict(base_action)
            action["doc_id"] = document.get("documentId")
            action["doc_name"] = document.get("documentName")
            if action["doc_id"]:
                action["doc_url"] = DOC_API_TEMPLATE.format(doc_id=document["documentId"])
            action["raw_document"] = document
            actions.append(action)
    return actions


def expand_top_level_documents(rows: list[dict[str, Any]], *, source_section: str) -> list[dict[str, Any]]:
    actions = []
    for document in rows or []:
        doc_id = document.get("documentVersionId") or document.get("documentId")
        date_iso = normalize_date(document.get("effectiveDate") or document.get("fileDate"))
        actions.append(
            {
                "date": date_iso,
                "date_text": document.get("effectiveDate") or document.get("fileDate"),
                "proceedings": document.get("documentName") or source_section,
                "source_section": source_section,
                "source_row_id": document.get("documentVersionId") or document.get("documentId"),
                "filed_by": None,
                "comment": None,
                "department": None,
                "time": None,
                "hearing_result": None,
                "hearing_flag": None,
                "doc_id": doc_id,
                "doc_name": document.get("documentName"),
                "doc_url": DOC_API_TEMPLATE.format(doc_id=doc_id) if doc_id else None,
                "doc_filename": None,
                "raw": document,
            }
        )
    return actions


async def _run_ocr_and_finalize(pdf_path: Path) -> dict[str, Any]:
    """OCR a saved PDF and return telemetry shaped to merge into an action.

    Mirrors the OK-scraper pipeline so detection_pilot scripts see the
    same fields across counties: text_filename, text_chars, text_pages,
    text_letter_frac, text_extraction_status, text_extraction_elapsed_s,
    ocr_engine. On status="ok" the PDF is deleted and replaced by the
    .txt unless KEEP_PDFS is True.
    """
    if not RUN_OCR:
        return {
            "text_extraction_status": "skipped",
            "text_filename": None,
            "doc_filename_kept": pdf_path.name,
        }
    try:
        ocr_res = await asyncio.to_thread(_ocr_pdf, pdf_path)
    except Exception as e:
        return {
            "text_extraction_status": "error",
            "text_extraction_error": f"thread error: {str(e)[:200]}",
            "text_extraction_elapsed_s": 0.0,
            "doc_filename_kept": pdf_path.name,
        }
    out = {
        "text_chars": ocr_res.get("chars", 0),
        "text_pages": ocr_res.get("pages", 0),
        "text_letter_frac": ocr_res.get("letter_frac", 0.0),
        "text_extraction_status": ocr_res.get("status", "error"),
        "text_extraction_elapsed_s": ocr_res.get("elapsed_s", 0.0),
        "ocr_engine": ocr_res.get("engine", "unknown"),
        "text_filename": None,
        "doc_filename_kept": pdf_path.name,
    }
    if ocr_res.get("error"):
        out["text_extraction_error"] = str(ocr_res["error"])[:200]
    text = ocr_res.get("text") or ""
    if text:
        txt_path = pdf_path.with_suffix(".txt")
        try:
            txt_path.write_text(text)
            out["text_filename"] = txt_path.name
        except Exception as e:
            out["text_extraction_error"] = f"write failed: {str(e)[:120]}"
    if out["text_extraction_status"] == "ok" and not KEEP_PDFS:
        try:
            pdf_path.unlink()
            out["doc_filename_kept"] = None
        except Exception:
            pass
    return out


async def download_case_documents(
    session: PortalSession,
    case_actions: list[dict[str, Any]],
    case_output_dir: Path,
    *,
    max_docs_per_case: int,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    downloaded: dict[str, str] = {}
    failures: list[dict[str, Any]] = []
    skipped_due_to_cap: list[dict[str, Any]] = []
    pending_ocr: list[tuple[dict[str, Any], asyncio.Task, str, Path]] = []
    attempts = 0
    for action in case_actions:
        doc_id = action.get("doc_id")
        doc_name = action.get("doc_name") or "document"
        if not doc_id:
            continue
        if doc_id in downloaded:
            action["doc_filename"] = downloaded[doc_id]
            continue
        if attempts >= max_docs_per_case:
            action["doc_download_skipped_reason"] = "per_case_cap"
            skipped_due_to_cap.append(
                {
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "date": action.get("date"),
                    "proceedings": action.get("proceedings"),
                }
            )
            continue
        try:
            raw_bytes, extension, raw_doc_payload = await session.fetch_document_bytes(doc_id)
            basename = stable_doc_basename(action.get("date") or "", doc_name, doc_id)
            filename = f"{basename}.{extension}"
            pdf_path = case_output_dir / filename
            pdf_path.write_bytes(raw_bytes)
            downloaded[doc_id] = filename
            action["doc_filename"] = filename
            if HEARTBEAT is not None:
                HEARTBEAT.increment("session_docs_collected")
            action["raw_document_payload"] = {
                "name": raw_doc_payload.get("name"),
                "docId": raw_doc_payload.get("docId"),
                "documentVersionId": raw_doc_payload.get("documentVersionId"),
                "pageCount": raw_doc_payload.get("pageCount"),
                "docType": raw_doc_payload.get("docType"),
            }
            # Kick off OCR as a background task (matches OK pipelining).
            # We drain and merge all results once doc downloads finish.
            if extension.lower() == "pdf":
                ocr_task = asyncio.create_task(_run_ocr_and_finalize(pdf_path))
                pending_ocr.append((action, ocr_task, doc_id, pdf_path))
        except Exception as exc:
            error_text = str(exc)
            action["doc_download_error"] = error_text
            failures.append(
                {
                    "doc_id": doc_id,
                    "doc_name": doc_name,
                    "date": action.get("date"),
                    "proceedings": action.get("proceedings"),
                    "error": error_text,
                }
            )
        attempts += 1

    # Drain pipelined OCR tasks; record per-doc_id final state so we can
    # propagate to ALL action rows that reference the same doc_id (SC
    # cases routinely list the same document on multiple events; only the
    # first occurrence runs OCR but every action needs the final filename
    # + OCR telemetry, otherwise duplicates point at a deleted .pdf).
    OCR_FIELDS = (
        "text_filename", "text_chars", "text_pages", "text_letter_frac",
        "text_extraction_status", "text_extraction_elapsed_s", "ocr_engine",
        "text_extraction_error",
    )
    final_state: dict[str, dict[str, Any]] = {}
    if pending_ocr:
        ocr_results = await asyncio.gather(
            *(t for _, t, _, _ in pending_ocr), return_exceptions=True
        )
        for (action, _, doc_id, pdf_path), ocr_result in zip(pending_ocr, ocr_results):
            if isinstance(ocr_result, BaseException):
                ocr_result = {
                    "text_extraction_status": "error",
                    "text_extraction_error": f"task failure: {str(ocr_result)[:160]}",
                    "text_extraction_elapsed_s": 0.0,
                    "doc_filename_kept": pdf_path.name if pdf_path.exists() else None,
                }
            ocr_fields: dict[str, Any] = {k: ocr_result[k] for k in OCR_FIELDS if k in ocr_result}
            kept_pdf_name = ocr_result.get("doc_filename_kept")
            final_state[doc_id] = {
                "doc_filename": kept_pdf_name,  # None when PDF was deleted post-OCR
                "ocr_fields": ocr_fields,
            }
            txt_name = ocr_result.get("text_filename")
            if kept_pdf_name is None and txt_name:
                downloaded[doc_id] = txt_name

    # Reconcile every action: propagate OCR results to duplicates, and
    # backfill doc_filename for non-OCR'd downloads.
    for action in case_actions:
        doc_id = action.get("doc_id")
        if not doc_id:
            continue
        if doc_id in final_state:
            state = final_state[doc_id]
            action["doc_filename"] = state["doc_filename"]
            for k, v in state["ocr_fields"].items():
                action[k] = v
        elif doc_id in downloaded and not action.get("doc_filename") and not action.get("text_filename"):
            action["doc_filename"] = downloaded[doc_id]
    return downloaded, failures, skipped_due_to_cap


def build_register_payload(
    *,
    search_row: dict[str, Any],
    case_payload: dict[str, Any],
) -> dict[str, Any]:
    raw_parties = case_payload.get("caseParties") or []
    raw_attorneys = case_payload.get("caseAttornies") or []
    raw_hearings = case_payload.get("caseHearings") or []
    parties = normalize_parties(raw_parties)
    attorneys = normalize_attorneys(raw_attorneys)
    links, represented_keys = build_attorney_party_links(parties, attorneys)
    parties = annotate_pro_se(parties, represented_keys)
    plaintiff_has_counsel, defendant_has_counsel = counsel_side_flags(parties)
    actions = []
    actions.extend(expand_event_like_rows(case_payload.get("caseEvents") or [], source_section="event"))
    actions.extend(expand_event_like_rows(raw_hearings, source_section="hearing"))
    actions.extend(expand_top_level_documents(case_payload.get("caseDocuments") or [], source_section="document"))
    actions.extend(expand_top_level_documents(case_payload.get("caseOtherDocuments") or [], source_section="other_document"))

    departments = unique_departments(raw_hearings)
    metadata = {
        "case_number": case_payload.get("caseNumber") or search_row.get("caseNumber"),
        "case_id": case_payload.get("id") or search_row.get("caseId"),
        "case_title": case_payload.get("style") or search_row.get("caseStyle"),
        "filing_date": normalize_date(case_payload.get("fileDate") or search_row.get("filingDate")),
        "filing_date_text": case_payload.get("fileDate") or search_row.get("filingDate"),
        "case_type": case_payload.get("type") or search_row.get("caseType"),
        "case_subtype": case_payload.get("caseSubType"),
        "case_category": case_payload.get("caseCategory"),
        "case_status": case_payload.get("status") or search_row.get("caseStatus"),
        "court_location": case_payload.get("courtLocation"),
        "department": departments[0] if len(departments) == 1 else None,
        "departments": departments,
        "judge": None,
        "security_group": case_payload.get("securityGroup"),
        "portal_case_url": case_url(str(case_payload.get("id") or search_row.get("caseId"))),
        "portal_search_case_type": search_row.get("caseType"),
        "portal_node_id": case_payload.get("nodeId") or search_row.get("nodeId"),
        "case_number_prefix": normalize_case_prefix(case_payload.get("caseNumber") or search_row.get("caseNumber") or ""),
        "parties": parties,
        "attorneys": attorneys,
        "attorney_party_links": links,
        "plaintiff_has_counsel": plaintiff_has_counsel,
        "defendant_has_counsel": defendant_has_counsel,
        "n_parties": len(parties),
        "n_attorneys": len(attorneys),
        "n_events": len(case_payload.get("caseEvents") or []),
        "n_hearings": len(raw_hearings),
        "n_actions": len(actions),
        "source": {
            "portal": "Santa Clara Superior Court Public Portal",
            "search_row": search_row,
            "raw_case": case_payload,
        },
        "timing": {"scraped_at": utc_now_iso()},
    }
    return {"metadata": metadata, "actions": actions}


async def calibrate_cases(
    session: PortalSession,
    *,
    filing_day: str,
    case_type: str,
    sample_rows: list[dict[str, Any]],
    sample_limit: int,
) -> None:
    out_dir = CALIBRATION_ROOT / filing_day
    out_dir.mkdir(parents=True, exist_ok=True)
    await session.capture_search_page_artifacts(out_dir, date_from=filing_day, date_to=filing_day, case_type=case_type)
    raw_results = await session.search_by_filing_dates(filing_day, filing_day, case_type)
    (out_dir / "search_results.json").write_text(json.dumps(raw_results, indent=2), encoding="utf-8")

    kept = 0
    for row in sample_rows:
        if kept >= sample_limit:
            break
        case_id = str(row["caseId"])
        case_number = row["caseNumber"]
        try:
            case_payload = await session.fetch_case_payload(case_id)
        except Exception as exc:
            (out_dir / f"{case_number}_error.json").write_text(
                json.dumps({"case_id": case_id, "case_number": case_number, "error": str(exc)}, indent=2),
                encoding="utf-8",
            )
            continue
        case_dir_out = out_dir / case_number
        case_dir_out.mkdir(parents=True, exist_ok=True)
        (case_dir_out / "case_payload.json").write_text(json.dumps(case_payload, indent=2), encoding="utf-8")
        (case_dir_out / "parties.json").write_text(
            json.dumps(case_payload.get("caseParties") or [], indent=2), encoding="utf-8"
        )
        (case_dir_out / "attorneys.json").write_text(
            json.dumps(case_payload.get("caseAttornies") or [], indent=2), encoding="utf-8"
        )
        (case_dir_out / "events.json").write_text(
            json.dumps(case_payload.get("caseEvents") or [], indent=2), encoding="utf-8"
        )
        (case_dir_out / "hearings.json").write_text(
            json.dumps(case_payload.get("caseHearings") or [], indent=2), encoding="utf-8"
        )
        (case_dir_out / "documents.json").write_text(
            json.dumps(
                {
                    "caseDocuments": case_payload.get("caseDocuments") or [],
                    "caseOtherDocuments": case_payload.get("caseOtherDocuments") or [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        await session.capture_case_page_artifacts(out_dir, case_id=case_id, case_number=case_number)
        kept += 1


async def scrape_day(
    session: PortalSession,
    *,
    filing_day: str,
    case_type: str,
    case_prefixes: set[str],
    include_case_type_regexes: list[re.Pattern[str]],
    exclude_case_type_regexes: list[re.Pattern[str]],
    max_cases: int | None,
    offset_cases: int,
    max_docs_per_case: int,
    force: bool,
    calibrate: bool,
    calibration_cases: int,
) -> dict[str, Any]:
    search_rows = await session.search_by_filing_dates(filing_day, filing_day, case_type)
    kept_rows, skipped_prefix_rows = filter_search_rows(search_rows, case_prefixes)
    kept_rows, skipped_type_rows = filter_rows_by_case_type_regex(
        kept_rows,
        include_regexes=include_case_type_regexes,
        exclude_regexes=exclude_case_type_regexes,
    )
    if offset_cases:
        kept_rows = kept_rows[offset_cases:]
    if max_cases:
        kept_rows = kept_rows[:max_cases]
    if calibrate:
        await calibrate_cases(
            session,
            filing_day=filing_day,
            case_type=case_type,
            sample_rows=kept_rows,
            sample_limit=calibration_cases,
        )

    failed_cases = []
    scraped_cases = 0
    skipped_existing = 0
    downloaded_documents = 0

    update_day_summary(
        filing_day,
        portal_case_type=case_type,
        raw_search_rows=len(search_rows),
        total_cases=len(kept_rows),
        skipped_nonmatching_prefix=len(skipped_prefix_rows),
        skipped_case_type_filter=len(skipped_type_rows),
        case_prefixes=sorted(case_prefixes),
        case_type_include_patterns=[r.pattern for r in include_case_type_regexes],
        case_type_exclude_patterns=[r.pattern for r in exclude_case_type_regexes],
        offset_cases=offset_cases,
        started_at=utc_now_iso(),
    )

    for index, row in enumerate(kept_rows, start=1):
        case_number = row["caseNumber"]
        if HEARTBEAT is not None:
            HEARTBEAT.update(current_case=case_number,
                             current_action=f"case {index}/{len(kept_rows)}")
        if not force and case_complete(filing_day, case_number):
            skipped_existing += 1
            continue
        try:
            case_payload = await session.fetch_case_payload(str(row["caseId"]))
            register = build_register_payload(search_row=row, case_payload=case_payload)
            cdir = case_dir(filing_day, case_number)
            cdir.mkdir(parents=True, exist_ok=True)
            downloaded, doc_failures, doc_skips = await download_case_documents(
                session,
                register["actions"],
                cdir,
                max_docs_per_case=max_docs_per_case,
            )
            register["metadata"]["timing"].update(
                {
                    "scraped_at": utc_now_iso(),
                    "sequence_in_day": index,
                    "downloaded_documents": len(downloaded),
                    "document_download_failures": len(doc_failures),
                    "document_download_skipped_due_to_cap": len(doc_skips),
                }
            )
            register["metadata"]["downloaded_documents"] = list(downloaded.values())
            register["metadata"]["document_download_failures"] = doc_failures
            register["metadata"]["document_download_skipped_due_to_cap"] = doc_skips
            (cdir / "register_of_actions.json").write_text(json.dumps(register, indent=2), encoding="utf-8")
            scraped_cases += 1
            if HEARTBEAT is not None:
                HEARTBEAT.increment("session_cases_scraped")
            downloaded_documents += len(downloaded)
        except Exception as exc:
            failed_cases.append(
                {
                    "case_number": case_number,
                    "case_id": row.get("caseId"),
                    "title": row.get("caseStyle"),
                    "case_url": case_url(str(row.get("caseId"))),
                    "reason": str(exc),
                    "search_row": row,
                }
            )

    write_failed_cases(filing_day, failed_cases)
    complete_cases = scraped_cases + skipped_existing
    summary = update_day_summary(
        filing_day,
        portal_case_type=case_type,
        raw_search_rows=len(search_rows),
        total_cases=len(kept_rows),
        skipped_nonmatching_prefix=len(skipped_prefix_rows),
        skipped_case_type_filter=len(skipped_type_rows),
        scraped_cases=complete_cases,
        newly_scraped_cases=scraped_cases,
        failed_cases=len(failed_cases),
        skipped_existing=skipped_existing,
        downloaded_documents=downloaded_documents,
        finished_at=utc_now_iso(),
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Santa Clara Superior Court civil scraper")
    parser.add_argument("--start-date", required=True, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--case-type", default="Civil", help="Portal filing-date case type. Default: Civil")
    parser.add_argument(
        "--case-prefix",
        action="append",
        default=["CV"],
        help="Case-number prefix to keep after the broad portal search. Default: CV",
    )
    parser.add_argument(
        "--case-type-include-regex",
        action="append",
        default=[],
        help="Keep only rows whose portal caseType matches this regex. Repeatable.",
    )
    parser.add_argument(
        "--case-type-exclude-regex",
        action="append",
        default=[],
        help="Drop rows whose portal caseType matches this regex. Repeatable.",
    )
    parser.add_argument("--max-cases", type=int, default=0, help="Optional cap per day, for smoke runs")
    parser.add_argument("--offset-cases", type=int, default=0, help="Skip the first N kept search rows before scraping")
    parser.add_argument("--max-docs-per-case", type=int, default=5, help="Polite per-case document cap")
    parser.add_argument("--day-retries", type=int, default=2, help="How many times to retry a filing day after access denial")
    parser.add_argument("--day-backoff-s", type=float, default=20.0, help="Sleep before retrying a blocked filing day")
    parser.add_argument("--headful", action="store_true",
                        help="(Deprecated, kept for backward-compat — headful is now the default.)")
    parser.add_argument("--headless", action="store_true",
                        help="Run the browser without a visible window. Captcha gates will fail "
                             "without a manual solver, so only useful for debug or warm sessions.")
    parser.add_argument("--no-ocr", action="store_true",
                        help="Skip the inline OCR pass; keep PDFs as-is. Useful for debugging.")
    parser.add_argument("--keep-pdfs", action="store_true",
                        help="Retain PDFs after successful OCR (default: delete to save space).")
    parser.add_argument(
        "--human-verify-on-start",
        action="store_true",
        help="Open the public search page and wait for a manual reCAPTCHA/session grant before scraping.",
    )
    parser.add_argument(
        "--human-verify-on-deny",
        action="store_true",
        help="When the API returns Access denied, pause on the search page and wait for a manual re-verification.",
    )
    parser.add_argument(
        "--human-verify-timeout-s",
        type=float,
        default=300.0,
        help="How long to wait for manual verification when a headful browser is open.",
    )
    parser.add_argument("--force", action="store_true", help="Re-scrape cases even if register_of_actions.json exists")
    parser.add_argument("--calibrate", action="store_true", help="Save raw page/API calibration artifacts")
    parser.add_argument("--calibration-cases", type=int, default=3, help="Sample case count for calibration output")
    parser.add_argument("--min-delay-s", type=float, default=0.6, help="Minimum sleep between API requests")
    parser.add_argument("--max-delay-s", type=float, default=1.4, help="Maximum sleep between API requests")
    parser.add_argument("--data-root", default=None,
                        help="Override the output root (default: santa_clara_scraper/data). "
                             "Useful for writing to an external drive.")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    # Headful by default; --headless opts out. Captcha gates require a
    # visible browser to solve, so this matches OK's Camoufox usage.
    global RUN_OCR, KEEP_PDFS, DATA_ROOT, CALIBRATION_ROOT, HEARTBEAT
    RUN_OCR = not args.no_ocr
    KEEP_PDFS = args.keep_pdfs or args.no_ocr
    if args.data_root:
        DATA_ROOT = Path(args.data_root).expanduser().resolve()
        CALIBRATION_ROOT = DATA_ROOT / "_calibration"
        print(f"Using data root: {DATA_ROOT}")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    HEARTBEAT = Heartbeat(DATA_ROOT, scraper="sc", args=sys.argv[1:])
    HEARTBEAT.update(
        start_date=args.start_date, end_date=args.end_date,
        case_type=args.case_type,
        case_prefixes=list(case_prefixes_for_intent(args)),
        include_regexes=list(args.case_type_include_regex),
        exclude_regexes=list(args.case_type_exclude_regex),
        max_cases=args.max_cases or None,
        max_docs_per_case=args.max_docs_per_case,
        headless=args.headless,
        rotation_managed=os.environ.get("ROTATE_MANAGED") == "1",
        current_ip=_probe_public_ip(),
        session_cases_scraped=0,
        session_docs_collected=0,
    )
    HEARTBEAT.start()
    case_prefixes = {prefix.upper() for prefix in args.case_prefix}
    include_case_type_regexes = [re.compile(pattern, re.I) for pattern in args.case_type_include_regex]
    exclude_case_type_regexes = [re.compile(pattern, re.I) for pattern in args.case_type_exclude_regex]
    summaries = []
    failed_days = []
    for day in daterange(args.start_date, args.end_date):
        day_iso = day.isoformat()
        if HEARTBEAT is not None:
            HEARTBEAT.update(current_day=day_iso, current_case=None,
                             current_action="day-start")
        last_error = None
        for attempt in range(1, args.day_retries + 2):
            try:
                async with PortalSession(
                    headless=args.headless,
                    min_delay_s=args.min_delay_s,
                    max_delay_s=args.max_delay_s,
                    # Default the deny-fallback ON now that headful is the
                    # default; the manual solve is always available.
                    human_verify_on_start=args.human_verify_on_start,
                    human_verify_on_deny=args.human_verify_on_deny or not args.headless,
                    human_verify_timeout_s=args.human_verify_timeout_s,
                ) as session:
                    summary = await scrape_day(
                        session,
                        filing_day=day_iso,
                        case_type=args.case_type,
                        case_prefixes=case_prefixes,
                        include_case_type_regexes=include_case_type_regexes,
                        exclude_case_type_regexes=exclude_case_type_regexes,
                        max_cases=(args.max_cases or None),
                        offset_cases=max(args.offset_cases, 0),
                        max_docs_per_case=args.max_docs_per_case,
                        force=args.force,
                        calibrate=args.calibrate,
                        calibration_cases=args.calibration_cases,
                    )
                    summaries.append(summary)
                    print(json.dumps(summary, indent=2))
                    last_error = None
                    break
            except AccessDeniedError as exc:
                last_error = str(exc)
                if attempt <= args.day_retries:
                    await asyncio.sleep(args.day_backoff_s)
                    continue
                failed_days.append({"filing_date": day_iso, "error": last_error})
                failure_summary = write_day_level_failure(
                    day_iso,
                    case_type=args.case_type,
                    case_prefixes=case_prefixes,
                    include_patterns=include_case_type_regexes,
                    exclude_patterns=exclude_case_type_regexes,
                    error=last_error,
                )
                summaries.append(failure_summary)
                print(json.dumps(failure_summary, indent=2))
                break
        if last_error and day_iso not in {item["filing_date"] for item in failed_days}:
            failed_days.append({"filing_date": day_iso, "error": last_error})
    if HEARTBEAT is not None:
        HEARTBEAT.close(
            status="exited",
            finished_reason=("failed_days" if failed_days else "completed"),
            failed_days=len(failed_days),
        )
    return 1 if failed_days else 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

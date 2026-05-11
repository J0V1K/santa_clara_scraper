# Santa Clara Scraper

Local-only scraper for the Santa Clara County Superior Court public portal.

This first phase follows the `ok_scraper` output layout but uses a different
collection path:

- bootstrap a real browser session with Camoufox / Playwright
- fetch a `case-token` from the portal
- call the portal JSON APIs for filing-date search, case detail, and document
  payloads through the browser context

Output layout:

```text
santa_clara_scraper/data/
├── 2024-01-02/
│   ├── day_summary.json
│   ├── failed_cases.json
│   ├── 24CV428427/
│   │   ├── register_of_actions.json
│   │   └── 2024-09-30_judgment-satisfaction_<hash>.pdf
│   └── ...
└── _calibration/
```

Example smoke run:

```bash
detection_pilot/.venv/bin/python santa_clara_scraper/scraper.py \
  --start-date 2024-01-02 --end-date 2024-01-02 \
  --max-cases 3 --calibrate
```

Target richer civil matters instead of the first `CV` rows:

```bash
detection_pilot/.venv/bin/python santa_clara_scraper/scraper.py \
  --start-date 2024-01-04 --end-date 2024-01-04 \
  --case-type-include-regex 'Auto|Contract|Employment|Fraud|Malpractice' \
  --max-cases 5 --max-docs-per-case 4
```

Range run with day-level retry/backoff:

```bash
detection_pilot/.venv/bin/python santa_clara_scraper/scraper.py \
  --start-date 2024-01-02 --end-date 2024-01-04 \
  --case-type-include-regex 'Auto|Contract|Employment|Fraud|Malpractice' \
  --max-cases 1 --day-retries 1 --day-backoff-s 25
```

Current schema choice:

- top-level `register_of_actions.json` is `{ "metadata": ..., "actions": [...] }`
- `metadata` keeps normalized case fields plus raw `search_row` and raw case JSON
- `actions` flattens event, hearing, and top-level document rows so each linked
  document has its own action entry with `doc_id`, `doc_url`, and `doc_filename`
- when the per-case document cap is reached, skipped documents are recorded in
  `metadata.document_download_skipped_due_to_cap` and on the action row via
  `doc_download_skipped_reason`
- document fetch failures are recorded in
  `metadata.document_download_failures` instead of failing the entire case

Deployment notes:

- The scraper now starts a fresh browser session per filing day.
- Day-level retries and backoff are available for transient `Access denied.`
  responses.
- Direct non-browser HTTP is still blocked by the portal; keep using the
  Playwright/Camoufox bootstrap path.

# Santa Clara Portal Reconnaissance

Observed on 2026-05-09 / 2026-05-10.

## Actual portal behavior

- Plain `curl` to `https://portal.scscourt.org/search` returns `403 Access denied.`
- A Camoufox / Playwright browser session loads the portal successfully.
- The public search UI is an AngularJS app. The landing HTML is mostly a shell;
  the real search UI is loaded from `/Scripts/views/search.html`.
- The filing-date workflow is not HTML form scraping. The client calls:
  - `GET /api/case/token`
  - `POST /api/cases/byfilingdates`
  - `GET /api/case/{id}`
  - `GET /api/doc/base64/doc?docId=...`

## Search flow

- The search page advertises a 5-business-day filing-date limit in the UI.
- The client-side controller posts JSON like:
  `{"dateFrom":"2024-01-02","dateTo":"2024-01-02","caseType":"Civil"}`
- The API requires a `case-token` header. Without it, the filing-date endpoint
  returns `400 {"message": "Case Search Session Expired."}`.
- In a real browser session, `GET /api/case/token` returned a usable token
  without an interactive reCAPTCHA solve during reconnaissance.
- Filing-date search for `caseType=Civil` returns a broad civil bucket, not just
  `CV` cases. Example for `2024-01-02`:
  - 154 rows total
  - prefixes included `CV`, `SC`, and `CH`
  - `CV` rows were 133 of 154

## Case detail structure

- Case detail is one JSON payload with arrays including:
  - `caseParties`
  - `caseAttornies`
  - `caseEvents`
  - `caseHearings`
  - `caseDocuments`
  - `caseOtherDocuments`
  - `relatedCases`
- For civil cases, document IDs usually appear on event/hearing rows inside a
  `documents` array, even when top-level `caseDocuments` is empty.
- Example hearing rows include a department-like `calendar` field such as
  `Department 3`.
- No obvious judge field was found in the public civil payloads inspected.

## Document path

- `GET /api/doc/base64/doc?docId=...` returned base64-encoded PDF content.
- The front-end also references `/api/doc/download/base64/{docId}` and
  `/api/doc/thumbnail/base64/{docId}`, but direct requests to those paths
  returned `404` during reconnaissance. The working path for the first phase is
  `doc/base64/doc`.

## Anti-bot / blocking observations

- Direct non-browser HTTP is denied.
- Repeated case-detail probing in quick succession intermittently returned
  non-JSON / blocked responses, so the scraper should pace requests and refresh
  the token or browser context on `Access denied.` / session-expired failures.
- In longer multi-day pilots, the portal could still flip to `Access denied.`
  after consecutive filing days. A fresh browser session per filing day plus
  day-level retry/backoff materially improved stability in testing.

## First-phase strategy

- Use filing-date search one day at a time.
- Keep only `CV` case numbers for the initial civil pass.
- Treat the portal JSON APIs as the primary source of truth, with Playwright
  used to bootstrap the session and to save calibration HTML / screenshots.

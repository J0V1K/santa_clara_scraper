const REFRESH_MS = 15000;

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}

function daysInMonth(year, month) {
  return new Date(year, month, 0).getDate();
}

function firstWeekday(year, month) {
  return new Date(year, month - 1, 1).getDay();
}

function formatRelative(iso) {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const deltaSec = Math.max(0, (Date.now() - then) / 1000);
  if (deltaSec < 60) return `${Math.round(deltaSec)}s ago`;
  if (deltaSec < 3600) return `${Math.round(deltaSec / 60)}m ago`;
  if (deltaSec < 86400) return `${Math.round(deltaSec / 3600)}h ago`;
  return `${Math.round(deltaSec / 86400)}d ago`;
}

function renderTotals(totals) {
  const root = document.getElementById("totals");
  root.innerHTML = "";
  const pairs = [
    ["Days tracked", totals.days_tracked],
    ["Days complete", totals.days_complete],
    ["Days in progress", totals.days_in_progress],
    ["Days with failures", totals.days_with_failures],
    ["Days with run error", totals.days_with_run_error],
    ["Cases scraped", totals.cases_scraped],
    ["Cases total", totals.cases_total],
  ];
  for (const [label, value] of pairs) {
    const cell = document.createElement("div");
    cell.className = "stat";
    cell.innerHTML = `<span class="label">${label}</span><span class="value">${value.toLocaleString()}</span>`;
    root.appendChild(cell);
  }
}

function renderRate(rate) {
  document.getElementById("rate-hour").textContent = rate.cases_last_hour.toLocaleString();
  document.getElementById("rate-24h").textContent = rate.cases_last_24h.toLocaleString();
  document.getElementById("rate-7d").textContent = rate.cases_last_7d.toLocaleString();
  document.getElementById("rate-pdf-24h").textContent = rate.pdfs_last_24h.toLocaleString();
  document.getElementById("last-activity").textContent = formatRelative(rate.last_activity_at);
}

function renderCalendar(days) {
  const byDate = new Map(days.map((d) => [d.date, d]));
  const root = document.getElementById("calendar");
  root.innerHTML = "";

  if (!days.length) {
    root.innerHTML = `<p class="empty">No filing-day folders found under the data root.</p>`;
    return;
  }

  const firstYear = parseDate(days[0].date).y;
  const lastYear = parseDate(days[days.length - 1].date).y;

  for (let year = lastYear; year >= firstYear; year--) {
    const yearEl = document.createElement("div");
    yearEl.className = "year";
    yearEl.innerHTML = `<h2>${year}</h2>`;
    const grid = document.createElement("div");
    grid.className = "year-grid";

    for (let month = 1; month <= 12; month++) {
      const monthEl = document.createElement("div");
      monthEl.className = "month";
      monthEl.innerHTML = `<h3>${MONTH_NAMES[month - 1]}</h3>`;
      const cal = document.createElement("div");
      cal.className = "days";

      const offset = firstWeekday(year, month);
      for (let i = 0; i < offset; i++) {
        const blank = document.createElement("div");
        blank.className = "day blank";
        cal.appendChild(blank);
      }

      const total = daysInMonth(year, month);
      for (let d = 1; d <= total; d++) {
        const iso = `${year}-${String(month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
        const info = byDate.get(iso);
        const cell = document.createElement("div");
        cell.className = "day";
        cell.classList.add(info ? info.status.replace("_", "-") : "untouched");
        cell.textContent = d;
        cell.dataset.date = iso;
        if (info) {
          cell.dataset.status = info.status;
          cell.dataset.total = info.total;
          cell.dataset.scraped = info.scraped;
          cell.dataset.missing = Math.max(0, info.total - info.scraped);
          cell.dataset.failed = info.failed;
          cell.dataset.runError = info.run_error || "";
        } else {
          cell.dataset.status = "untouched";
          cell.dataset.runError = "";
        }
        cal.appendChild(cell);
      }
      monthEl.appendChild(cal);
      grid.appendChild(monthEl);
    }
    yearEl.appendChild(grid);
    root.appendChild(yearEl);
  }
}

async function refresh() {
  try {
    const resp = await fetch("/api/status", { cache: "no-store" });
    if (!resp.ok) throw new Error(`status ${resp.status}`);
    const payload = await resp.json();
    document.getElementById("data-root").textContent = payload.data_root;
    document.getElementById("generated-at").textContent = `updated ${formatRelative(payload.generated_at)}`;
    renderTotals(payload.totals);
    renderRate(payload.rate);
    renderCalendar(payload.days);
  } catch (err) {
    console.error("refresh failed", err);
  }
}

function tooltipHtml(cell) {
  const { date, status, runError } = cell.dataset;
  if (status === "untouched") {
    return `<div class="tt-title">${date}</div><div class="tt-line tt-muted">Untouched</div>`;
  }
  const total = Number(cell.dataset.total || 0);
  const scraped = Number(cell.dataset.scraped || 0);
  const missing = Number(cell.dataset.missing || 0);
  const failed = Number(cell.dataset.failed || 0);
  const statusLabel = status.replace(/_/g, " ");
  const lines = [
    `<div class="tt-title">${date}</div>`,
    `<div class="tt-line"><span>Scraped</span><span>${scraped.toLocaleString()} / ${total.toLocaleString()}</span></div>`,
    `<div class="tt-line"><span>Missing</span><span>${missing.toLocaleString()}</span></div>`,
  ];
  if (failed > 0) {
    lines.push(`<div class="tt-line"><span>Failed</span><span>${failed.toLocaleString()}</span></div>`);
  }
  lines.push(`<div class="tt-line tt-muted"><span>Status</span><span>${statusLabel}</span></div>`);
  if (runError) {
    lines.push(`<div class="tt-error">${runError}</div>`);
  }
  return lines.join("");
}

function positionTooltip(tooltip, cell) {
  const rect = cell.getBoundingClientRect();
  const ttRect = tooltip.getBoundingClientRect();
  const gap = 6;
  let top = rect.top + window.scrollY - ttRect.height - gap;
  let left = rect.left + window.scrollX + rect.width / 2 - ttRect.width / 2;
  if (top < window.scrollY + 4) {
    top = rect.bottom + window.scrollY + gap;
  }
  left = Math.max(4 + window.scrollX, Math.min(left, window.scrollX + document.documentElement.clientWidth - ttRect.width - 4));
  tooltip.style.top = `${top}px`;
  tooltip.style.left = `${left}px`;
}

function setupTooltip() {
  const tooltip = document.getElementById("tooltip");
  const calendar = document.getElementById("calendar");
  calendar.addEventListener("mouseover", (e) => {
    const cell = e.target.closest(".day");
    if (!cell || cell.classList.contains("blank")) return;
    tooltip.innerHTML = tooltipHtml(cell);
    tooltip.hidden = false;
    positionTooltip(tooltip, cell);
  });
  calendar.addEventListener("mouseout", (e) => {
    const cell = e.target.closest(".day");
    if (!cell) return;
    const next = e.relatedTarget?.closest?.(".day");
    if (next && next !== cell) return;
    tooltip.hidden = true;
  });
  window.addEventListener("scroll", () => { tooltip.hidden = true; }, { passive: true });
}

setupTooltip();
refresh();
setInterval(refresh, REFRESH_MS);

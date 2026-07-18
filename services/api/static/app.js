/* Dashboard (PLAN §6.8): ranked feed + trust badges + sparklines + health states.
   Vanilla JS against the read API; every render treats API strings as text
   (textContent, never innerHTML) — item text is attacker-controlled. */

"use strict";

const REFRESH_MS = 30_000;
const SPARK_ROWS = 20; // fetch sparklines for the top rows only

const $ = (sel) => document.querySelector(sel);

// Feed tabs (§6.5/§6.8): "gap" = the ranked target feed; "corroborated" and
// "primary" are trust watchlists (each exactly that trust_state, kept visible even
// at gap≈0 before they age out); "weather" segregates NOAA/NWS alerts so they don't
// clutter the news feed. Order/keys here must match STORY_QUERIES and the tab ids.
const FEED_VIEWS = ["gap", "corroborated", "primary", "weather"];
let feedView = "gap";
const FEED_HEADINGS = {
  gap: 'Ranked by gap score <span class="hint">velocity × (1−mainstream) × corroboration × (1−inauthenticity), weather aside</span>',
  corroborated: 'Corroborated watchlist <span class="hint">exactly corroborated — kept visible even at low gap</span>',
  primary: 'Primary-backed watchlist <span class="hint">backed by a primary/authoritative source — kept visible even at low gap</span>',
  weather: 'Weather <span class="hint">NOAA/NWS active alerts, kept out of the news feed</span>',
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

function rel(ts) {
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}

/* ── health banners (§6.8 system states) ─────────────────────────────────── */

function renderBanners(health, degraded) {
  const box = $("#banners");
  box.replaceChildren();
  if (degraded) box.append(el("span", "banner critical", "DB failover in progress — showing last-known data"));
  if (health?.baseline_warming) box.append(el("span", "banner warning", "baseline warming — velocity not yet trusted"));
  if (health?.sampling_active) box.append(el("span", "banner warning", "sampling active — social firehose being shed"));
  if (!box.children.length) box.append(el("span", "banner ok", "all signals nominal"));
}

function renderSystem(health) {
  const box = $("#system");
  box.replaceChildren();
  const parts = health?.components ?? {};
  for (const [key, v] of Object.entries(parts)) {
    const row = el("div", "kv");
    const label = el("b", null, key);
    row.append(label, document.createTextNode(
      key.startsWith("enrich:")
        ? ` θ=${(v.theta ?? 0).toFixed(2)} ${v.sampling_active ? "sampling" : "full"}`
        : ` scored=${v.stories_scored ?? "?"} alerts=${v.alerts_raised ?? 0}${v.baseline_warming ? " warming" : ""}`,
    ));
    box.append(row);
  }
  if (!box.children.length) box.append(el("div", "empty", "no components reporting yet"));
}

/* ── sparkline: single series, one hue, min–max scaled inline SVG ────────── */

function sparkline(points, w = 120, h = 28) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("role", "img");
  if (!points.length) return svg;
  const max = Math.max(...points.map((p) => p.mentions), 1);
  const step = w / Math.max(points.length - 1, 1);
  const xy = points.map((p, i) => `${(i * step).toFixed(1)},${(h - 2 - (p.mentions / max) * (h - 6)).toFixed(1)}`);
  const area = document.createElementNS(svg.namespaceURI, "polygon");
  area.setAttribute("class", "fillarea");
  area.setAttribute("points", `0,${h} ${xy.join(" ")} ${w},${h}`);
  const line = document.createElementNS(svg.namespaceURI, "polyline");
  line.setAttribute("points", xy.join(" "));
  const title = document.createElementNS(svg.namespaceURI, "title");
  title.textContent = `mentions per bin, peak ${max}`;
  svg.append(title, area, line);
  return svg;
}

/* ── story feed ──────────────────────────────────────────────────────────── */

function meter(frac) {
  const m = el("span", "meter");
  const track = el("span", "track");
  const fill = el("span", "fill");
  fill.style.width = `${Math.round(frac * 100)}%`;
  track.append(fill);
  m.append(track, el("span", null, `${Math.round(frac * 100)}% mainstream`));
  return m;
}

// The story whose detail is expanded, so it survives the 30s feed refresh
// (self_notes #1): a background poll must not yank the row the user is reading.
let openStoryId = null;

async function fillDetail(box, storyId) {
  try {
    const { data } = await getJSON(`/api/stories/${storyId}?items=15`);
    const rows = [];
    for (const it of data.items) {
      const item = el("div", "item");
      item.append(
        el("div", "src", `${it.source} · ${it.source_class} · ${rel(it.ts_observed)}`),
        el("p", "txt", it.text),
      );
      rows.push(item);
    }
    if (!rows.length) rows.push(el("div", "empty", "items aged out"));
    box.replaceChildren(...rows); // swap in one shot — no loading flash on refresh
  } catch {
    box.replaceChildren(el("div", null, "failed to load items"));
  }
}

// Open (or reuse) a detail box on `row` and load it. Reusing the prior box across a
// refresh keeps the already-rendered items on screen while fresh ones load in.
function openDetail(row, storyId, box) {
  if (!box) { box = el("div", "story-detail", "loading…"); }
  row.append(box);
  openStoryId = storyId;
  fillDetail(box, storyId);
}

function toggleDetail(row, story) {
  const open = row.querySelector(".story-detail");
  if (open) { open.remove(); openStoryId = null; return; }
  openDetail(row, story.id);
}

function storyRow(s, withSpark) {
  const row = el("article", "story");
  const head = el("div", "story-head");
  head.append(
    el("span", "gap-num", s.gap.toFixed(2)),
    el("span", `badge ${s.trust_state}`, s.trust_state.replace("_", " ")),
    el("span", "entities", s.entity_set.join(", ") || "(no entities)"),
  );
  const meta = el("div", "story-meta");
  const spark = el("span", "spark-slot");
  if (withSpark) {
    getJSON(`/api/stories/${s.id}/velocity`)
      .then(({ data }) => spark.append(sparkline(data.points)))
      .catch(() => {});
  }
  meta.append(
    spark,
    el("span", null, `vz ${s.velocity_z.toFixed(2)}`),
    meter(s.mainstream_presence),
    el("span", null, `${s.independent_origins} origin${s.independent_origins === 1 ? "" : "s"}`),
    el("span", null, `${s.item_count ?? "?"} items · ${s.source_set.join(", ")}`),
    el("span", null, `active ${rel(s.last_seen)}`),
  );
  row.append(head, meta);
  row.addEventListener("click", () => toggleDetail(row, s));
  return row;
}

const EMPTY_MSG = {
  gap: "no active stories in the window",
  corroborated: "no corroborated stories in the window yet",
  primary: "no primary-backed stories in the window yet",
  weather: "no active weather alerts in the window",
};

function renderFeed(stories) {
  const feed = $("#feed");
  // Detach the open detail box before the wipe so we can re-attach it to the
  // rebuilt row — the expanded story stays put across the refresh (self_notes #1).
  const prevBox = openStoryId ? feed.querySelector(".story-detail") : null;
  feed.replaceChildren();
  stories.forEach((s, i) => {
    const row = storyRow(s, i < SPARK_ROWS);
    feed.append(row);
    if (s.id === openStoryId) openDetail(row, s.id, prevBox);
  });
  if (!stories.length) {
    feed.append(el("div", "empty", EMPTY_MSG[feedView] ?? EMPTY_MSG.gap));
  }
}

function renderAlerts(alerts) {
  const box = $("#alerts");
  box.replaceChildren();
  for (const a of alerts) {
    const row = el("div", "alert-row");
    row.append(
      el("div", null, `${a.entity_set.join(", ") || a.story_id} — gap ${a.gap.toFixed(2)} (${a.trust_state.replace("_", " ")})`),
      el("div", "when", rel(a.ts)),
    );
    box.append(row);
  }
  if (!alerts.length) box.append(el("div", "empty", "no alerts"));
}

/* ── refresh loop ────────────────────────────────────────────────────────── */

async function refresh() {
  let degraded = false;
  try {
    const [stories, alerts, health] = await Promise.all([
      getJSON(`/api/stories?limit=50&sort=${feedView}`),
      getJSON("/api/alerts?limit=20"),
      getJSON("/api/health"),
    ]);
    degraded = stories.db_degraded || alerts.db_degraded || health.db_degraded;
    renderFeed(stories.data);
    renderAlerts(alerts.data);
    renderSystem(health.data ?? health);
    renderBanners(health.data ?? health, degraded);
    $("#refreshed").textContent = new Date().toLocaleTimeString();
  } catch {
    renderBanners(null, true); // API itself unreachable — worst-case banner
  }
}

/* ── feed view tabs (gap feed · trust watchlists · weather) ──────────────── */

function setView(view) {
  if (view === feedView) return;
  feedView = view;
  $("#feed-heading").innerHTML = FEED_HEADINGS[view]; // static trusted strings only
  for (const id of FEED_VIEWS) {
    const tab = $(`#tab-${id}`);
    const active = id === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  refresh();
}
for (const id of FEED_VIEWS) $(`#tab-${id}`).addEventListener("click", () => setView(id));

refresh();
setInterval(refresh, REFRESH_MS);

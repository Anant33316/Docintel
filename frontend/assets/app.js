/* DocIntel console. Plain JavaScript, no build step, no external libraries.
 *
 * Two modes, detected at start-up:
 *   local  - served by local_server.py: same-origin API, no key, uploads via POST /local/upload,
 *            per-document "provenance" says what was extracted, heuristic, a fixture or unavailable.
 *   remote - opened from disk or a static host against the AWS API: API URL + key from Settings,
 *            uploads via the presigned S3 URL from POST /documents/upload-url.
 * Every number and list shown comes from an API response; nothing is hard-coded.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ utilities
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (n) => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const store = {
    get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable: keep in memory */ } },
  };
  const fmtTime = (s) => {
    if (!s) return "–";
    const d = new Date(typeof s === "number" ? s * 1000 : s);
    return isNaN(d) ? String(s) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  };
  const fmtBytes = (n) => (n == null ? "–" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const todayUTC = () => new Date().toISOString().slice(0, 10);
  function highlight(text, terms) {
    let out = esc(text);
    for (const t of terms || []) {
      if (t.length < 2) continue;
      out = out.replace(new RegExp(`\\b(${t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\w*)`, "gi"), "<mark>$1</mark>");
    }
    return out;
  }

  // ------------------------------------------------------------------ state
  const S = {
    mode: null, base: "", key: "", user: store.get("di.user") || "web-user",
    local: null, fixtures: new Set(), docs: null, doc: null, firstRoute: true,
  };
  const isLocal = () => S.mode === "local";
  const isFixture = (id) => S.fixtures.has(id);

  // ------------------------------------------------------------------ API
  async function api(method, path, body) {
    if (!isLocal() && !S.base) {
      const e = new Error("Set the API URL in Settings to connect to your DocIntel API.");
      e.needsConfig = true;
      throw e;
    }
    const headers = { "X-User-Id": S.user };
    if (!isLocal() && S.key) headers["x-api-key"] = S.key;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let r;
    try {
      r = await fetch(S.base + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw new Error(isLocal() ? "Cannot reach the local server. Check that local_server.py is still running, then retry."
        : "Cannot reach the API. Check the API URL in Settings and your network connection.");
    }
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    if (!r.ok) {
      const err = new Error(data.error || data.message || `Request failed (HTTP ${r.status}).`);
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  async function loadDocs(force) {
    if (!S.docs || force) S.docs = (await api("GET", "/documents")).documents || [];
    updateReviewCount();
    return S.docs;
  }
  function invalidate() { S.docs = null; S.doc = null; }

  // ------------------------------------------------------------------ vocabulary
  const STATUS = {
    COMPLETED: ["Processed", "ok"], FAILED: ["Failed", "bad"], PROCESSING: ["Processing", "warn"],
  };
  const statusPill = (s) => { const [l, c] = STATUS[s] || [s || "Unknown", ""]; return `<span class="pill ${c}">${esc(l)}</span>`; };
  function reviewPill(d) {
    if (d.status !== "COMPLETED") return `<span class="muted">–</span>`;
    if (d.reviewStatus === "PENDING_REVIEW") return `<span class="pill warn">Needs review</span>`;
    if (d.reviewStatus === "REVIEWED") return `<span class="pill ok">Reviewed</span>`;
    if (isLocal() && !isFixture(d.docId)) return `<span class="pill plain" title="Text-only extraction: no fields to review">No fields</span>`;
    return `<span class="pill plain">Not required</span>`;
  }
  const versionChip = (d) => (d.versionNumber ? `<span class="pill plain" title="Part of a version history">Version ${esc(d.versionNumber)}</span>` : "");
  const reviewRank = (d) => ({ PENDING_REVIEW: 0, REVIEWED: 2, NOT_REQUIRED: 3 }[d.reviewStatus] ?? 4);
  const sourcePill = (id) => (isLocal() ? (isFixture(id) ? `<span class="pill fixture">Demo fixture</span>` : `<span class="pill info plain">Uploaded</span>`) : "");
  const FIELD_STATUS = {
    REQUIRES_REVIEW: ["Needs review", "warn"], AUTO_ACCEPTED: ["Auto-accepted", "ok"], VERIFIED: ["Confirmed", "ok"],
    CORRECTED: ["Corrected", "info"], NO_CONFIDENCE: ["No confidence score", ""],
  };
  const fieldPill = (s) => { const [l, c] = FIELD_STATUS[s] || [s, ""]; return `<span class="pill ${c}">${esc(l)}</span>`; };

  const PROV_NAMES = { genuine: "Genuine", heuristic: "Heuristic", fixture: "Demo fixture", unavailable: "Unavailable" };
  const STAGE_NAMES = {
    textExtraction: "Text extraction", fields: "Form fields", tables: "Tables", confidence: "Confidence scores",
    summary: "Summary", category: "Category", intent: "Intent", language: "Language", entities: "Entities",
    keyPhrases: "Key phrases", keyDates: "Key dates", actions: "Required actions", qa: "Questions and answers",
    pii: "PII detection", comparisonAI: "AI comparison", search: "Search",
  };
  const AWS_LABEL = {
    textExtraction: "Amazon Textract", fields: "Amazon Textract", tables: "Amazon Textract", confidence: "Amazon Textract",
    entities: "Amazon Comprehend", keyPhrases: "Amazon Comprehend", language: "Amazon Comprehend", pii: "Amazon Comprehend",
  };
  /** Badge saying what produced a value. Local documents carry provenance; AWS documents are labelled by service. */
  function prov(doc, key) {
    const st = doc && doc.provenance && doc.provenance.stages && doc.provenance.stages[key];
    if (st) return `<span class="prov ${esc(st.status)}" title="${esc(st.label)}">${PROV_NAMES[st.status] || esc(st.status)}</span>`;
    if (isLocal()) return "";
    const aws = AWS_LABEL[key];
    return aws ? `<span class="prov genuine" title="Produced by ${aws}">${aws}</span>`
      : `<span class="prov ai" title="Generated by an AI model (Amazon Bedrock)">AI-generated</span>`;
  }
  const stageOf = (doc, key) => (doc && doc.provenance && doc.provenance.stages && doc.provenance.stages[key]) || null;

  // ------------------------------------------------------------------ rendering helpers
  const view = () => $("#view");
  function setView(html) { const v = view(); v.innerHTML = html; v.setAttribute("aria-busy", "false"); }
  const loading = (msg) => `<div class="loading"><span class="spinner" aria-hidden="true"></span>${esc(msg || "Loading…")}</div>`;
  const emptyState = (title, text, action) => `<div class="empty"><h3>${esc(title)}</h3>${text ? `<p>${text}</p>` : ""}${action || ""}</div>`;
  function errorPanel(err, retryLabel) {
    const cfg = err.needsConfig ? `<a class="btn primary" href="#/settings">Open Settings</a>` : "";
    return `<div class="notice bad" role="alert">${icon("alert")}<div><p><strong>${esc(err.message)}</strong></p>
      <p><button class="btn sm" type="button" data-retry>${esc(retryLabel || "Retry")}</button> ${cfg}</p></div></div>`;
  }
  function bindRetry(el, fn) { $$("[data-retry]", el).forEach((b) => (b.onclick = fn)); }
  function crumbs(parts) {
    $("#crumbs").innerHTML = parts.map(([label, href], i) => (i === parts.length - 1 ? `<strong>${esc(label)}</strong>`
      : `<a href="${href}">${esc(label)}</a> / `)).join("");
    document.title = `${parts[parts.length - 1][0]} – DocIntel`;
  }
  function toast(msg, kind) {
    const t = document.createElement("div");
    t.className = `toast ${kind || ""}`;
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), 4500);
  }
  function busy(btn, on, label) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; if (label) btn.textContent = label; }
    else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
  }
  function updateReviewCount() {
    const n = (S.docs || []).filter((d) => d.status === "COMPLETED" && d.reviewStatus === "PENDING_REVIEW").length;
    const el = $("#reviewCount");
    el.hidden = !n;
    el.textContent = n;
    el.setAttribute("aria-label", `${n} awaiting review`);
  }

  // ------------------------------------------------------------------ navigation shell
  function openNav(open) {
    $("#sidebar").classList.toggle("open", open);
    $("#scrim").hidden = !open;
    $("#menuBtn").setAttribute("aria-expanded", String(open));
    if (open) $("#nav a").focus();
  }
  $("#menuBtn").onclick = () => openNav(!$("#sidebar").classList.contains("open"));
  $("#scrim").onclick = () => openNav(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#sidebar").classList.contains("open")) { openNav(false); $("#menuBtn").focus(); } });

  function renderModeBox() {
    const box = $("#modeBox");
    if (isLocal()) {
      const pdf = S.local.pdfExtraction || {};
      box.innerHTML = `<strong><span class="mode-dot${pdf.available ? "" : " off"}"></span>Local demo</strong>
        In-memory data, no AWS calls.${pdf.available ? "" : "<br>PyMuPDF missing: PDF uploads are disabled."}`;
    } else {
      box.innerHTML = `<strong><span class="mode-dot remote${S.base ? "" : " off"}"></span>AWS API</strong>${S.base ? esc(new URL(S.base, location.href).host) : "Not configured"}`;
    }
  }

  // ------------------------------------------------------------------ router
  const ROUTES = [
    [/^\/dashboard$/, "dashboard", viewDashboard],
    [/^\/documents$/, "documents", viewDocuments],
    [/^\/documents\/([A-Za-z0-9_-]{1,64})(?:\/([a-z]+))?$/, "documents", viewDocument],
    [/^\/review$/, "review", viewReview],
    [/^\/anomalies$/, "anomalies", viewAnomalies],
    [/^\/search$/, "search", viewSearch],
    [/^\/compare$/, "compare", viewCompare],
    [/^\/audit$/, "audit", viewAudit],
    [/^\/settings$/, "settings", viewSettings],
  ];
  function parseHash() {
    const raw = (location.hash || "").replace(/^#/, "") || "/dashboard";
    const [path, qs] = raw.split("?");
    return { path, params: new URLSearchParams(qs || "") };
  }
  async function route() {
    openNav(false);
    const { path, params } = parseHash();
    const hit = ROUTES.find(([rx]) => rx.test(path));
    if (!hit) { location.replace("#/dashboard"); return; }
    const [rx, nav, fn] = hit;
    $$("#nav a").forEach((a) => (a.dataset.nav === nav ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
    view().setAttribute("aria-busy", "true");
    const m = path.match(rx);
    try {
      await fn(params, ...m.slice(1));
    } catch (err) {
      setView(errorPanel(err));
      bindRetry(view(), route);
    }
    if (!S.firstRoute && !(nav === "documents" && m[2])) view().focus({ preventScroll: true });
    S.firstRoute = false;
  }

  // ------------------------------------------------------------------ dashboard
  async function viewDashboard() {
    crumbs([["Dashboard"]]);
    if (!S.docs) setView(loading("Loading dashboard…"));
    const [s, docs, an] = await Promise.all([api("GET", "/stats"), loadDocs(true), api("GET", "/anomalies?status=open").catch(() => null)]);
    const D = s.documents;
    const pending = s.review.documentsPendingReview;
    const pct = (n) => (D.total ? (n / D.total) * 100 : 0);
    const fixtures = docs.filter((d) => isFixture(d.docId)).length;
    const recent = docs.slice(0, 6);
    const cats = Object.entries(s.categories || {});
    const maxCat = Math.max(1, ...cats.map(([, v]) => v));
    const ev = s.ai || {};
    const activity = [["Questions answered", ev.questionsAnswered], ["Searches", ev.searches], ["Field reviews", ev.fieldVerifications],
      ["PII scans", ev.piiScans], ["Redactions", ev.redactions], ["Exports", ev.exports], ["Comparisons", ev.comparisons]];
    const awsUsage = isLocal() ? [] : [["Textract pages", ev.textractPages], ["Bedrock tokens in / out", `${ev.bedrockInputTokens} / ${ev.bedrockOutputTokens}`],
      ["Average OCR confidence", ev.averageOcrConfidence != null ? `${ev.averageOcrConfidence}%` : "–"]];

    setView(`
      <div class="page-head"><div><h1>Dashboard</h1>
        <p>${D.total ? `${plural(D.total, "document")} in ${isLocal() ? "this local session" : "your account"}.` : "No documents yet."}</p></div>
        <div class="actions"><button class="btn" type="button" id="dashRefresh">${icon("refresh")}Refresh</button></div></div>

      <section class="ledger" aria-label="Document status">
        <a class="ledger-cell" href="#/documents"><span class="n">${D.total}</span><span class="l">Total documents</span></a>
        <a class="ledger-cell" href="#/documents?status=COMPLETED"><span class="n">${D.completed}</span><span class="l">Processed</span></a>
        <a class="ledger-cell ${pending ? "attention" : ""}" href="#/review"><span class="n">${pending}</span><span class="l">Need review</span></a>
        <a class="ledger-cell ${D.failed ? "bad" : ""}" href="#/documents?status=FAILED"><span class="n">${D.failed}</span><span class="l">Failed${D.processing ? `, ${D.processing} processing` : ""}</span></a>
        <div class="split" role="img" aria-label="${D.completed} processed, ${D.processing} processing, ${D.failed} failed">
          <span style="width:${pct(D.completed)}%;background:var(--genuine)"></span>
          <span style="width:${pct(D.processing)}%;background:var(--heuristic)"></span>
          <span style="width:${pct(D.failed)}%;background:var(--danger)"></span></div>
      </section>
      ${isLocal() ? `<p class="source-note">${plural(fixtures, "seeded demo fixture")} and ${plural(D.total - fixtures, "uploaded document")}. Data lives in memory and is cleared when the server stops.</p>` : ""}

      <div class="grid-2">
        <div class="stack"><section class="panel flush" aria-labelledby="recentH">
          <div class="panel-head"><div><h2 id="recentH">Recent documents</h2></div><a class="btn quiet sm" href="#/documents">All documents</a></div>
          ${recent.length ? `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>Document</th><th>Status</th><th>Review</th><th>Uploaded</th></tr></thead><tbody>
            ${recent.map((d) => `<tr><td class="primary-cell"><a class="doc-name" href="#/documents/${esc(d.docId)}">${esc(d.fileName)}</a>
              <span class="cell-sub">${sourcePill(d.docId)} ${esc(d.category || "")}</span></td>
              <td data-label="Status">${statusPill(d.status)}</td><td data-label="Review">${reviewPill(d)}</td>
              <td data-label="Uploaded" class="small muted">${fmtTime(d.createdAt)}</td></tr>`).join("")}
            </tbody></table></div>`
            : emptyState("No documents yet", "Upload a PDF to see it processed here.", `<button class="btn primary" type="button" data-upload>${icon("upload")}Upload document</button>`)}
        </section>
          <section class="panel" aria-labelledby="datesH"><div class="panel-head"><h2 id="datesH">Upcoming dates</h2></div>
            ${s.upcomingKeyDates.length ? `<table><tbody>${s.upcomingKeyDates.slice(0, 6).map((k) => `<tr><td class="num">${esc(k.date)}</td>
              <td><a href="#/documents/${esc(k.docId)}">${esc(k.fileName)}</a><span class="cell-sub">${esc(k.description || k.type)}</span></td></tr>`).join("")}</tbody></table>`
              : `<p class="muted small">No dated items from today onwards.</p>`}</section>

        </div>

        <div class="stack">
          <section class="panel" aria-labelledby="upH"><div class="panel-head"><div><h2 id="upH">Add a document</h2>
            <p>${isLocal() ? `PDF with selectable text, up to ${fmtBytes(S.local.maxUploadBytes)}. Processed on this computer.` : "PDF or image. Processed by AWS Textract, Comprehend and Bedrock."}</p></div></div>
            <button class="btn primary" type="button" data-upload>${icon("upload")}Upload document</button></section>

          ${an ? `<section class="panel" aria-labelledby="anH"><div class="panel-head"><div><h2 id="anH">Open findings</h2>
            <p>${plural(an.documentsChecked, "invoice")} checked by deterministic rules.</p></div><a class="btn quiet sm" href="#/anomalies">Review</a></div>
            <div class="actions">${["high", "medium", "low", "info"].map((k) => `<span class="pill ${SEV[k][1]}">${an.summary.openBySeverity[k]} ${SEV[k][0].toLowerCase()}</span>`).join("")}</div></section>` : ""}

          <section class="panel" aria-labelledby="catH"><div class="panel-head"><h2 id="catH">Categories</h2>${isLocal() ? `<span class="prov heuristic" title="${esc(S.local.capabilities.category.label)}">Heuristic</span>` : ""}</div>
            ${cats.length ? `<div class="bars">${cats.map(([k, v]) => `<div class="bar-row"><span>${esc(k)}</span><div class="bar" aria-hidden="true"><span style="width:${(v / maxCat) * 100}%"></span></div><span class="num">${v}</span></div>`).join("")}</div>`
              : `<p class="muted small">Appears once documents are processed.</p>`}</section>

          ${s.failures.recent.length ? `<section class="panel" aria-labelledby="failH"><div class="panel-head"><h2 id="failH">Recent failures</h2></div>
            ${s.failures.recent.slice(0, 5).map((f) => `<div class="section"><a href="#/documents/${esc(f.docId)}">${esc(f.fileName)}</a>
              <span class="cell-sub">${esc(f.error)}${f.error.length >= 200 ? "…" : ""}</span></div>`).join("")}</section>` : ""}

          <section class="panel" aria-labelledby="actH"><div class="panel-head"><h2 id="actH">Activity</h2></div>
            <dl class="facts">${activity.concat(awsUsage).map(([k, v]) => `<dt>${esc(k)}</dt><dd class="num">${esc(v ?? 0)}</dd>`).join("")}
            ${s.processingTimeSeconds.average != null ? `<dt>Average processing time</dt><dd class="num">${s.processingTimeSeconds.average < 0.01 ? "under 0.01 s" : `${esc(s.processingTimeSeconds.average)} s`}</dd>` : ""}</dl></section>
        </div>
      </div>`);
    $("#dashRefresh").onclick = () => { invalidate(); route(); };
    $$("[data-upload]").forEach((b) => (b.onclick = openUpload));
  }

  // ------------------------------------------------------------------ documents list
  const listState = { q: "", status: "", review: "", source: "", sort: "createdAt", dir: "desc", page: 1, size: 10 };
  async function viewDocuments(params) {
    crumbs([["Documents"]]);
    if (params.has("status")) { listState.status = params.get("status"); listState.page = 1; }
    if (!S.docs) setView(loading("Loading documents…"));
    const docs = await loadDocs(true);
    setView(`
      <div class="page-head"><div><h1>Documents</h1><p>Every uploaded document and its processing result. Select a name to open it.</p></div>
        <div class="actions"><button class="btn" type="button" id="docsRefresh">${icon("refresh")}Refresh</button>
        <button class="btn primary" type="button" data-upload>${icon("upload")}Upload document</button></div></div>
      <div class="toolbar" role="search">
        <label class="field grow">Find by name<input type="search" id="fq" value="${esc(listState.q)}" placeholder="e.g. invoice"></label>
        <label class="field">Status<select id="fs"><option value="">All</option>${Object.entries(STATUS).map(([k, [l]]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
        <label class="field">Review<select id="fr"><option value="">All</option><option value="PENDING_REVIEW">Needs review</option><option value="REVIEWED">Reviewed</option><option value="NOT_REQUIRED">${isLocal() ? "Not required / no fields" : "Not required"}</option></select></label>
        ${isLocal() ? `<label class="field">Source<select id="fsrc"><option value="">All</option><option value="upload">Uploaded</option><option value="fixture">Demo fixtures</option></select></label>` : ""}
      </div>
      <section class="panel flush" aria-label="Documents"><div id="docTable"></div></section>
      <p class="small muted" style="margin-top:10px">The API returns the 50 most recent documents.</p>`);
    $("#fs").value = listState.status; $("#fr").value = listState.review;
    if ($("#fsrc")) $("#fsrc").value = listState.source;
    const onFilter = () => {
      listState.q = $("#fq").value.trim(); listState.status = $("#fs").value; listState.review = $("#fr").value;
      listState.source = $("#fsrc") ? $("#fsrc").value : ""; listState.page = 1; renderDocTable(docs);
    };
    $("#fq").oninput = onFilter; $("#fs").onchange = onFilter; $("#fr").onchange = onFilter; if ($("#fsrc")) $("#fsrc").onchange = onFilter;
    $("#docsRefresh").onclick = () => { invalidate(); route(); };
    $$("[data-upload]").forEach((b) => (b.onclick = openUpload));
    renderDocTable(docs);
  }

  function renderDocTable(docs) {
    const st = listState;
    let rows = docs.filter((d) => (!st.q || (d.fileName || "").toLowerCase().includes(st.q.toLowerCase()))
      && (!st.status || d.status === st.status) && (!st.review || d.reviewStatus === st.review)
      && (!st.source || (st.source === "fixture") === isFixture(d.docId)));
    const key = { name: (d) => (d.fileName || "").toLowerCase(), status: (d) => d.status || "", category: (d) => d.category || "~",
      review: reviewRank, createdAt: (d) => d.createdAt || 0 }[st.sort];
    rows.sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (st.dir === "asc" ? 1 : -1); });
    const pages = Math.max(1, Math.ceil(rows.length / st.size));
    st.page = Math.min(st.page, pages);
    const shown = rows.slice((st.page - 1) * st.size, st.page * st.size);
    const th = (k, label) => `<th scope="col" aria-sort="${st.sort === k ? (st.dir === "asc" ? "ascending" : "descending") : "none"}">
      <button class="sort-btn" type="button" data-sort="${k}">${label}<span class="arrow" aria-hidden="true">${st.sort === k && st.dir === "asc" ? "▲" : "▼"}</span></button></th>`;
    const el = $("#docTable");
    if (!docs.length) {
      el.innerHTML = emptyState("No documents yet", isLocal() ? "Upload a PDF that contains selectable text. You can try one of the files in the project's samples folder." : "Upload a document to start.",
        `<button class="btn primary" type="button" data-upload>${icon("upload")}Upload document</button>`);
    } else if (!rows.length) {
      el.innerHTML = emptyState("No documents match these filters", "Clear the search box or set the filters back to All.", `<button class="btn" type="button" id="clearF">Clear filters</button>`);
      $("#clearF").onclick = () => { Object.assign(listState, { q: "", status: "", review: "", source: "" }); viewDocuments(new URLSearchParams()); };
    } else {
      el.innerHTML = `<div class="table-wrap"><table class="stack-sm"><caption class="sr-only">Documents, sorted by ${esc(st.sort)}</caption>
        <thead><tr>${th("name", "Document")}${th("status", "Status")}${th("category", "Category")}${th("review", "Review")}${th("createdAt", "Uploaded")}<th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${shown.map((d) => `<tr>
          <td class="primary-cell"><a class="doc-name" href="#/documents/${esc(d.docId)}">${esc(d.fileName)}</a>
            <span class="cell-sub">${sourcePill(d.docId)} ${versionChip(d)} ${d.pageCount ? plural(d.pageCount, "page") : ""}</span></td>
          <td data-label="Status">${statusPill(d.status)}</td>
          <td data-label="Category">${d.category ? esc(d.category) : `<span class="muted">–</span>`}</td>
          <td data-label="Review">${reviewPill(d)}</td>
          <td data-label="Uploaded" class="small muted">${fmtTime(d.createdAt)}</td>
          <td><a class="btn sm" href="#/documents/${esc(d.docId)}" aria-label="Open ${esc(d.fileName)}">Open</a></td></tr>`).join("")}</tbody></table></div>
        <div class="pager"><span>${rows.length === docs.length ? plural(rows.length, "document") : `${rows.length} of ${docs.length} documents`}</span>
          ${pages > 1 ? `<span class="actions"><button class="btn sm" type="button" data-page="-1" ${st.page === 1 ? "disabled" : ""}>Previous</button>
            <span>Page ${st.page} of ${pages}</span><button class="btn sm" type="button" data-page="1" ${st.page === pages ? "disabled" : ""}>Next</button></span>` : ""}</div>`;
    }
    $$("[data-sort]", el).forEach((b) => (b.onclick = () => {
      const k = b.dataset.sort;
      if (st.sort === k) st.dir = st.dir === "asc" ? "desc" : "asc"; else { st.sort = k; st.dir = k === "createdAt" ? "desc" : "asc"; }
      renderDocTable(docs); $(`[data-sort="${k}"]`, el).focus();
    }));
    $$("[data-page]", el).forEach((b) => (b.onclick = () => { st.page += Number(b.dataset.page); renderDocTable(docs); }));
    $$("[data-upload]", el).forEach((b) => (b.onclick = openUpload));
  }

  // ------------------------------------------------------------------ document detail
  const TABS = [
    ["overview", "Overview", "file"], ["invoice", "Invoice checks", "alert"], ["fields", "Fields & review", "review"],
    ["text", "Extracted text", "text"], ["ask", "Ask", "chat"], ["versions", "Versions", "compare"],
    ["privacy", "Privacy", "shield"], ["export", "Export", "download"], ["audit", "Audit", "audit"],
  ];
  async function viewDocument(params, id, tab) {
    tab = tab || "overview";
    if (!S.doc || S.doc.docId !== id) {
      setView(loading("Loading document…"));
      S.doc = await api("GET", `/documents/${id}`);
    }
    const d = S.doc;
    const done = d.status === "COMPLETED";
    const tabs = TABS.filter(([k]) => (k !== "text" || isLocal()) && (done || k === "overview" || k === "audit"));
    if (!tabs.some(([k]) => k === tab)) tab = "overview";
    crumbs([["Documents", "#/documents"], [d.fileName || d.docId]]);
    const hasOriginal = isLocal() && !isFixture(d.docId) && d.provenance;
    setView(`
      <div class="doc-head"><div style="min-width:0">
        <h1>${esc(d.fileName)}</h1>
        <div class="doc-meta">${statusPill(d.status)} ${sourcePill(d.docId)} ${versionChip(d)}
          <span>${d.pageCount != null ? plural(d.pageCount, "page") : ""}</span><span>Uploaded ${fmtTime(d.createdAt)}</span>
          <span class="wrap-any">ID <code>${esc(d.docId)}</code></span></div></div>
        <div class="actions">${hasOriginal ? `<a class="btn" href="/local/documents/${esc(d.docId)}/original" target="_blank" rel="noopener">${icon("external")}View original PDF</a>` : ""}
          <button class="btn" type="button" id="docRefresh">${icon("refresh")}Refresh</button></div></div>
      ${d.provenance ? `<div class="notice">${icon("info")}<div><p>${esc(d.provenance.notice)}</p></div></div>` : ""}
      <div class="tabs" role="tablist" aria-label="Document sections">
        ${tabs.map(([k, label, ic]) => `<button role="tab" id="tab-${k}" aria-controls="tabpanel" aria-selected="${k === tab}" tabindex="${k === tab ? 0 : -1}" data-tab="${k}">${icon(ic)}${label}</button>`).join("")}
      </div>
      <div id="tabpanel" role="tabpanel" aria-labelledby="tab-${tab}" tabindex="0"></div>`);
    $("#docRefresh").onclick = () => { S.doc = null; S.docs = null; route(); };
    const tabBtns = $$("[role=tab]");
    tabBtns.forEach((b, i) => {
      b.onclick = () => { location.hash = `#/documents/${d.docId}/${b.dataset.tab}`; };
      b.onkeydown = (e) => {
        const j = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? tabBtns.length - 1 : null;
        if (j == null) return;
        e.preventDefault();
        const t = tabBtns[(j + tabBtns.length) % tabBtns.length];
        t.focus(); t.click();
      };
    });
    if (!S.firstRoute && params !== null) { const sel = $(`[role=tab][aria-selected=true]`); if (sel && location.hash.split("/").length > 3) sel.focus(); }
    const panel = $("#tabpanel");
    const render = { overview: tabOverview, invoice: tabInvoice, fields: tabFields, text: tabText, ask: tabAsk, versions: tabVersions,
      privacy: tabPrivacy, export: tabExport, audit: tabAudit }[tab];
    try { await render(d, panel); } catch (err) { panel.innerHTML = errorPanel(err); bindRetry(panel, () => render(d, panel)); }
  }

  function tabOverview(d, el) {
    if (d.status !== "COMPLETED") {
      const ocr = /OCR/i.test(d.errorMessage || "");
      el.innerHTML = d.status === "FAILED"
        ? `<div class="notice bad" role="alert">${icon("alert")}<div><p><strong>Processing failed.</strong> ${esc(d.errorMessage || "No error message was recorded.")}</p>
            ${ocr ? `<p>Try a PDF exported from a word processor or accounting system; those contain selectable text.</p>` : ""}</div></div>
            <div style="max-width:760px">${stagesPanel(d)}</div>`
        : `<div class="notice warn">${icon("info")}<div><p>This document is still processing. Use Refresh to check again.</p></div></div>`;
      bindRetryStage(el, d);
      return;
    }
    const lines = (d.summary || "").split("\n").filter(Boolean);
    const intentSt = stageOf(d, "intent");
    const actionsSt = stageOf(d, "actions");
    const kd = d.keyDates || [];
    const acts = d.actions || [];
    const ents = d.entities || [];
    const stages = d.provenance ? Object.entries(d.provenance.stages) : [];
    el.innerHTML = `<div class="grid-2">
      <div class="stack">
        <section class="panel"><div class="section"><h3>Summary ${prov(d, "summary")}</h3>
          ${lines.length ? `<div class="summary-text">${lines.map((l) => `<p>${esc(l)}</p>`).join("")}</div>` : `<p class="muted">No summary available.</p>`}
          ${stageOf(d, "summary") ? `<p class="small muted">${esc(stageOf(d, "summary").label)}</p>` : ""}</div></section>

        <section class="panel"><div class="section"><h3>Key dates ${prov(d, "keyDates")}</h3>
          ${kd.length ? `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>Date</th><th>Type</th><th>What it refers to</th><th>Check</th></tr></thead><tbody>
            ${kd.map((k) => `<tr><td class="num primary-cell"${k.isoDate ? ` title="${esc(k.isoDate)}"` : ""}>${esc(k.date || k.isoDate)}</td>
              <td data-label="Type">${esc(k.type)}</td><td data-label="Refers to">${esc(k.description || "")}${k.sourceText ? `<q class="quote">${esc(k.sourceText)}</q>` : ""}</td>
              <td data-label="Check">${k.grounded ? `<span class="pill ok" title="The quoted text appears verbatim in the document">Found in text</span>` : `<span class="pill warn" title="The quoted text was not found in the document">Not found in text</span>`}</td></tr>`).join("")}
            </tbody></table></div>` : `<p class="muted">No dates were found.</p>`}</div>
          <div class="section"><h3>Required actions ${prov(d, "actions")}</h3>
          ${acts.length ? `<table class="stack-sm"><thead><tr><th>Action</th><th>Deadline</th><th>Responsible</th><th>Check</th></tr></thead><tbody>${acts.map((a) => `<tr><td class="primary-cell">${esc(a.action)}</td>
              <td data-label="Deadline">${esc(a.isoDeadline || a.deadline || "–")}</td><td data-label="Responsible">${esc(a.responsibleParty || "–")}</td>
              <td data-label="Check">${a.grounded ? `<span class="pill ok">Found in text</span>` : `<span class="pill warn">Not found in text</span>`}</td></tr>`).join("")}</tbody></table>`
            : `<p class="muted">${actionsSt && actionsSt.status === "unavailable" ? esc(actionsSt.label) : "No required actions were found."}</p>`}</div></section>

        <section class="panel"><div class="section"><h3>Entities ${prov(d, "entities")}</h3>
          ${ents.length ? `<div>${ents.map((e) => `<span class="chip">${esc(e.text)}<small>${esc(e.type)}${e.score != null ? `, ${Number(e.score).toFixed(2)}` : ""}</small></span>`).join("")}</div>` : `<p class="muted">None found.</p>`}
          ${stageOf(d, "entities") ? `<p class="small muted">${esc(stageOf(d, "entities").label)}</p>` : ""}</div>
          <div class="section"><h3>Key phrases ${prov(d, "keyPhrases")}</h3>
          ${(d.keyPhrases || []).length ? `<div>${d.keyPhrases.map((p) => `<span class="chip">${esc(p)}</span>`).join("")}</div>` : `<p class="muted">None found.</p>`}</div></section>
      </div>

      <div class="stack">
        <section class="panel"><h3 style="margin-bottom:10px">Classification</h3>
          <dl class="facts"><dt>Category</dt><dd>${esc(d.category || "–")} ${prov(d, "category")}</dd>
            <dt>Method</dt><dd>${esc(d.classificationMethod || "–")}</dd>
            <dt>Intent</dt><dd>${d.intent ? esc(d.intent) : `<span class="muted">${intentSt ? esc(intentSt.label) : "–"}</span>`}</dd>
            <dt>Language</dt><dd>${esc(d.language || "–")}${stageOf(d, "language") ? ` <span class="muted small">(default, not detected)</span>` : ""}</dd></dl></section>
        <section class="panel"><h3 style="margin-bottom:10px">Processing</h3>
          <dl class="facts"><dt>Pages</dt><dd class="num">${esc(d.pageCount ?? "–")}</dd>
            <dt>Characters</dt><dd class="num">${esc(d.charCount ?? "–")}</dd>
            <dt>Review fields</dt><dd>${esc((d.fields || []).length)}${(d.fields || []).some((f) => f.source === "deterministic-parser") ? " (from the invoice parser)" : ""}${(d.reviewSummary || {}).lowConfidence ? `, ${d.reviewSummary.lowConfidence} below threshold` : ""}</dd>
            <dt>Tables</dt><dd>${esc(d.tableCount ?? 0)}</dd>
            <dt>OCR confidence</dt><dd>${d.avgOcrConfidence != null ? `${esc(d.avgOcrConfidence)}%` : `<span class="muted">Not available</span>`}</dd>
            <dt>Indexed chunks</dt><dd class="num">${esc(d.chunkCount ?? "–")}</dd>
            <dt>Processing time</dt><dd>${d.processingMs == null ? "–" : d.processingMs < 1000 ? `${d.processingMs} ms` : `${(d.processingMs / 1000).toFixed(1)} s`}</dd>
            ${d.sizeBytes ? `<dt>File size</dt><dd>${fmtBytes(d.sizeBytes)}</dd>` : ""}</dl></section>
        ${stagesPanel(d)}
        ${stages.length ? `<section class="panel" aria-labelledby="provH"><h3 id="provH">How each result was produced</h3>
          <div class="prov-counts">${Object.keys(PROV_NAMES).map((k) => [k, stages.filter(([, st]) => st.status === k).length]).filter(([, n]) => n)
            .map(([k, n]) => `<span class="prov ${k}">${n} ${PROV_NAMES[k].toLowerCase()}</span>`).join("")}</div>
          <details class="prov-box"><summary>Show all ${stages.length} stages</summary>
          <ul class="prov-ledger">${stages.map(([k, st]) => `<li><span class="tick ${esc(st.status)}" aria-hidden="true"></span><div><b>${esc(STAGE_NAMES[k] || k)}</b>
            <span>${PROV_NAMES[st.status] || esc(st.status)}: ${esc(st.label)}</span></div></li>`).join("")}</ul></details></section>` : ""}
      </div></div>`;
    bindRetryStage(el, d);
  }

  // ---- fields & review (shared with the review queue)
  function fieldRows(fields, thr, docId) {
    return fields.map((f) => `<tr class="${f.requiresVerification ? "needs" : ""}" data-doc="${esc(docId)}" data-f="${esc(f.fieldId)}" data-key="${esc(f.key)}">
      <td class="primary-cell"><strong>${esc(f.key)}</strong><span class="cell-sub">${f.page != null ? `Page ${esc(f.page)}` : "Page unknown"}</span></td>
      <td data-label="Extracted">${esc(f.aiValue)}${f.source === "deterministic-parser" ? `<span class="cell-sub"><span class="prov heuristic" title="Found by the deterministic invoice parser, not by OCR or AI">Parser</span> ${esc(INV_STATUS[f.parseStatus] ? INV_STATUS[f.parseStatus][0] : "")}</span>` : ""}
        ${f.evidence ? `<q class="quote">${esc(f.evidence)}</q>` : ""}</td>
      <td data-label="Confidence">${f.confidence != null ? `<div class="bar ${f.confidence < thr ? "low" : ""}" aria-hidden="true"><span style="width:${Math.max(0, Math.min(100, f.confidence))}%"></span></div>
        <span class="small num">${Number(f.confidence).toFixed(1)}%</span>` : `<span class="muted small">${f.source === "deterministic-parser" ? "No score (text rule)" : "None"}</span>`}</td>
      <td data-label="Status">${fieldPill(f.status)}</td>
      <td data-label="Reviewed">${f.status === "CORRECTED" ? `<strong>${esc(f.correctedValue)}</strong>` : f.verifiedBy ? "Confirmed as extracted" : `<span class="muted">–</span>`}
        ${f.verifiedBy ? `<span class="cell-sub">by ${esc(f.verifiedBy)}, ${fmtTime(f.verifiedAt)}</span>` : ""}</td>
      <td data-label="Actions"><div class="inline-form">
        <button class="btn sm" type="button" data-act="confirm">${icon("check")}Confirm</button>
        <label class="sr-only" for="c-${esc(docId)}-${esc(f.fieldId)}">Corrected value for ${esc(f.key)}</label>
        <input id="c-${esc(docId)}-${esc(f.fieldId)}" type="text" placeholder="Correct value">
        <button class="btn sm" type="button" data-act="correct">Save correction</button></div></td></tr>`).join("");
  }
  function bindFieldActions(el, after) {
    $$("tr[data-f]", el).forEach((tr) => $$("button[data-act]", tr).forEach((b) => (b.onclick = async () => {
      const body = { action: b.dataset.act };
      if (body.action === "correct") {
        const input = $("input", tr);
        body.correctedValue = input.value.trim();
        if (!body.correctedValue) { toast("Type the corrected value first."); input.focus(); return; }
      }
      busy(b, true, "Saving…");
      try {
        await api("POST", `/documents/${tr.dataset.doc}/fields/${tr.dataset.f}/verify`, body);
        toast(body.action === "confirm" ? `Confirmed "${tr.dataset.key}".` : `Correction saved for "${tr.dataset.key}".`);
        S.docs = null; if (S.doc && S.doc.docId === tr.dataset.doc) S.doc = null;
        await after();
      } catch (err) { busy(b, false); toast(err.message, "bad"); }
    })));
  }
  const FIELD_HEAD = `<thead><tr><th>Field</th><th>Extracted value</th><th>Confidence</th><th>Status</th><th>Reviewed value</th><th>Actions</th></tr></thead>`;

  async function tabFields(d, el, thr) {
    el.innerHTML = loading("Loading fields…");
    const r = await api("GET", `/documents/${d.docId}/fields${thr ? `?threshold=${encodeURIComponent(thr)}` : ""}`);
    const s = r.summary;
    const fst = stageOf(d, "fields");
    if (!r.fields.length) {
      el.innerHTML = `<section class="panel">${emptyState("No fields were extracted",
        fst ? esc(fst.label) + " The text itself is available in the Extracted text tab and through Ask and Search." : "This document has no form fields (key-value pairs) to review.")}</section>`;
      return;
    }
    const conf = stageOf(d, "confidence");
    el.innerHTML = `<section class="panel flush">
      <div class="panel-head"><div><h2>Fields ${prov(d, "fields")}</h2>
        <p>${conf ? esc(conf.label) : "Confidence is the lower of Textract's key and value scores. Corrections are stored next to the extracted value, which is never overwritten."}</p></div>
        <form class="inline-form" id="thrForm"><label class="field">Review threshold (%)<input id="thr" type="number" min="0" max="100" step="0.5" value="${esc(s.threshold)}" style="width:100px"></label>
          <button class="btn sm" type="submit" style="align-self:flex-end">Apply</button></form></div>
      <div style="padding:0 20px 14px" class="actions"><span class="pill ${s.pendingReview ? "warn" : "ok"}">${s.pendingReview} awaiting review</span>
        <span class="pill plain">${s.lowConfidence} below threshold</span><span class="pill plain">${s.verified} confirmed</span><span class="pill plain">${s.corrected} corrected</span></div>
      <div class="table-wrap"><table class="stack-sm">${FIELD_HEAD}<tbody>${fieldRows(r.fields, s.threshold, d.docId)}</tbody></table></div></section>
      <section class="panel"><h3 style="margin-bottom:8px">Review history</h3><div id="trail">${loading("Loading history…")}</div></section>`;
    $("#thrForm").onsubmit = (e) => { e.preventDefault(); tabFields(d, el, $("#thr").value); };
    bindFieldActions(el, async () => { S.doc = await api("GET", `/documents/${d.docId}`); tabFields(S.doc, el, $("#thr") && $("#thr").value); });
    const t = await api("GET", `/documents/${d.docId}/verifications`);
    $("#trail").innerHTML = t.auditTrail.length ? `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>When</th><th>Field</th><th>Extracted</th><th>Corrected</th><th>Decision</th><th>By</th></tr></thead><tbody>
      ${t.auditTrail.map((x) => `<tr><td class="primary-cell small">${fmtTime(x.verifiedAt)}</td><td data-label="Field">${esc(x.fieldKey)}</td><td data-label="Extracted">${esc(x.originalValue)}</td>
        <td data-label="Corrected">${esc(x.correctedValue ?? "–")}</td><td data-label="Decision">${fieldPill(x.status)}</td><td data-label="By">${esc(x.verifiedBy)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted small">No review decisions yet.</p>`;
  }

  async function tabText(d, el) {
    el.innerHTML = loading("Loading extracted text…");
    const r = await api("GET", `/local/documents/${d.docId}/text`);
    el.innerHTML = `<section class="panel"><div class="panel-head"><div><h2>Extracted text ${prov(d, "textExtraction")}</h2>
      <p>${r.extraction ? esc(r.extraction.label) : ""} ${plural(r.charCount, "character")} across ${plural(r.pages.length, "page")}.</p></div></div>
      ${r.pages.map((p) => `<div class="pagebox"><h4>Page ${esc(p.page ?? "?")}</h4>${p.lines.length ? `<ol>${p.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ol>` : `<p class="muted small" style="padding:10px 12px;margin:0">No text on this page.</p>`}</div>`).join("")}</section>`;
  }

  async function tabAsk(d, el) {
    el.innerHTML = loading("Loading conversations…");
    const s = await api("GET", `/documents/${d.docId}/chat`);
    const qa = stageOf(d, "qa");
    let sessionId = null;
    el.innerHTML = `<section class="panel">
      <div class="panel-head"><div><h2>Ask about this document ${prov(d, "qa")}</h2>
        <p>${qa ? esc(qa.label) : "Answers use only passages retrieved from this document, cite their sources, and say so when the document does not contain the answer."}</p></div>
        <div class="inline-form"><label class="field">Conversation<select id="sess"><option value="">New conversation</option>
          ${s.sessions.map((x) => `<option value="${esc(x.sessionId)}">${esc(x.title)} (${esc(x.messageCount)})</option>`).join("")}</select></label>
          <button class="btn sm danger" type="button" id="delSess" style="align-self:flex-end" disabled>Delete conversation</button></div></div>
      <div class="chat" id="chat" aria-live="polite"><p class="muted small" id="chatHint">Try: “What is the total amount due?”</p></div>
      <form class="composer" id="askForm"><label class="sr-only" for="question">Your question</label>
        <input id="question" type="text" maxlength="1000" placeholder="Ask a question about this document" autocomplete="off">
        <button class="btn primary" type="submit" id="askBtn">Ask</button></form></section>`;
    const chat = $("#chat");
    const render = (m) => {
      const hint = $("#chatHint"); if (hint) hint.remove();
      const div = document.createElement("div");
      div.className = `msg ${m.role}`;
      let html = esc(m.text);
      if (m.role === "assistant") {
        const mode = ANSWER_MODES[m.answerMode];
        html += `<div class="small" style="margin-top:6px">${mode ? `<span class="prov ${mode[1]}" title="${esc(mode[2])}">${mode[0]}</span> ` : ""}Confidence: <span class="pill plain">${esc(m.confidence || "–")}</span></div>`;
        for (const c of m.citations || []) html += `<div class="cite">${esc(c.fileName || "")}, page ${esc(c.page ?? "unknown")}, relevance ${Number(c.relevance || 0).toFixed(2)}${c.ocrConfidence != null ? `, OCR ${esc(c.ocrConfidence)}%` : ""}<br>“${esc(c.snippet)}”</div>`;
        for (const w of m.warnings || []) html += `<div class="small" style="color:var(--heuristic);margin-top:4px">${esc(w)}</div>`;
      }
      div.innerHTML = html;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
    };
    $("#sess").onchange = async () => {
      sessionId = $("#sess").value || null;
      $("#delSess").disabled = !sessionId;
      chat.innerHTML = "";
      if (sessionId) {
        try { (await api("GET", `/documents/${d.docId}/chat/${sessionId}`)).messages.forEach(render); } catch (err) { toast(err.message, "bad"); }
      }
    };
    $("#delSess").onclick = async () => {
      if (!sessionId || !confirm("Delete this conversation? This cannot be undone.")) return;
      try { await api("DELETE", `/documents/${d.docId}/chat/${sessionId}`); toast("Conversation deleted."); tabAsk(d, el); } catch (err) { toast(err.message, "bad"); }
    };
    $("#askForm").onsubmit = async (e) => {
      e.preventDefault();
      const q = $("#question").value.trim();
      if (!q) return;
      $("#question").value = "";
      render({ role: "user", text: q });
      const btn = $("#askBtn");
      busy(btn, true, "Asking…");
      try {
        const r = await api("POST", `/documents/${d.docId}/chat`, { question: q, ...(sessionId ? { sessionId } : {}) });
        sessionId = r.sessionId;
        $("#delSess").disabled = false;
        render({ role: "assistant", text: r.answer, citations: r.citations, confidence: r.confidence, warnings: r.warnings, answerMode: r.answerMode });
      } catch (err) { toast(err.message, "bad"); }
      finally { busy(btn, false); $("#question").focus(); }
    };
  }

  const PII_TYPES_AWS = ["NAME", "EMAIL", "PHONE", "ADDRESS", "SSN", "BANK_ACCOUNT_NUMBER", "CREDIT_DEBIT_NUMBER", "PASSPORT_NUMBER", "DRIVER_ID", "IN_AADHAAR", "IN_PERMANENT_ACCOUNT_NUMBER", "DATE_TIME"];
  const PII_TYPES_LOCAL = ["EMAIL", "PHONE", "ADDRESS", "SSN", "BANK_ACCOUNT_NUMBER", "CREDIT_DEBIT_NUMBER", "IN_PERMANENT_ACCOUNT_NUMBER"];
  async function tabPrivacy(d, el) {
    const pst = stageOf(d, "pii");
    const types = isLocal() ? PII_TYPES_LOCAL : PII_TYPES_AWS;
    el.innerHTML = `<section class="panel">
      <div class="panel-head"><div><h2>Personal information ${prov(d, "pii")}</h2>
        <p>${pst ? esc(pst.label) : "Detection uses Amazon Comprehend."} Detection shows masked previews only. Redaction creates a new redacted copy; the original is never changed.</p></div></div>
      <fieldset style="border:0;padding:0;margin:0 0 12px"><legend class="small muted" style="margin-bottom:6px">Types to include (none selected means all)</legend>
        <div class="actions">${types.map((t) => `<label class="check"><input type="checkbox" class="pt" value="${t}">${t.replace(/_/g, " ").toLowerCase()}</label>`).join("")}</div></fieldset>
      <div class="toolbar">${isLocal() ? "" : `<label class="field">Minimum score<input id="minScore" type="number" step="0.05" min="0" max="1" value="0.5" style="width:110px"></label>`}
        <label class="field">Redaction style<select id="mask"><option value="TYPE">Replace with [TYPE]</option><option value="CHAR">Replace with *****</option></select></label>
        <button class="btn" type="button" id="detectBtn">${icon("search")}Detect</button>
        <button class="btn primary" type="button" id="redactBtn">${icon("shield")}Create redacted copy</button></div>
      <div id="piiOut"></div></section>
      <section class="panel"><h3 style="margin-bottom:8px">Redacted copies</h3><div id="redList">${loading("Loading…")}</div></section>`;
    const opts = () => {
      const sel = $$(".pt:checked", el).map((c) => c.value);
      return { minScore: $("#minScore") ? Number($("#minScore").value) : 0.5, ...(sel.length ? { entityTypes: sel } : {}) };
    };
    $("#detectBtn").onclick = async () => {
      const b = $("#detectBtn"); busy(b, true, "Detecting…");
      try {
        const r = await api("POST", `/documents/${d.docId}/pii`, opts());
        $("#piiOut").innerHTML = `<p>${r.total ? `Found ${plural(r.total, "item")}: ${Object.entries(r.counts).map(([k, v]) => `<span class="chip">${esc(k)}<small>${v}</small></span>`).join("")}` : "No personal information of the selected types was found."}</p>
          ${r.warning ? `<div class="notice warn">${icon("alert")}<p>${esc(r.warning)}</p></div>` : ""}
          ${r.entities.length ? `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>Type</th><th>Masked preview</th><th>Where</th><th>${isLocal() ? "Match" : "Score"}</th></tr></thead><tbody>
          ${r.entities.map((e) => `<tr><td class="primary-cell">${esc(e.type)}</td><td data-label="Preview"><code>${esc(e.preview)}</code></td>
            <td data-label="Where">${esc(e.location)}${e.page != null ? `, page ${esc(e.page)}` : ""}</td><td data-label="${isLocal() ? "Match" : "Score"}">${isLocal() ? "Pattern match" : Number(e.score).toFixed(2)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
      } catch (err) { toast(err.message, "bad"); }
      finally { busy(b, false); }
    };
    const loadList = async () => {
      try {
        const r = await api("GET", `/documents/${d.docId}/redactions`);
        $("#redList").innerHTML = r.redactions.length ? `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>Created</th><th>Redacted</th><th>Types</th><th>By</th><th>Download</th></tr></thead><tbody>
          ${r.redactions.map((x) => `<tr><td class="primary-cell small">${fmtTime(x.createdAt)}</td><td data-label="Redacted" class="num">${esc(x.totalRedacted)}</td>
            <td data-label="Types">${esc((x.entityTypes || []).join(", "))}</td><td data-label="By">${esc(x.requestedBy)}</td>
            <td data-label="Download"><a href="${esc(x.downloads.text)}" download>Text</a>, <a href="${esc(x.downloads.json)}" download>JSON</a></td></tr>`).join("")}</tbody></table></div>
          <p class="small muted" style="margin-top:8px">Download links are valid for 5 minutes; reopen this tab for fresh links.</p>`
          : `<p class="muted small">No redacted copies yet.</p>`;
      } catch (err) { $("#redList").innerHTML = errorPanel(err); bindRetry($("#redList"), loadList); }
    };
    $("#redactBtn").onclick = async () => {
      const b = $("#redactBtn"); busy(b, true, "Redacting…");
      try {
        const r = await api("POST", `/documents/${d.docId}/redact`, { ...opts(), mask: $("#mask").value });
        toast(`Redacted copy created: ${plural(r.totalRedacted, "item")} redacted.`);
        loadList();
      } catch (err) { toast(err.message, "bad"); }
      finally { busy(b, false); }
    };
    loadList();
  }

  function tabExport(d, el) {
    const formats = [["pdf", "PDF report", "A readable report: summary, dates, fields with confidence and review decisions."],
      ["json", "JSON", "Everything, machine-readable, including original values next to corrections."],
      ["csv", "CSV", "One row per item; opens in Excel."]];
    el.innerHTML = `<section class="panel"><div class="panel-head"><div><h2>Export results</h2>
      <p>${d.provenance ? "Exports state that this document was processed in local demo mode." : "Exports include the original extracted values and any human corrections."}</p></div></div>
      <label class="check" style="margin-bottom:14px"><input type="checkbox" id="incText" checked>Include the extracted text</label>
      <div class="stack">${formats.map(([k, label, desc]) => `<div class="inline-form" style="justify-content:space-between;border-top:1px solid var(--line);padding-top:12px">
        <div><strong>${label}</strong><span class="cell-sub">${desc}</span></div>
        <button class="btn" type="button" data-fmt="${k}">${icon("download")}Create ${label}</button></div>`).join("")}</div>
      <div id="expOut" style="margin-top:16px" aria-live="polite"></div></section>`;
    $$("[data-fmt]", el).forEach((b) => (b.onclick = async () => {
      busy(b, true, "Creating…");
      try {
        const r = await api("GET", `/documents/${d.docId}/export?format=${b.dataset.fmt}&includeText=${$("#incText").checked}`);
        $("#expOut").innerHTML = `<div class="notice ok">${icon("check")}<div><p><strong>${esc(r.format.toUpperCase())} ready (${fmtBytes(r.bytes)}).</strong>
          <a href="${esc(r.downloadUrl)}" download>Download file</a></p><p class="small">The link is valid for 5 minutes.</p></div></div>`;
      } catch (err) { toast(err.message, "bad"); }
      finally { busy(b, false); }
    }));
  }

  async function tabAudit(d, el) {
    el.innerHTML = loading("Loading audit trail…");
    const r = await api("GET", `/documents/${d.docId}/audit`);
    el.innerHTML = `<section class="panel flush"><div class="panel-head"><div><h2>Audit trail</h2><p>Events never contain document content, questions or answers.</p></div></div>
      ${auditTable(r.events, false)}</section>`;
  }
  function auditTable(events, withDoc) {
    if (!events.length) return emptyState("No events", "Nothing was recorded for this selection.");
    return `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>When</th><th>Event</th>${withDoc ? "<th>Document</th>" : ""}<th>By</th><th>Details</th></tr></thead><tbody>
      ${events.map((e) => `<tr><td class="primary-cell small">${fmtTime(e.timestamp)}</td><td data-label="Event">${esc(e.eventType.replace(/_/g, " ").toLowerCase())}</td>
        ${withDoc ? `<td data-label="Document">${e.docId ? `<a href="#/documents/${esc(e.docId)}"><code>${esc(e.docId.slice(0, 8))}</code></a>` : "–"}</td>` : ""}
        <td data-label="By">${esc(e.actor)}</td><td data-label="Details"><code class="small wrap-any">${esc(JSON.stringify(e.details || {}))}</code></td></tr>`).join("")}</tbody></table></div>`;
  }

  // ------------------------------------------------------------------ review queue
  async function viewReview() {
    crumbs([["Review queue"]]);
    setView(loading("Loading review queue…"));
    const docs = await loadDocs(true);
    const pending = docs.filter((d) => d.status === "COMPLETED" && d.reviewStatus === "PENDING_REVIEW");
    const groups = await Promise.all(pending.map((d) => api("GET", `/documents/${d.docId}/fields?status=REQUIRES_REVIEW`).then((r) => [d, r])));
    setView(`<div class="page-head"><div><h1>Review queue</h1>
      <p>Fields whose confidence is below the review threshold. Confirm the extracted value or save a correction; the original value is kept for the audit trail.</p></div></div>
      ${isLocal() ? `<div class="notice">${icon("info")}<p>Uploaded invoices appear here with the values found by the deterministic parser. They have no confidence score, so each one needs a reviewer. The demo fixture shows scripted Textract-style confidence scores instead.</p></div>` : ""}
      ${groups.length ? groups.map(([d, r]) => `<section class="panel flush" style="margin-bottom:18px"><div class="panel-head"><div>
        <h2><a class="doc-name" href="#/documents/${esc(d.docId)}/fields">${esc(d.fileName)}</a></h2>
        <p>${plural(r.fields.length, "field")} awaiting review, threshold ${esc(r.summary.threshold)}% ${sourcePill(d.docId)}</p></div></div>
        <div class="table-wrap"><table class="stack-sm">${FIELD_HEAD}<tbody>${fieldRows(r.fields, r.summary.threshold, d.docId)}</tbody></table></div></section>`).join("")
        : `<section class="panel">${emptyState("Nothing awaiting review", "Every extracted field is either above the confidence threshold or has been reviewed.", `<a class="btn" href="#/documents">View documents</a>`)}</section>`}`);
    bindFieldActions(view(), viewReview);
  }

  // ------------------------------------------------------------------ search
  const searchState = { q: "", category: "", status: "", dateFrom: "", dateTo: "" };
  async function viewSearch() {
    crumbs([["Search"]]);
    const docs = await loadDocs().catch(() => []);
    const cats = [...new Set(docs.map((d) => d.category).filter(Boolean))].sort();
    setView(`<div class="page-head"><div><h1>Search</h1><p>Search the text of every processed document. ${isLocal() ? "Ranking is lexical (BM25): it matches words, not meaning." : ""}</p></div></div>
      <form class="panel toolbar" id="searchForm" role="search" style="margin-bottom:18px">
        <label class="field grow">Words to find<input type="search" id="q" value="${esc(searchState.q)}" placeholder="e.g. late fee"></label>
        <label class="field">Category<select id="sCat"><option value="">Any</option>${cats.map((c) => `<option>${esc(c)}</option>`).join("")}</select></label>
        <label class="field">Status<select id="sStatus"><option value="">Any</option>${Object.entries(STATUS).map(([k, [l]]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
        <label class="field">Uploaded from<input type="date" id="sFrom" value="${esc(searchState.dateFrom)}"></label>
        <label class="field">to<input type="date" id="sTo" value="${esc(searchState.dateTo)}"></label>
        <button class="btn primary" type="submit">${icon("search")}Search</button></form>
      <div id="searchOut" aria-live="polite">${emptyState("Search your documents", "Enter words to search for, or choose filters and search to browse.")}</div>`);
    $("#sCat").value = searchState.category; $("#sStatus").value = searchState.status;
    $("#searchForm").onsubmit = async (e) => {
      e.preventDefault();
      Object.assign(searchState, { q: $("#q").value.trim(), category: $("#sCat").value, status: $("#sStatus").value, dateFrom: $("#sFrom").value, dateTo: $("#sTo").value });
      await runSearch();
    };
    if (searchState.q || searchState.category || searchState.status) runSearch();
  }
  async function runSearch() {
    const out = $("#searchOut");
    out.innerHTML = loading("Searching…");
    const p = new URLSearchParams();
    for (const k of ["q", "category", "status", "dateFrom", "dateTo"]) if (searchState[k]) p.set(k, searchState[k]);
    try {
      const r = await api("GET", `/search?${p}`);
      out.innerHTML = `<p class="small muted">${plural(r.total, "result")}${r.searchMode === "browse" ? " (filters only)" : `, ${esc(r.searchMode)} ranking`}${r.indexTruncated ? ", index truncated" : ""}.</p>
        ${r.results.length ? r.results.map((x) => `<article class="panel" style="margin-bottom:12px"><div class="panel-head" style="margin-bottom:6px">
          <h3><a class="doc-name" href="#/documents/${esc(x.docId)}">${esc(x.fileName)}</a></h3>
          <span class="actions small muted">${x.category ? esc(x.category) : ""} ${statusPill(x.status)} ${x.score != null ? `<span class="num">score ${Number(x.score).toFixed(2)}</span>` : ""}</span></div>
          ${(x.snippets || []).map((s) => `<p class="small" style="margin:6px 0 0">${s.page ? `<span class="chip">Page ${esc(s.page)}</span>` : ""}${s.kind !== "text" ? `<span class="chip">${esc(s.kind)}</span>` : ""}${highlight(s.text, x.matchedTerms)}</p>`).join("")}</article>`).join("")
          : emptyState("No matches", "Try fewer or different words, or remove filters.")}`;
    } catch (err) { out.innerHTML = errorPanel(err); bindRetry(out, runSearch); }
  }

  // ------------------------------------------------------------------ compare
  async function viewCompare(params) {
    crumbs([["Compare"]]);
    setView(loading("Loading documents…"));
    const docs = (await loadDocs(true)).filter((d) => d.status === "COMPLETED");
    const opts = docs.map((d) => `<option value="${esc(d.docId)}">${esc(d.fileName)}</option>`).join("");
    setView(`<div class="page-head"><div><h1>Compare documents</h1><p>Line-by-line and field-by-field differences between two processed documents.</p></div></div>
      ${docs.length < 2 ? `<section class="panel">${emptyState("Two processed documents are needed", "Upload another document to compare it with an existing one.", `<button class="btn primary" type="button" data-upload>${icon("upload")}Upload document</button>`)}</section>` : `
      <form class="panel toolbar" id="cmpForm" style="margin-bottom:18px">
        <label class="field grow">First document (A)<select id="cmpA">${opts}</select></label>
        <label class="field grow">Second document (B)<select id="cmpB">${opts}</select></label>
        <label class="check" style="align-self:center"><input type="checkbox" id="cmpForce">Recompute</label>
        <button class="btn primary" type="submit">${icon("compare")}Compare</button></form>
      <div id="cmpOut" aria-live="polite"></div>`}`);
    $$("[data-upload]").forEach((b) => (b.onclick = openUpload));
    if (docs.length < 2) return;
    $("#cmpB").selectedIndex = 1;
    const pa = params && params.get("a"), pb = params && params.get("b");
    if (pa && pb && docs.some((d) => d.docId === pa) && docs.some((d) => d.docId === pb)) {
      $("#cmpA").value = pa; $("#cmpB").value = pb;
      setTimeout(() => $("#cmpForm").requestSubmit(), 0);
    }
    $("#cmpForm").onsubmit = async (e) => {
      e.preventDefault();
      const a = $("#cmpA").value, b = $("#cmpB").value;
      if (a === b) { toast("Choose two different documents."); return; }
      const out = $("#cmpOut");
      out.innerHTML = loading("Comparing…");
      try {
        const r = await api("POST", "/compare", { docIdA: a, docIdB: b, force: $("#cmpForce").checked });
        out.innerHTML = renderComparison(r);
      } catch (err) { out.innerHTML = errorPanel(err); bindRetry(out, () => $("#cmpForm").requestSubmit()); }
    };
  }
  function renderComparison(r) {
    const t = r.factualDiff.text, f = r.factualDiff.fields, ai = r.aiInterpretation;
    const lines = (items, cls, sign) => items.map((x) => `<div class="${cls} small">${sign} ${esc(x)}</div>`).join("");
    const verified = (x) => (x.verified ? `<span class="pill ok">Quote found</span>` : `<span class="pill warn">Quote not found</span>`);
    const aiList = (title, items, fmt) => (items && items.length ? `<div class="section"><h3>${title}</h3>${items.map((x) => `<div style="margin:6px 0">${verified(x)} ${esc(x.point)}${fmt(x)}</div>`).join("")}</div>` : "");
    const localUnavailable = ai.status !== "OK" && ai.error === "LocalAIUnavailable";
    return `<div class="grid-2">
      <section class="panel"><div class="panel-head"><div><h2>Factual differences <span class="prov genuine" title="Computed with difflib from the extracted text">Computed</span></h2>
        <p>${esc(r.fileNameA)} (A) compared with ${esc(r.fileNameB)} (B). ${(t.lineSimilarity * 100).toFixed(0)}% line similarity${r.cached ? ", cached result" : ""}.</p></div></div>
        <div class="section"><h3>Changed fields</h3>${f.changed.map((c) => `<div class="small"><strong>${esc(c.key)}</strong>: <span class="diff-del">${esc(c.before)}</span> to <span class="diff-add">${esc(c.after)}</span></div>`).join("") || `<p class="muted small">None</p>`}</div>
        ${f.added.length ? `<div class="section"><h3>Fields only in B</h3>${f.added.map((x) => `<div class="diff-add small">+ ${esc(x.key)}: ${esc(x.value)}</div>`).join("")}</div>` : ""}
        ${f.removed.length ? `<div class="section"><h3>Fields only in A</h3>${f.removed.map((x) => `<div class="diff-del small">− ${esc(x.key)}: ${esc(x.value)}</div>`).join("")}</div>` : ""}
        <div class="section"><h3>Changed lines (${t.changedCount})</h3>${t.changed.map((c) => `<div class="small" style="margin-bottom:6px"><span class="diff-del">− ${esc(c.before)}</span><br><span class="diff-add">+ ${esc(c.after)}</span></div>`).join("") || `<p class="muted small">None</p>`}</div>
        <div class="section"><h3>Only in B (${t.addedCount})</h3>${lines(t.added, "diff-add", "+") || `<p class="muted small">None</p>`}</div>
        <div class="section"><h3>Only in A (${t.removedCount})</h3>${lines(t.removed, "diff-del", "−") || `<p class="muted small">None</p>`}</div></section>
      <section class="panel"><div class="panel-head"><div><h2>AI interpretation ${localUnavailable ? `<span class="prov unavailable">Unavailable</span>` : `<span class="prov ai">AI-generated</span>`}</h2></div></div>
        ${localUnavailable ? `<p class="muted">An AI comparison needs a language model (Amazon Bedrock), which the local demo does not include. The factual differences are complete without it.</p>`
          : ai.status !== "OK" ? `<div class="notice warn">${icon("alert")}<p>The AI comparison failed (${esc(ai.status)}). The factual differences are still valid.</p></div>`
          : `<p class="small muted">${esc(ai.disclaimer)}</p><p>${esc(ai.summary || "")}</p>
            ${aiList("Added in B", ai.added, (x) => (x.evidenceB ? `<q class="quote">${esc(x.evidenceB)}</q>` : ""))}
            ${aiList("Removed from A", ai.removed, (x) => (x.evidenceA ? `<q class="quote">${esc(x.evidenceA)}</q>` : ""))}
            ${aiList("Changed", ai.changed, (x) => `<div class="small"><span class="diff-del">${esc(x.before)}</span> to <span class="diff-add">${esc(x.after)}</span></div>`)}
            ${aiList("Contradictions", ai.contradictions, (x) => `<q class="quote">A: ${esc(x.docA)}</q><q class="quote">B: ${esc(x.docB)}</q>`)}
            ${aiList("Similarities", ai.similarities, () => "")}`}</section></div>`;
  }

  // ------------------------------------------------------------------ audit log
  const AUDIT_EVENTS = ["UPLOAD_URL_ISSUED", "DOCUMENT_UPLOADED", "DOCUMENT_PROCESSED", "DOCUMENT_FAILED", "DOCUMENT_QUERIED", "DOCUMENT_COMPARED",
    "DOCUMENTS_SEARCHED", "PII_DETECTED", "PII_REDACTED", "RESULT_MODIFIED", "FIELD_VERIFIED", "EXPORT_GENERATED", "CHAT_SESSION_DELETED"];
  async function viewAudit() {
    crumbs([["Audit log"]]);
    setView(`<div class="page-head"><div><h1>Audit log</h1><p>Everything that happened on one day (UTC). Events record identifiers and counts, never document content.</p></div></div>
      <form class="panel toolbar" id="auditForm" style="margin-bottom:18px">
        <label class="field">Day<input type="date" id="aDay" value="${todayUTC()}" required></label>
        <label class="field">Event<select id="aType"><option value="">All events</option>${AUDIT_EVENTS.map((e) => `<option value="${e}">${e.replace(/_/g, " ").toLowerCase()}</option>`).join("")}</select></label>
        <button class="btn primary" type="submit">Show events</button></form>
      <section class="panel flush" id="auditOut" aria-live="polite"></section>`);
    const load = async () => {
      const out = $("#auditOut");
      out.innerHTML = loading("Loading events…");
      try {
        const p = new URLSearchParams({ date: $("#aDay").value || todayUTC() });
        if ($("#aType").value) p.set("eventType", $("#aType").value);
        const r = await api("GET", `/audit?${p}`);
        out.innerHTML = auditTable(r.events, true);
      } catch (err) { out.innerHTML = errorPanel(err); bindRetry(out, load); }
    };
    $("#auditForm").onsubmit = (e) => { e.preventDefault(); load(); };
    load();
  }

  // ------------------------------------------------------------------ settings
  function viewSettings() {
    crumbs([["Settings"]]);
    const userField = `<label class="field">Your name (recorded in the audit trail and used to keep your conversations separate)
      <input type="text" id="userId" value="${esc(S.user)}" maxlength="80" autocomplete="name"></label>`;
    if (isLocal()) {
      const L = S.local;
      const pdf = L.pdfExtraction || {};
      setView(`<div class="page-head"><div><h1>Settings</h1><p>This console is connected to the local demo server on this computer.</p></div></div>
        <div class="grid-2"><div class="stack">
          <section class="panel"><h2 style="margin-bottom:12px">Local server</h2>
            <dl class="facts"><dt>Mode</dt><dd>Local demo, no AWS calls</dd>
              <dt>API address</dt><dd>${esc(location.origin)} (same origin, no API key needed)</dd>
              <dt>PDF text extraction</dt><dd>${pdf.available ? esc(pdf.engine) : `<span class="pill bad">PyMuPDF not installed</span> Run <code>pip install pymupdf==1.28.2</code> and restart the server.`}</dd>
              <dt>Upload limit</dt><dd>${fmtBytes(L.maxUploadBytes)}, ${esc(L.acceptedExtensions.join(", "))} only</dd>
              <dt>Storage</dt><dd>${esc(L.storage)}</dd>
              <dt>Review threshold</dt><dd>${esc(L.confidenceThreshold)}%</dd></dl></section>
          <form class="panel stack" id="userForm"><h2>You</h2>${userField}<div><button class="btn primary" type="submit">Save</button></div></form>
        </div>
        <section class="panel"><h2 style="margin-bottom:4px">What runs locally</h2><p class="small muted">${esc(L.notice)}</p>
          <div class="legend" style="margin:10px 0 6px">${Object.entries(PROV_NAMES).map(([k, v]) => `<span class="prov ${k}">${v}</span>`).join("")}</div>
          <ul class="prov-ledger">${Object.entries(L.capabilities).map(([k, st]) => `<li><span class="tick ${esc(st.status)}" aria-hidden="true"></span><div><b>${esc(STAGE_NAMES[k] || k)}</b><span>${PROV_NAMES[st.status]}: ${esc(st.label)}</span></div></li>`).join("")}</ul>
        </section></div>`);
      $("#userForm").onsubmit = (e) => { e.preventDefault(); saveUser(); toast("Settings saved."); };
      return;
    }
    setView(`<div class="page-head"><div><h1>Settings</h1><p>Connect this console to your deployed DocIntel API. Values are stored only in this browser.</p></div></div>
      <form class="panel stack" id="cfgForm" style="max-width:640px">
        <label class="field">API URL<input type="text" id="apiUrl" value="${esc(S.base)}" placeholder="https://xxxx.execute-api.us-east-1.amazonaws.com/prod" required></label>
        <label class="field">API key<input type="password" id="apiKey" value="${esc(S.key)}" autocomplete="off"></label>
        ${userField}
        <div class="actions"><button class="btn primary" type="submit">Save and connect</button></div></form>`);
    $("#cfgForm").onsubmit = (e) => {
      e.preventDefault();
      S.base = $("#apiUrl").value.trim().replace(/\/+$/, "");
      S.key = $("#apiKey").value.trim();
      store.set("di.url", S.base); store.set("di.key", S.key);
      saveUser(); invalidate(); renderModeBox();
      toast("Settings saved.");
      location.hash = "#/dashboard";
    };
  }
  function saveUser() { S.user = ($("#userId").value.trim() || "web-user").replace(/[^A-Za-z0-9@._-]/g, "_"); store.set("di.user", S.user); }

  // ------------------------------------------------------------------ processing stages (Feature D)
  const STAGE_LABELS = {
    upload: "Upload received", validation: "File validation", textExtraction: "Text extraction",
    fieldExtraction: "Field extraction", analysis: "Document analysis", indexing: "Search indexing", completion: "Completion",
  };
  const STAGE_STATUS = { completed: ["Completed", "ok"], failed: ["Failed", "bad"], not_run: ["Not run", "plain"],
    skipped: ["Skipped", "plain"], pending: ["Pending", "warn"] };
  function stagesPanel(d) {
    const diag = d.diagnostics || {};
    const st = diag.stages || [];
    if (!st.length) {
      return `<section class="panel"><h3 style="margin-bottom:6px">Processing stages</h3><p class="small muted">${d.status === "PROCESSING"
        ? "Processing is in progress; stage details are recorded when it finishes." : "No stage diagnostics were recorded for this document (processed before diagnostics existed)."}</p></section>`;
    }
    const failed = st.find((x) => x.status === "failed");
    const canRetry = isLocal() && d.status === "FAILED" && failed && failed.retryable && !isFixture(d.docId);
    return `<section class="panel" aria-labelledby="stH"><div class="panel-head" style="margin-bottom:8px"><h3 id="stH">Processing stages</h3>
        ${diag.attempt > 1 ? `<span class="pill plain">Attempt ${esc(diag.attempt)}</span>` : ""}</div>
      <ol class="stages">${st.map((x) => { const [l, c] = STAGE_STATUS[x.status] || [x.status, "plain"]; return `<li class="stage ${esc(x.status)}">
        <div class="stage-row"><b>${esc(STAGE_LABELS[x.stage] || x.stage)}</b><span class="pill ${c}">${esc(l)}</span>
          <span class="small muted num">${x.durationMs != null ? `${esc(x.durationMs)} ms` : ""}</span></div>
        ${x.detail ? `<div class="small muted">${esc(x.detail)}</div>` : ""}
        ${x.error ? `<div class="small" style="color:var(--danger)">${esc(x.error)}</div>` : ""}</li>`; }).join("")}</ol>
      <p class="small muted" style="margin:8px 0 0">Durations are measured on this run; the upload stage is not timed.</p>
      ${failed && d.status === "FAILED" ? (canRetry ? `<div class="actions" style="margin-top:10px"><button class="btn primary" type="button" id="retryBtn">${icon("refresh")}Retry processing</button>
          <span class="small muted">Re-runs the same stored file; no new record is created.</span></div>`
        : `<p class="small muted" style="margin-top:8px">${failed.retryable === false ? "Retrying would give the same result because the failure depends on the file itself." : "Retry is available in the local demo only; in AWS the queue retries automatically."}</p>`) : ""}
    </section>`;
  }
  function bindRetryStage(el, d) {
    const b = $("#retryBtn", el);
    if (!b) return;
    b.onclick = async () => {
      busy(b, true, "Retrying…");
      try {
        const r = await api("POST", `/local/documents/${d.docId}/retry`);
        toast(r.message || "Processing finished.");
      } catch (err) { toast(err.message, "bad"); }
      S.doc = null; S.docs = null;
      route();
    };
  }

  // ------------------------------------------------------------------ invoice checks + findings (Features A, C)
  const INV_STATUS = { extracted: ["Extracted", "ok"], inferred: ["Inferred", "info"], ambiguous: ["Ambiguous", "warn"],
    invalid: ["Unreadable", "bad"], missing: ["Not found", "plain"], corrected: ["Corrected", "info"] };
  const CHECK_STATUS = { pass: ["Pass", "ok"], fail: ["Fail", "bad"], warn: ["Check", "warn"], skipped: ["Not checked", "plain"] };
  const CHECK_NAMES = { arithmetic: "Amounts add up", taxRate: "Tax matches rate", dateOrder: "Due date after invoice date", negativeAmounts: "No negative amounts" };
  const SEV = { high: ["High", "bad"], medium: ["Medium", "warn"], low: ["Low", "plain"], info: ["Info", "info"] };
  const FSTATUS = { open: ["Open", "warn"], acknowledged: ["Acknowledged", "info"], dismissed: ["Dismissed", "plain"] };
  const ANSWER_MODES = {
    template: ["Template answer", "heuristic", "Built from an extracted field and the document line containing it; no language model"],
    extractive: ["Closest line", "heuristic", "The best-matching line from the retrieved passages, quoted verbatim; no language model"],
    none: ["Not found", "unavailable", "No retrieved passage matched the question well enough"],
    llm: ["AI-generated", "ai", "Answered by a language model (Amazon Bedrock) from retrieved passages"],
  };
  const pill = (map, k) => { const [l, c] = map[k] || [k, "plain"]; return `<span class="pill ${c}">${esc(l)}</span>`; };

  function findingCard(f, withDoc) {
    const r = f.review;
    return `<article class="finding sev-${esc(f.severity)}" data-fid="${esc(f.findingId)}" data-doc="${esc(f.docId)}">
      <div class="finding-head">${pill(SEV, f.severity)} <h3>${esc(f.title)}</h3> ${pill(FSTATUS, f.status)}
        <code class="small muted" title="Rule that produced this finding">${esc(f.rule)}</code></div>
      ${withDoc ? `<p class="small" style="margin:4px 0 0"><a href="#/documents/${esc(f.docId)}/invoice">${esc(f.fileName || f.docId)}</a></p>` : ""}
      <p style="margin:8px 0">${esc(f.explanation)}</p>
      ${f.evidence && f.evidence.length ? `<div class="table-wrap"><table class="stack-sm evidence"><thead><tr><th>Field</th><th>Value</th><th>Page</th><th>Source line</th></tr></thead><tbody>
        ${f.evidence.map((e) => `<tr><td class="primary-cell">${esc(e.label)} ${e.status !== "extracted" ? pill(INV_STATUS, e.status) : ""}</td>
          <td data-label="Value">${e.value ? esc(e.value) : `<span class="muted">–</span>`}${e.original ? `<span class="cell-sub">originally ${esc(e.original.raw || "–")}</span>` : ""}</td>
          <td data-label="Page">${esc(e.page ?? "–")}</td><td data-label="Source">${e.snippet ? `<q class="quote">${esc(e.snippet)}</q>` : `<span class="muted small">Not in the text</span>`}</td></tr>`).join("")}
        </tbody></table></div>` : ""}
      ${f.relatedDocs && f.relatedDocs.length ? `<p class="small" style="margin:8px 0 0">Related: ${f.relatedDocs.map((x) => `<a href="#/documents/${esc(x.docId)}">${esc(x.fileName || x.docId)}</a>`).join(", ")}</p>` : ""}
      <p class="small" style="margin:8px 0 0"><strong>Suggested review:</strong> ${esc(f.suggestedAction)}</p>
      ${r ? `<p class="small muted" style="margin:6px 0 0">${esc(FSTATUS[r.decision][0])} by ${esc(r.decidedBy)}, ${fmtTime(r.decidedAt)}${r.note ? `: “${esc(r.note)}”` : ""}</p>` : ""}
      <div class="inline-form" style="margin-top:10px">${f.status === "open" ? `
        <label class="sr-only" for="n-${esc(f.findingId)}">Review note</label>
        <input id="n-${esc(f.findingId)}" type="text" maxlength="500" placeholder="Note (required to dismiss)" style="flex:1 1 220px">
        <button class="btn sm" type="button" data-fact="acknowledge">Acknowledge</button>
        <button class="btn sm" type="button" data-fact="dismiss">Dismiss</button>`
        : `<button class="btn sm" type="button" data-fact="reopen">Reopen</button>`}</div></article>`;
  }
  function bindFindingActions(el, after) {
    $$("article.finding", el).forEach((card) => $$("button[data-fact]", card).forEach((b) => (b.onclick = async () => {
      const action = b.dataset.fact;
      const input = $("input", card);
      const note = input ? input.value.trim() : "";
      if (action === "dismiss" && note.length < 3) { toast("Add a short note explaining why this finding is dismissed."); if (input) input.focus(); return; }
      busy(b, true, "Saving…");
      try {
        await api("POST", `/documents/${card.dataset.doc}/anomalies/${card.dataset.fid}/review`, { action, ...(note ? { note } : {}) });
        toast(action === "dismiss" ? "Finding dismissed." : action === "acknowledge" ? "Finding acknowledged." : "Finding reopened.");
        await after();
      } catch (err) { busy(b, false); toast(err.message, "bad"); }
    })));
  }

  async function tabInvoice(d, el) {
    el.innerHTML = loading("Loading invoice fields…");
    const r = await api("GET", `/documents/${d.docId}/invoice`);
    if (!r.available) { el.innerHTML = `<section class="panel">${emptyState("No invoice data", esc(r.reason))}</section>`; return; }
    const names = Object.keys(r.fields);
    const parserFields = (d.fields || []).some((f) => f.source === "deterministic-parser");
    el.innerHTML = `
      ${r.invoiceLike ? "" : `<div class="notice">${icon("info")}<p>This document does not look like an invoice (category “${esc(d.category || "unknown")}” and no invoice number), so no anomaly rules are applied. Parsed values are shown for reference only.</p></div>`}
      <section class="panel flush"><div class="panel-head"><div><h2>Invoice fields <span class="prov heuristic" title="Deterministic text rules: labels, dates and amounts. Not OCR, not AI.">Deterministic parser</span></h2>
        <p>Each value is shown as written, with its normalized form, the page and the line it came from. Nothing is filled in when a value is not found.
        ${parserFields ? `To correct a value, use <a href="#/documents/${esc(d.docId)}/fields">Fields &amp; review</a>; checks below use the corrected value.` : ""}</p></div></div>
        <div class="table-wrap"><table class="stack-sm"><thead><tr><th>Field</th><th>As written</th><th>Normalized</th><th>Status</th><th>Page</th></tr></thead><tbody>
        ${names.map((n) => { const f = r.fields[n]; return `<tr class="${f.status === "ambiguous" || f.status === "invalid" ? "needs" : ""}">
          <td class="primary-cell"><strong>${esc(f.label)}</strong></td>
          <td data-label="As written">${f.raw ? esc(f.raw) : `<span class="muted">–</span>`}${f.evidence ? `<q class="quote">${esc(f.evidence)}</q>` : ""}
            ${f.original ? `<span class="cell-sub">Extracted value: ${esc(f.original.raw || "–")}</span>` : ""}</td>
          <td data-label="Normalized" class="num">${f.value ? esc(f.value) : `<span class="muted">–</span>`}${f.currency && n !== "currency" ? ` ${esc(f.currency)}` : ""}${f.taxRate ? `<span class="cell-sub">rate ${esc(f.taxRate)}%</span>` : ""}</td>
          <td data-label="Status">${pill(INV_STATUS, f.status)}${f.note ? `<span class="cell-sub">${esc(f.note)}</span>` : ""}</td>
          <td data-label="Page">${esc(f.page ?? "–")}</td></tr>`; }).join("")}
        </tbody></table></div></section>
      <section class="panel"><h3 style="margin-bottom:10px">Consistency checks</h3>
        <ul class="checks">${r.checks.map((c) => `<li>${pill(CHECK_STATUS, c.status)} <b>${esc(CHECK_NAMES[c.id] || c.id)}</b><span class="small">${esc(c.message)}</span></li>`).join("")}</ul>
        <p class="small muted" style="margin:8px 0 0">Arithmetic allows ${"0.01"} for rounding and includes tax, shipping and discount lines when they are found. A failed check is a reason to look, not proof of an error.</p></section>
      <section class="panel" id="docFindings"><div class="panel-head"><h3>Findings for this document</h3><a class="btn quiet sm" href="#/anomalies">All findings</a></div>
        ${r.invoiceLike ? (r.findings.length ? r.findings.map((f) => findingCard(f, false)).join("") : `<p class="muted small">No rule produced a finding for this document.</p>`) : `<p class="muted small">Not applicable.</p>`}</section>`;
    bindFindingActions(el, () => tabInvoice(d, el));
  }

  // ------------------------------------------------------------------ anomalies page
  const anState = { status: "open", severity: "" };
  async function viewAnomalies() {
    crumbs([["Anomalies"]]);
    if (!$("#anList")) setView(loading("Running checks…"));
    const p = new URLSearchParams();
    if (anState.status) p.set("status", anState.status);
    if (anState.severity) p.set("severity", anState.severity);
    const r = await api("GET", `/anomalies?${p}`);
    const o = r.summary.openBySeverity;
    setView(`<div class="page-head"><div><h1>Anomalies</h1>
        <p>Deterministic rules applied to ${plural(r.documentsChecked, "invoice document")}. Each finding names its rule, shows the evidence and suggests a review step. Findings are prompts to check, not conclusions about fraud.</p></div>
        <div class="actions"><button class="btn" type="button" id="anRefresh">${icon("refresh")}Re-run checks</button></div></div>
      <section class="ledger" aria-label="Open findings by severity">
        ${["high", "medium", "low", "info"].map((k) => `<button type="button" class="ledger-cell as-btn ${k === "high" && o[k] ? "bad" : k === "medium" && o[k] ? "attention" : ""}" data-sev="${k}" aria-pressed="${anState.severity === k}">
          <span class="n">${o[k]}</span><span class="l">Open ${SEV[k][0].toLowerCase()}</span></button>`).join("")}
      </section>
      <p class="source-note">${r.summary.acknowledged} acknowledged and ${r.summary.dismissed} dismissed. High-value threshold: ${esc(r.highValueThreshold)} (same currency unit, no conversion).</p>
      <div class="toolbar">
        <label class="field">Review status<select id="anStatus"><option value="open">Open</option><option value="acknowledged">Acknowledged</option><option value="dismissed">Dismissed</option><option value="">All</option></select></label>
        <label class="field">Severity<select id="anSev"><option value="">All</option>${Object.entries(SEV).map(([k, [l]]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
      </div>
      <div id="anList">${r.findings.length ? r.findings.map((f) => findingCard(f, true)).join("")
        : `<section class="panel">${emptyState(anState.status === "open" ? "No open findings" : "No findings match", anState.status === "open" ? "Every rule passed, or all findings have been reviewed." : "Change the filters to see other findings.")}</section>`}</div>
      <details class="panel" style="margin-top:18px"><summary><strong>How findings are decided</strong></summary>
        <div class="table-wrap" style="margin-top:10px"><table class="stack-sm"><thead><tr><th>Rule</th><th>Severity</th><th>Suggested review</th></tr></thead><tbody>
        ${Object.entries(r.rules).map(([k, v]) => `<tr><td class="primary-cell"><strong>${esc(v.title)}</strong><span class="cell-sub"><code>${esc(k)}</code></span></td><td data-label="Severity">${esc(v.severity)}</td><td data-label="Review">${esc(v.action)}</td></tr>`).join("")}
        </tbody></table></div></details>`);
    $("#anStatus").value = anState.status; $("#anSev").value = anState.severity;
    $("#anStatus").onchange = () => { anState.status = $("#anStatus").value; viewAnomalies(); };
    $("#anSev").onchange = () => { anState.severity = $("#anSev").value; viewAnomalies(); };
    $$("[data-sev]").forEach((b) => (b.onclick = () => { anState.severity = anState.severity === b.dataset.sev ? "" : b.dataset.sev; anState.status = "open"; viewAnomalies(); }));
    $("#anRefresh").onclick = viewAnomalies;
    bindFindingActions(view(), viewAnomalies);
  }

  // ------------------------------------------------------------------ versions (Feature E)
  const CHANGE = { changed: ["Changed", "warn"], added: ["Added", "ok"], removed: ["Removed", "bad"] };
  async function tabVersions(d, el) {
    el.innerHTML = loading("Loading versions…");
    const [r, docs] = await Promise.all([api("GET", `/documents/${d.docId}/versions`), loadDocs()]);
    const candidates = docs.filter((x) => x.status === "COMPLETED" && x.docId !== d.docId);
    const rows = (fd) => ["changed", "added", "removed"].flatMap((k) => fd[k].map((c) => `<tr><td class="primary-cell">${esc(c.label)}</td>
      <td data-label="Change">${pill(CHANGE, k)}</td><td data-label="Before">${c.before ? esc(c.before) : `<span class="muted">–</span>`}</td>
      <td data-label="After">${c.after ? esc(c.after) : `<span class="muted">–</span>`}</td></tr>`)).join("");
    el.innerHTML = `<section class="panel"><div class="panel-head"><div><h2>Version history</h2>
        <p>Linking records that one document revises another. Both documents are kept unchanged.</p></div></div>
      <ol class="timeline">${r.versions.map((v) => `<li class="${v.docId === d.docId ? "current" : ""}"><span class="vnum">${v.versionNumber ? `v${esc(v.versionNumber)}` : "–"}</span>
        <div><a class="doc-name" href="#/documents/${esc(v.docId)}/versions">${esc(v.fileName)}</a>${v.docId === d.docId ? ` <span class="pill info plain">This document</span>` : ""}
        <span class="cell-sub">Uploaded ${fmtTime(v.createdAt)}${v.invoiceNumber ? `, invoice ${esc(v.invoiceNumber)}` : ""}${v.invoiceDate ? `, dated ${esc(v.invoiceDate)}` : ""}${v.total ? `, total ${esc(v.total)}` : ""}</span></div></li>`).join("")}</ol>
      ${r.versionGroup ? "" : candidates.length ? `<form class="toolbar" id="linkForm" style="margin-top:14px">
          <label class="field grow">This document revises<select id="linkPrev">${candidates.map((x) => `<option value="${esc(x.docId)}">${esc(x.fileName)}${x.versionNumber ? ` (version ${esc(x.versionNumber)})` : ""}</option>`).join("")}</select></label>
          <button class="btn primary" type="submit">Link as new version</button></form>
          <p class="small muted">You can also choose the earlier version when uploading.</p>` : `<p class="small muted">Upload another processed document to link versions.</p>`}</section>
      ${r.changes.map((c) => `<section class="panel"><div class="panel-head"><div><h3>Version ${esc(c.fromVersion)} to version ${esc(c.toVersion)} <span class="prov genuine" title="${esc(c.method)}">Computed</span></h3>
          <p>Detected differences only; what a change means for the business is not interpreted.</p></div>
          <a class="btn sm" href="#/compare?a=${esc(c.from)}&b=${esc(c.to)}">${icon("compare")}Full comparison</a></div>
        ${c.identicalText ? `<div class="notice">${icon("info")}<p>The extracted text of these versions is identical.</p></div>` : ""}
        <h4 style="margin:4px 0 8px">Invoice fields</h4>
        ${rows(c.fields) ? `<div class="table-wrap"><table class="stack-sm"><thead><tr><th>Field</th><th>Change</th><th>Before</th><th>After</th></tr></thead><tbody>${rows(c.fields)}</tbody></table></div>` : `<p class="muted small">No invoice field changed.</p>`}
        ${c.text ? `<h4 style="margin:14px 0 6px">Text</h4><p class="small">${c.text.changedCount} changed, ${c.text.addedCount} added and ${c.text.removedCount} removed lines; ${(c.text.lineSimilarity * 100).toFixed(0)}% line similarity.</p>
          ${c.text.changed.slice(0, 5).map((x) => `<div class="small" style="margin-bottom:6px"><span class="diff-del">− ${esc(x.before)}</span><br><span class="diff-add">+ ${esc(x.after)}</span></div>`).join("")}
          ${c.text.added.slice(0, 5).map((x) => `<div class="small diff-add">+ ${esc(x)}</div>`).join("")}
          ${c.text.removed.slice(0, 5).map((x) => `<div class="small diff-del">− ${esc(x)}</div>`).join("")}` : ""}</section>`).join("")}`;
    const form = $("#linkForm", el);
    if (form) form.onsubmit = async (e) => {
      e.preventDefault();
      const btn = $("button[type=submit]", form); busy(btn, true, "Linking…");
      try {
        const v = await api("POST", `/documents/${d.docId}/versions`, { previousDocId: $("#linkPrev").value });
        toast(`Linked as version ${v.versionNumber}.`);
        S.doc = null; S.docs = null; route();
      } catch (err) { busy(btn, false); toast(err.message, "bad"); }
    };
  }

  // ------------------------------------------------------------------ upload dialog
  const UP = { file: null, state: "idle", msg: "", code: "", doc: null, problem: "" };
  const dlg = $("#uploadDialog");
  function acceptList() { return isLocal() ? [".pdf"] : [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"]; }
  function openUpload() {
    if (UP.state === "done" || UP.state === "error") Object.assign(UP, { file: null, state: "idle", msg: "", doc: null, problem: "" });
    renderUpload();
    if (!dlg.open) dlg.showModal();
    if (isLocal() && !S.docs) loadDocs().then(() => { if (UP.state === "idle" || UP.state === "selected") renderUpload(); }).catch(() => {});
  }
  $("#uploadOpen").onclick = openUpload;
  function validateFile(f) {
    const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (!acceptList().includes(ext)) return isLocal() && [".png", ".jpg", ".jpeg", ".tif", ".tiff"].includes(ext)
      ? "Images need OCR, which the local demo does not include. Choose a PDF that contains selectable text." : `Choose a ${acceptList().join(", ")} file.`;
    if (f.size === 0) return "This file is empty (0 bytes).";
    if (isLocal() && f.size > S.local.maxUploadBytes) return `This file is ${fmtBytes(f.size)}; the limit is ${fmtBytes(S.local.maxUploadBytes)}.`;
    return "";
  }
  function pickFile(f) {
    if (!f) return;
    Object.assign(UP, { file: f, state: "selected", problem: validateFile(f), msg: "", doc: null });
    renderUpload();
  }
  function renderUpload() {
    const body = $("#uploadBody");
    const limits = isLocal() ? `PDF only, up to ${fmtBytes(S.local.maxUploadBytes)}. The PDF must contain selectable text (scanned pages need OCR).`
      : "PDF, PNG, JPEG or TIFF. The file goes straight to S3 and is processed in AWS.";
    const fileCard = UP.file ? `<div class="file-card">${icon("file")}<div><b>${esc(UP.file.name)}</b><span class="small muted">${fmtBytes(UP.file.size)}${UP.file.type ? `, ${esc(UP.file.type)}` : ""}</span></div>
      ${UP.state === "uploading" ? "" : `<button class="btn quiet sm" type="button" id="upClear">Remove</button>`}</div>` : "";
    if (UP.state === "uploading") {
      body.innerHTML = `${fileCard}<div class="loading" role="status">${`<span class="spinner" aria-hidden="true"></span>`}<div><strong>${esc(UP.msg)}</strong>
        <div class="small">${isLocal() ? "Text extraction and analysis run on this computer and usually finish in a few seconds." : "Processing runs in AWS and usually takes 10 to 60 seconds."} You can close this window; you will be notified when it finishes.</div></div></div>`;
      return;
    }
    if (UP.state === "done") {
      body.innerHTML = `${fileCard}<div class="notice ${UP.code === "duplicate" ? "" : "ok"}" role="status" style="margin-top:14px">${icon(UP.code === "duplicate" ? "info" : "check")}<div><p><strong>${UP.code === "duplicate" ? "Already uploaded" : "Document processed"}</strong></p><p>${esc(UP.msg)}</p></div></div>
        <div class="dialog-actions"><button class="btn" type="button" id="upAnother">Upload another</button>${UP.doc ? `<button class="btn primary" type="button" id="upOpenDoc">Open document</button>` : ""}</div>`;
    } else if (UP.state === "error") {
      body.innerHTML = `${fileCard}<div class="notice bad" role="alert" style="margin-top:14px">${icon("alert")}<div><p><strong>${esc(UP.title || "Upload failed")}</strong></p><p>${esc(UP.msg)}</p>
        ${UP.doc ? `<p>A failed record was added to the document list so the attempt stays visible.</p>` : ""}</div></div>
        <div class="dialog-actions"><button class="btn" type="button" id="upAnother">Choose another file</button>
          ${UP.doc ? `<button class="btn" type="button" id="upOpenDoc">Open failed record</button>` : ""}
          ${UP.retryable ? `<button class="btn primary" type="button" id="upRetry">${icon("refresh")}Try again</button>` : ""}</div>`;
    } else {
      body.innerHTML = `<div class="drop" id="drop">${icon("upload")}<p><strong>Drag a file here</strong> or</p>
          <label class="btn" for="upInput">Choose file</label><input type="file" id="upInput" accept="${acceptList().join(",")}" class="sr-only"></div>
        <p class="small muted" style="margin:10px 0 0">${limits}</p>
        ${fileCard}${UP.problem ? `<p class="small" role="alert" style="color:var(--danger);margin-top:8px">${esc(UP.problem)}</p>` : ""}
        ${isLocal() && (S.docs || []).some((d) => d.status === "COMPLETED") ? `<label class="field" style="margin-top:14px">This file is a revised version of (optional)
          <select id="upPrev"><option value="">Not a revision</option>${S.docs.filter((d) => d.status === "COMPLETED").map((d) => `<option value="${esc(d.docId)}" ${UP.prev === d.docId ? "selected" : ""}>${esc(d.fileName)}</option>`).join("")}</select></label>` : ""}
        <div class="dialog-actions"><button class="btn" type="button" id="upCancel">Cancel</button>
          <button class="btn primary" type="button" id="upGo" ${!UP.file || UP.problem || (isLocal() && !(S.local.pdfExtraction || {}).available) ? "disabled" : ""}>${icon("upload")}Upload and process</button></div>
        ${isLocal() && !(S.local.pdfExtraction || {}).available ? `<div class="notice bad" style="margin-top:12px">${icon("alert")}<p>PyMuPDF is not installed on the server, so PDFs cannot be read. Run <code>pip install pymupdf==1.28.2</code> and restart local_server.py.</p></div>` : ""}`;
      const drop = $("#drop");
      $("#upInput").onchange = (e) => pickFile(e.target.files[0]);
      drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
      drop.ondragleave = () => drop.classList.remove("over");
      drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pickFile(e.dataTransfer.files[0]); };
      $("#upCancel").onclick = () => dlg.close();
      if ($("#upPrev")) $("#upPrev").onchange = (e) => { UP.prev = e.target.value; };
      $("#upGo").onclick = doUpload;
    }
    if ($("#upClear")) $("#upClear").onclick = () => { Object.assign(UP, { file: null, state: "idle", problem: "" }); renderUpload(); };
    if ($("#upAnother")) $("#upAnother").onclick = () => { Object.assign(UP, { file: null, state: "idle", msg: "", doc: null, problem: "" }); renderUpload(); };
    if ($("#upRetry")) $("#upRetry").onclick = doUpload;
    if ($("#upOpenDoc")) $("#upOpenDoc").onclick = () => { dlg.close(); S.doc = null; location.hash = `#/documents/${UP.doc.docId}`; };
  }
  const ERROR_TITLES = {
    ocr_required: "This PDF needs OCR", not_a_pdf: "Not a PDF", malformed_pdf: "The PDF is damaged", encrypted_pdf: "The PDF is password-protected",
    too_large: "File too large", empty_file: "The file is empty", unsupported_type: "Unsupported file type", too_many_pages: "Too many pages",
    extractor_unavailable: "PDF reader not installed", processing_failed: "Processing failed",
  };
  async function doUpload() {
    const f = UP.file;
    if (!f || UP.problem) return;
    Object.assign(UP, { state: "uploading", msg: `Uploading and processing ${f.name}…` });
    renderUpload();
    try {
      if (isLocal()) await uploadLocal(f); else await uploadRemote(f);
    } catch (err) {
      Object.assign(UP, { state: "error", title: "Upload failed", msg: err.message, retryable: true, doc: null });
    }
    invalidate();
    renderUpload();
    if (!dlg.open) toast(UP.state === "done" ? `${f.name}: ${UP.msg}` : `${f.name}: ${UP.msg}`, UP.state === "done" ? "" : "bad");
    const { path } = parseHash();
    if (/^\/(dashboard|documents|review)$/.test(path)) route();
  }
  async function uploadLocal(f) {
    let r, data = {};
    try {
      r = await fetch(`/local/upload?fileName=${encodeURIComponent(f.name)}`, { method: "POST", headers: { "Content-Type": "application/pdf", "X-User-Id": S.user }, body: f });
    } catch (e) { throw new Error("Cannot reach the local server. Check that local_server.py is still running, then try again."); }
    try { data = await r.json(); } catch (e) { /* non-JSON */ }
    if (r.ok) {
      Object.assign(UP, { state: "done", code: data.duplicate ? "duplicate" : "ok", msg: data.message || "Processed.", doc: data.document });
      if (UP.prev && !data.duplicate && data.document) {
        try {
          const v = await api("POST", `/documents/${data.document.docId}/versions`, { previousDocId: UP.prev });
          UP.msg += ` Linked as version ${v.versionNumber} of the selected document.`;
        } catch (e) { UP.msg += ` The version link failed: ${e.message}`; }
      }
      UP.prev = "";
      return;
    }
    Object.assign(UP, {
      state: "error", code: data.code, title: ERROR_TITLES[data.code] || "Upload failed",
      msg: data.error || `The server answered HTTP ${r.status}.`, doc: data.document || null,
      retryable: r.status >= 500 || data.code === "internal_error",
    });
  }
  async function uploadRemote(f) {
    const up = await api("POST", "/documents/upload-url", { fileName: f.name });
    UP.msg = `Uploading ${f.name}…`; renderUpload();
    let put;
    try { put = await fetch(up.uploadUrl, { method: "PUT", headers: { "Content-Type": up.contentType }, body: f }); }
    catch (e) { throw new Error("The upload to storage failed (network or CORS error). Check your connection and the bucket's CORS settings."); }
    if (!put.ok) throw new Error(`Storage rejected the upload (HTTP ${put.status}).`);
    const name = up.key.split("/").pop();
    UP.msg = `Uploaded. Waiting for ${name} to be processed…`; renderUpload();
    const started = Date.now();
    while (Date.now() - started < 5 * 60 * 1000) {
      await new Promise((res) => setTimeout(res, 5000));
      const docs = (await api("GET", "/documents")).documents;
      const mine = docs.find((d) => d.fileName === name && d.createdAt * 1000 >= started - 60000);
      if (mine && mine.status === "COMPLETED") { Object.assign(UP, { state: "done", code: "ok", msg: "Processing finished.", doc: mine }); return; }
      if (mine && mine.status === "FAILED") { Object.assign(UP, { state: "error", title: "Processing failed", msg: mine.errorMessage || "See the document for details.", doc: mine, retryable: false }); return; }
    }
    Object.assign(UP, { state: "done", code: "duplicate", msg: "The file was uploaded but is still processing after 5 minutes. Check the Documents page later.", doc: null });
  }

  // ------------------------------------------------------------------ start-up
  async function boot() {
    if (/^https?:$/.test(location.protocol)) {
      try {
        const r = await fetch("/local/config", { headers: { Accept: "application/json" } });
        if (r.ok) {
          const cfg = await r.json();
          if (cfg.mode === "local-demo") {
            S.mode = "local"; S.base = ""; S.local = cfg;
            S.fixtures = new Set((cfg.fixtures || []).map((x) => x.docId));
          }
        }
      } catch (e) { /* not the local server */ }
    }
    if (!S.mode) { S.mode = "remote"; S.base = store.get("di.url"); S.key = store.get("di.key"); }
    renderModeBox();
    window.addEventListener("hashchange", route);
    if (!location.hash) history.replaceState(null, "", isLocal() || S.base ? "#/dashboard" : "#/settings");
    route();
  }
  boot();
})();

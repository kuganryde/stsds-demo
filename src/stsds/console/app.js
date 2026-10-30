// STSDS console. Security notes: the token lives in a closure only (never storage, URL or cookies); every
// value from the server is inserted as a plain text node, never parsed as markup; the page is served with a
// CSP that forbids inline script, inline style and all third-party origins. What a person sees is filtered by
// the roles the server reports, but the server enforces every rule again.
(() => {
  "use strict";
  let token = "";
  let me = null;
  let current = null;       // document open on the review page
  let backTo = "review";    // page the detail and proof pages return to
  let queue = [];
  let page = "";
  let openBatchId = null;
  const $ = (id) => document.getElementById(id);
  const hasRole = (...r) => !!me && r.some((x) => me.roles.includes(x));

  const LABEL = {
    INGESTED: "Received", CLASSIFIED: "Read", CALCULATED: "Duty calculated", PREFLIGHT_OK: "Checks passed",
    PARITY_OK: "Matches LHDN", SUBMITTING: "Submitting", SUBMITTED: "Filed, payment due", PAYING: "Paying",
    PAID: "Paid", VAULTED: "Certificate saved", COMPLETE: "Complete and verified", REVIEW: "Waiting for review",
    AWAITING_APPROVAL: "Waiting for a checker", PAYMENT_UNCERTAIN: "Payment unclear", SUBMIT_UNCERTAIN: "Submission unclear",
    VERIFY_FAILED: "Verification failed", HALTED: "Stopped", REJECTED: "Rejected", REJECTED_AT_INTAKE: "Rejected at intake",
  };
  const KIND = { COMPLETE: "ok", VAULTED: "ok", PAID: "ok", REVIEW: "wait", AWAITING_APPROVAL: "wait", SUBMITTED: "wait",
    PAYMENT_UNCERTAIN: "bad", SUBMIT_UNCERTAIN: "bad", VERIFY_FAILED: "bad", HALTED: "bad", REJECTED: "bad", REJECTED_AT_INTAKE: "bad" };
  const CHECK_LABEL = {
    audit_chain: "Audit log is unbroken", certificate_hashed: "Certificate fingerprinted", vault_retention: "Locked for 7 years",
    amounts_agree: "Our duty equals the LHDN figure", payment_matches_duty: "Payment equals the duty",
    payment_confirmed_by_provider: "Payment provider confirms it", portal_shows_stamped: "Portal shows the bill as stamped",
    qr_verified: "Certificate QR verified on the official host", ledger_chain: "Ledger chain is unbroken",
  };

  function say(msg, bad) {
    const s = me ? $("status") : $("signin-status");
    s.textContent = msg;
    s.className = "status " + (bad ? "bad" : "good");
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ Authorization: "Bearer " + token }, opts.headers || {});
    const res = await fetch(path, Object.assign({}, opts, { headers, cache: "no-store" }));
    if (!res.ok) {
      let detail = res.status + "";
      try {
        const j = await res.json();
        detail = typeof j.detail === "string" ? j.detail : (j.detail && j.detail.message) || detail;
      } catch (e) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return res;
  }

  function cell(tr, text, cls) {
    const td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    if (cls) td.className = cls;
    tr.appendChild(td);
    return td;
  }

  function pill(state) {
    const s = document.createElement("span");
    s.className = "pill " + (KIND[state] || "");
    s.textContent = LABEL[state] || String(state).toLowerCase().replace(/_/g, " ");
    return s;
  }

  function emptyRow(body, text, cols) {
    const tr = document.createElement("tr");
    cell(tr, text).colSpan = cols;
    body.appendChild(tr);
  }

  function actionCell(tr, ...buttons) {
    const td = document.createElement("td");
    for (const b of buttons) td.appendChild(b);
    tr.appendChild(td);
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? String(iso || "") : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function scoreBar(score) {
    const td = document.createElement("td");
    if (score == null) { td.textContent = "–"; return td; }
    const bar = document.createElement("span"), fill = document.createElement("i");
    bar.className = "bar";
    fill.className = "w" + Math.max(0, Math.min(20, Math.round(score * 20))) * 5;   // width classes: the CSP forbids inline style
    bar.appendChild(fill);
    td.appendChild(bar);
    td.appendChild(document.createTextNode(score.toFixed(2)));
    if (me && score < me.threshold) td.className = "bad";
    return td;
  }

  // ---- shell and navigation ---------------------------------------------------------------------
  const PAGES = {
    clients: { load: () => loadClients() },
    batches: { load: () => loadBatches() },
    signing: { feature: "signing", load: () => loadSigning() },
    review: { roles: ["reviewer", "checker"], load: () => loadQueues() },
    approvals: { roles: ["reviewer", "checker"], load: () => loadQueues() },
    exceptions: { roles: ["reviewer", "checker"], load: () => loadQueues() },
    dashboard: { load: () => loadDashboard() },
    register: { load: () => loadRegister() },
    ledger: { roles: ["operator", "checker"], feature: "ledger", load: () => loadLedger() },
  };

  function allowed(name) {
    const p = PAGES[name];
    if (!p || !me) return false;
    if (p.roles && !hasRole(...p.roles)) return false;
    if (p.feature && !me.features[p.feature]) return false;
    return true;
  }

  function showPage(name, navName) {
    page = name;
    for (const el of document.querySelectorAll(".page")) el.hidden = el.id !== "page-" + name;
    const active = navName || name;
    for (const b of document.querySelectorAll("button.nav[data-page]")) {
      if (b.dataset.page === active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    }
    const p = PAGES[name];
    if (p) p.load().catch((e) => say(e.message, true));
    $("main").scrollTop = 0;
    window.scrollTo(0, 0);
  }

  function enterApp() {
    $("signin-view").hidden = true;
    $("app").hidden = false;
    for (const b of document.querySelectorAll("button.nav[data-page]")) b.hidden = !allowed(b.dataset.page);
    const who = $("who");
    who.replaceChildren();
    const b = document.createElement("b");
    b.textContent = me.name;
    who.append("Signed in as ", b, document.createElement("br"), me.roles.join(", "), document.createElement("br"),
      me.mode === "live" ? "Mode: live" : "Mode: shadow (nothing is paid)", document.createElement("br"), "Rules " + me.rules);
    const first = hasRole("reviewer") ? "review" : hasRole("checker") ? "approvals" : "batches";
    showPage(allowed(first) ? first : "clients");
    if (allowed("review")) loadQueues().catch(() => {});
  }

  // ---- queues: review, approvals, exceptions -----------------------------------------------------
  const isApproval = (i) => i.state === "AWAITING_APPROVAL" || i.state === "SUBMITTED";
  const isReview = (i) => i.state === "REVIEW" && i.resolvable;
  const isException = (i) => !isApproval(i) && !isReview(i);

  function setBadge(name, n) {
    const b = $("badge-" + name);
    b.textContent = String(n);
    b.hidden = n === 0;
  }

  async function loadQueues() {
    queue = (await (await api("/v1/review/queue")).json()).items;
    const rv = queue.filter(isReview), ap = queue.filter(isApproval), ex = queue.filter(isException);
    setBadge("review", rv.length); setBadge("approvals", ap.length); setBadge("exceptions", ex.length);

    const body = $("q-review");
    body.replaceChildren();
    if (!rv.length) emptyRow(body, "Nothing is waiting for review.", 5);
    for (const it of rv) {
      const tr = document.createElement("tr");
      cell(tr, it.name || it.txn_id.slice(0, 8));
      tr.appendChild(scoreBar(it.score));
      cell(tr, it.duty_rm || "–");
      cell(tr, (it.code ? it.code + ": " : "") + (it.message || ""));
      actionCell(tr, btn("Open", () => openItem(it.txn_id, "review"), "Open " + (it.name || it.txn_id)));
      body.appendChild(tr);
    }

    const ab = $("q-approvals");
    ab.replaceChildren();
    if (!ap.length) emptyRow(ab, "Nothing is waiting for a second approval.", 5);
    for (const it of ap) {
      const tr = document.createElement("tr");
      cell(tr, it.name || it.txn_id.slice(0, 8));
      cell(tr, it.reviewed_by || "–");
      cell(tr, it.duty_rm || "–");
      cell(tr, it.state === "SUBMITTED" ? "Payment retry: " + (it.message || "needs a named approver")
        : it.needs_second_approval ? "A person edited the extraction, or the client's limit was reached"
        : "The duty is above the single-approver limit");
      actionCell(tr, btn("Open", () => openItem(it.txn_id, "approvals"), "Open " + (it.name || it.txn_id)));
      ab.appendChild(tr);
    }
    renderExceptions(ex);
  }

  function article(title, sub, state) {
    const a = document.createElement("article");
    a.className = "card";
    const h = document.createElement("div");
    h.className = "row spread";
    const t = document.createElement("div");
    const h2 = document.createElement("h2");
    h2.textContent = title;
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = sub;
    t.append(h2, p);
    h.append(t, state);
    a.appendChild(h);
    return a;
  }

  function para(text) {
    const p = document.createElement("p");
    p.textContent = text;
    return p;
  }

  function renderExceptions(items) {
    const box = $("exceptions");
    box.replaceChildren();
    if (!items.length) box.appendChild(para("No exceptions right now."));
    for (const it of items) {
      const name = it.name || it.txn_id.slice(0, 8);
      const duty = it.duty_rm ? " · duty RM " + it.duty_rm : "";
      if (it.state === "REVIEW") {
        const client = (it.code || "").indexOf("CLIENT_") === 0;
        const a = article(name, (it.code || "") + ": " + (it.message || ""), pill("REVIEW"));
        a.appendChild(para(client
          ? "This document is held because of its client. Register or approve the client and it is released by itself; nobody has to approve it through."
          : "This kind of hold cannot be approved from the console. The tax team handles it outside the app. Rejecting records who decided and why."));
        const row = document.createElement("div");
        row.className = "row";
        if (client) row.appendChild(btn("Go to Clients", () => showPage("clients")));
        row.appendChild(btn(hasRole("reviewer") ? "Open and reject" : "Open", () => openItem(it.txn_id, "exceptions"), "Open " + name, true));
        a.appendChild(row);
        box.appendChild(a);
      } else if (it.state === "PAYMENT_UNCERTAIN") {
        const a = article(name, "Payment status unclear" + duty, pill("PAYMENT_UNCERTAIN"));
        a.appendChild(para("The payment provider did not give a clear answer, so the app cannot tell whether money left. It will not retry on its own, because a second payment would be a real loss."));
        const ol = document.createElement("ol");
        for (const t of ["Compare with the bank statement for key stsds-" + it.txn_id.slice(0, 8) + "…",
          "Ask the payment provider. If it reports paid, the document carries on to the stamp.",
          "If it reports unpaid, the document moves to Approvals, and a retry needs a named checker."]) {
          const li = document.createElement("li");
          li.textContent = t;
          ol.appendChild(li);
        }
        a.appendChild(ol);
        const b = btn("Ask the payment provider", () => reconcile(it), "Ask the payment provider about " + name);
        b.disabled = !hasRole("checker");
        if (b.disabled) b.title = "Only a checker can do this";
        a.appendChild(b);
        box.appendChild(a);
      } else if (it.state === "SUBMIT_UNCERTAIN") {
        const a = article(name, "Submission unclear" + duty, pill("SUBMIT_UNCERTAIN"));
        a.appendChild(para("The portal did not confirm that the form was submitted. The app will not submit again, because that could file twice. An officer checks the e-Duti Setem portal for this document's bill; there is no override in this console."));
        box.appendChild(a);
      } else if (it.state === "VERIFY_FAILED") {
        const a = article(name, (it.message || "A final check failed") + duty, pill("VERIFY_FAILED"));
        a.appendChild(para("The certificate is saved, but one of the final checks did not pass, so the document is not marked complete. A person must find out why."));
        a.appendChild(btn("Open the proof", () => showProof(it.txn_id, "exceptions"), "Open the proof for " + name, true));
        box.appendChild(a);
      }
    }
    const ref = document.createElement("section");
    ref.className = "callout";
    const h = document.createElement("h2");
    h.textContent = "Other reasons the app may stop";
    ref.appendChild(h);
    for (const t of ["Not signed, or edited after signing: held at intake, reject only.",
      "Stamping deadline passed: held for the tax team, reject only.",
      "Document type not yet live in the signed rules (PHASE_NOT_LIVE), or a JPPH valuation is needed: reject only.",
      "Client missing, unapproved, suspended or without valid authority: released automatically once fixed on Clients.",
      "Duplicate of a document already filed: stopped. Our total differs from LHDN by even 1 sen: stopped.",
      "The batch payment cap is reached: held for an officer."]) ref.appendChild(para(t));
    box.appendChild(ref);
  }

  async function reconcile(it) {
    try {
      const r = await (await api("/v1/payments/" + it.txn_id + "/reconcile", { method: "POST" })).json();
      const code = r.halt ? r.halt.code : "";
      say(code === "PAYMENT_RETRY_NEEDS_APPROVAL" ? "The provider shows nothing paid. A checker must approve a retry on Approvals."
        : code === "PAYMENT_UNCERTAIN" ? "The provider still cannot confirm the payment."
        : "The provider confirms the payment. The document is now: " + (LABEL[r.state] || r.state), code === "PAYMENT_UNCERTAIN");
      await loadQueues();
    } catch (e) { say("Could not reconcile: " + e.message, true); }
  }

  // ---- one document: review and second approval -------------------------------------------------
  async function openItem(txn, from) {
    const d = await (await api("/v1/review/" + txn)).json();
    current = d;
    backTo = from || backTo;
    const meta = $("meta");
    meta.replaceChildren();
    const lines = [
      "State: " + (LABEL[d.state] || d.state),
      "Score: " + (d.assessment ? d.assessment.score.toFixed(2) + " (auto-process needs " + d.assessment.threshold.toFixed(2) + ")" : "n/a"),
      d.duty_rm ? "Duty calculated by the rules engine: RM " + d.duty_rm + ". You can correct facts but never type the duty." : "",
      d.review ? "Held because: " + d.review.code + " – " + d.review.message : "",
      d.assessment && d.assessment.reasons.length ? "Reasons: " + d.assessment.reasons.join("; ") : "",
      d.assessment ? "Score components: " + Object.entries(d.assessment.components).map(([k, v]) => k + " " + v).join(", ") : "",
      d.reviewed_by ? "Reviewed by: " + d.reviewed_by : "",
    ].filter(Boolean);
    for (const l of lines) meta.appendChild(para(l));
    $("extraction").value = d.extraction ? JSON.stringify(d.extraction, null, 2) : "";
    $("doctext").textContent = d.text + (d.text_truncated ? "\n… (truncated)" : "");
    $("attest").checked = false;
    $("note").value = "";
    const canReview = hasRole("reviewer");
    const reviewable = d.state === "REVIEW" && d.review && d.review.resolvable && canReview;
    $("review-actions").hidden = d.state !== "REVIEW";
    $("approve").disabled = !reviewable;
    $("approve").title = !canReview ? "Only a reviewer can approve a document"
      : reviewable ? "" : "This hold cannot be cleared by editing the extraction. It can only be rejected.";
    $("reject").disabled = !canReview;
    $("reject").title = canReview ? "" : "Only a reviewer can reject a document";
    const waitingCheck = d.state === "AWAITING_APPROVAL" || d.state === "SUBMITTED";
    $("checker-actions").hidden = !waitingCheck;
    $("approve-filing").disabled = !hasRole("checker");
    $("approve-filing").title = hasRole("checker") ? "" : "Only a checker can give the second approval";
    $("checker-note").textContent = d.state === "SUBMITTED"
      ? "The payment was refused, or the provider reported it unpaid. Approving allows one more attempt. The same bank key is used, so a duplicate charge is refused by the provider."
      : d.needs_second_approval
        ? "A person edited this extraction, or the client's own limit was reached, so a different person must approve the filing."
        : "The duty is above the single-approver limit.";
    showPage("detail", backTo);
    $("detail").focus();
  }

  async function decide(decision) {
    if (!current) return;
    const payload = { decision, attest: $("attest").checked, note: $("note").value };
    if (decision === "approve") {
      try { payload.corrections = JSON.parse($("extraction").value); }
      catch (e) { say("The extraction is not valid JSON: " + e.message, true); return; }
      if (!payload.attest) { say("Tick the confirmation that you checked the document.", true); return; }
    }
    try {
      const r = await (await api("/v1/review/" + current.txn_id + "/decision", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })).json();
      say("Saved. The document is now: " + (LABEL[r.state] || r.state) + (r.halt ? " (" + r.halt.code + ")" : ""), false);
      showPage(backTo);
    } catch (e) { say("Could not save: " + e.message, true); }
  }

  async function approveFiling() {
    if (!current) return;
    try {
      const r = await (await api("/v1/approvals/" + current.txn_id, { method: "POST" })).json();
      say("Approved. The document is now: " + (LABEL[r.state] || r.state) + (r.halt ? " (" + r.halt.code + ")" : ""), false);
      showPage(backTo);
    } catch (e) { say("Could not approve: " + e.message, true); }
  }

  async function viewPdf() {
    if (!current) return;
    try {
      const blob = await (await api("/v1/review/" + current.txn_id + "/document")).blob();
      window.open(URL.createObjectURL(blob), "_blank", "noopener");
    } catch (e) { say("The original is no longer available: " + e.message, true); }
  }

  // ---- transaction proof -------------------------------------------------------------------------
  async function showProof(txn, from) {
    const t = await (await api("/v1/transactions/" + txn)).json();
    backTo = from || backTo;
    $("proof-title").textContent = t.name || t.txn_id.slice(0, 8);
    $("proof-sub").textContent = "Transaction " + t.txn_id.slice(0, 8) + "… · " + (LABEL[t.state] || t.state) +
      (t.duty_rm ? " · duty RM " + t.duty_rm : "") + (t.client ? " · client " + t.client : "") +
      (t.reviewed_by ? " · reviewed by " + t.reviewed_by : "") + (t.approved_by ? " · approved by " + t.approved_by : "");
    const tl = $("timeline");
    tl.replaceChildren();
    if (!t.timeline.length) {
      const li = document.createElement("li");
      li.textContent = me.features.ledger ? "No ledger entries yet." : "The ledger is not configured on this deployment.";
      tl.appendChild(li);
    }
    for (const r of t.timeline) {
      const li = document.createElement("li"), when = document.createElement("div"), what = document.createElement("div");
      when.className = "when";
      when.textContent = fmtTime(r.ts);
      what.append(r.stage + " · " + r.outcome + (r.code ? " (" + r.code + ")" : "") + (r.actor && r.actor !== "system" ? " · " + r.actor : ""));
      if (r.detail) { const d = document.createElement("div"); d.className = "hint"; d.textContent = r.detail; what.appendChild(d); }
      const h = document.createElement("div");
      h.className = "mono hint";
      h.textContent = r.hash.slice(0, 16) + "…";
      what.appendChild(h);
      li.append(when, what);
      tl.appendChild(li);
    }
    const ck = $("checks");
    ck.replaceChildren();
    const entries = Object.entries(t.verification || {});
    if (!entries.length) ck.appendChild(para("The final checks have not run yet."));
    else {
      const pass = entries.filter(([, v]) => v === "PASS").length;
      const h = document.createElement("p");
      h.textContent = pass + " of " + entries.length + " checks passed" + (entries.some(([, v]) => v === "SKIPPED") ? " (some do not apply to this setup)" : "");
      ck.appendChild(h);
      for (const [k, v] of entries) {
        const row = document.createElement("div"), s = document.createElement("span");
        row.className = "check-row";
        s.className = "pill " + (v === "PASS" ? "ok" : v === "SKIPPED" ? "" : "bad");
        s.textContent = v === "PASS" ? "Passed" : v === "SKIPPED" ? "Not applicable" : "Failed";
        row.append(s, (CHECK_LABEL[k] || k) + (v.indexOf("FAIL") === 0 ? ": " + v.slice(6) : ""));
        ck.appendChild(row);
      }
    }
    $("vault").textContent = t.vault
      ? "Locked until " + t.vault.retain_until + ". Bundle " + String(t.vault.bundle_sha256).slice(0, 16) + "…, certificate " + String(t.vault.sijil_sha256).slice(0, 16) + "…"
      : "Nothing is vaulted yet.";
    showPage("proof", backTo);
  }

  // ---- batches ---------------------------------------------------------------------------------
  function b64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] || "");
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
  }

  async function loadBatches() {
    const op = hasRole("operator");
    $("upload").disabled = !op;
    $("upload-hint").textContent = op
      ? "Choose several PDFs. Documents for a company that is not an approved client are held until it is registered."
      : "Submitting documents needs the operator service token. You are signed in as " + me.name + ", so you can follow batches but not create them.";
    const data = await (await api("/v1/batches")).json();
    const body = $("batches");
    body.replaceChildren();
    if (!data.items.length) emptyRow(body, "No batches yet.", 7);
    for (const b of data.items) {
      const tr = document.createElement("tr"), t = b.totals;
      cell(tr, b.batch_id.slice(0, 8) + "…"); cell(tr, fmtTime(b.created_at)); cell(tr, t.documents);
      cell(tr, t.complete); cell(tr, t.waiting_for_people, t.waiting_for_people ? "" : ""); cell(tr, t.failed, t.failed ? "bad" : "");
      actionCell(tr, btn("Open", () => openBatch(b.batch_id), "Open batch " + b.batch_id.slice(0, 8)));
      body.appendChild(tr);
    }
    if (openBatchId) await openBatch(openBatchId, true);
  }

  async function openBatch(id, quiet) {
    const s = await (await api("/v1/batches/" + id)).json();
    openBatchId = id;
    $("batch-detail").hidden = false;
    $("bd-h").textContent = "Batch " + id.slice(0, 8) + "… · " + fmtTime(s.created_at) + " · " + s.created_by;
    const t = s.totals;
    $("batch-tiles").replaceChildren(
      tile("Documents", t.documents), tile("Complete and verified", t.complete),
      tile("Waiting for a person", t.waiting_for_people), tile("Stopped", t.failed, t.failed > 0),
      tile("Still processing", t.in_progress), tile("Duty completed (RM)", t.duty_completed_rm),
      tile("Batch payment cap (RM)", t.payment_cap_rm));
    const docs = s.documents.slice().sort((a, b) => (a.score == null) - (b.score == null) || (a.score || 0) - (b.score || 0));
    const body = $("batch-docs");
    body.replaceChildren();
    for (const d of docs) {
      const tr = document.createElement("tr");
      cell(tr, d.name + (d.duplicate_in_batch ? " (duplicate in this batch)" : ""));
      cell(tr, d.item || "–"); cell(tr, d.duty_rm || "–");
      tr.appendChild(scoreBar(d.score));
      const st = document.createElement("td");
      st.appendChild(pill(d.state));
      if (d.code && d.state !== "COMPLETE") { const h = document.createElement("div"); h.className = "hint"; h.textContent = d.code; st.appendChild(h); }
      tr.appendChild(st);
      const waiting = ["REVIEW", "AWAITING_APPROVAL", "SUBMITTED"].includes(d.state) && hasRole("reviewer", "checker") && d.txn_id;
      const target = d.state === "AWAITING_APPROVAL" || d.state === "SUBMITTED" ? "approvals" : d.state === "REVIEW" ? "review" : "exceptions";
      if (waiting) actionCell(tr, btn(d.state === "REVIEW" ? "Review" : "Open", () => openItem(d.txn_id, target), "Open " + d.name));
      else if (d.txn_id) actionCell(tr, btn("View proof", () => showProof(d.txn_id, "batches"), "View the proof for " + d.name, true));
      else tr.appendChild(document.createElement("td"));
      body.appendChild(tr);
    }
    if (!quiet) $("batch-detail").scrollIntoView();
  }

  async function upload() {
    const files = Array.from($("files").files);
    if (!files.length) { say("Choose at least one PDF.", true); return; }
    try {
      const documents = [];
      for (const f of files) documents.push({ name: f.name, pdf_base64: await b64(f) });
      const r = await (await api("/v1/batches", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: "console", documents }) })).json();
      say("Batch submitted: " + r.accepted + " accepted, " + r.rejected_at_intake + " rejected at intake. Processing has started.", false);
      $("files").value = "";
      openBatchId = r.batch_id;
      await loadBatches();
    } catch (e) { say("Upload failed: " + e.message, true); }
  }

  // ---- signing (step 1) ---------------------------------------------------------------------------
  function addSignerRow() {
    const box = $("sg-signers");
    const n = box.children.length;
    if (n >= 10) { say("A document can have at most 10 signers.", true); return; }
    const row = document.createElement("div");
    row.className = "row";
    for (const [ph, type, cls] of [["Signer name", "text", "sg-n"], ["Signer email", "text", "sg-e"]]) {
      const i = document.createElement("input");
      i.type = type; i.placeholder = ph; i.maxLength = 120; i.className = cls;
      i.setAttribute("aria-label", ph + " " + (n + 1));
      row.appendChild(i);
    }
    box.appendChild(row);
  }

  async function loadSigning() {
    if (!$("sg-signers").children.length) { addSignerRow(); addSignerRow(); }
    $("sg-send").disabled = !hasRole("operator");
    const data = await (await api("/v1/signing")).json();
    const body = $("sg-rows");
    body.replaceChildren();
    if (!data.items.length) emptyRow(body, "Nothing has been sent for signature yet.", 4);
    for (const r of data.items) {
      const tr = document.createElement("tr");
      cell(tr, r.name); cell(tr, r.signers_done + " of " + r.signers.length);
      const st = document.createElement("td"), p = document.createElement("span");
      p.className = "pill " + (r.state === "COMPLETED" || r.state === "SIGNED" ? "ok" : r.state === "PENDING" ? "wait" : "bad");
      p.textContent = r.state.toLowerCase().replace(/_/g, " ");
      st.appendChild(p);
      tr.appendChild(st);
      const acts = [];
      if (r.txn_id) acts.push(btn("View proof", () => showProof(r.txn_id, "signing"), "View proof for " + r.name, true));
      else if (hasRole("operator")) acts.push(btn("Check now", () => collectSigning(r.envelope_id), "Check the signing status of " + r.name, true));
      actionCell(tr, ...acts);
      body.appendChild(tr);
    }
  }

  async function collectSigning(id) {
    try {
      const r = await (await api("/v1/signing/" + id + "/collect", { method: "POST" })).json();
      say(r.ingested ? "Everyone has signed. The signed PDF is now in the pipeline." : "Status: " + r.state.toLowerCase() + ".", false);
      await loadSigning();
    } catch (e) { say("Could not check: " + e.message, true); }
  }

  async function sendSigning() {
    const f = $("sg-file").files[0];
    if (!f) { say("Choose a PDF first.", true); return; }
    const signers = Array.from($("sg-signers").children).map((r) => ({ name: r.querySelector(".sg-n").value.trim(), email: r.querySelector(".sg-e").value.trim() }))
      .filter((s) => s.name || s.email);
    if (!signers.length || signers.some((s) => !s.name || !s.email)) { say("Every signer needs a name and an email.", true); return; }
    try {
      await api("/v1/signing/requests", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: $("sg-name").value.trim() || f.name, pdf_base64: await b64(f), signers }) });
      say("Sent for signature.", false);
      $("sg-file").value = ""; $("sg-name").value = "";
      for (const i of document.querySelectorAll("#sg-signers input")) i.value = "";
      await loadSigning();
    } catch (e) { say("Could not send: " + e.message, true); }
  }

  // ---- ledger and audit ----------------------------------------------------------------------------
  async function loadLedger() {
    const d = await (await api("/v1/ledger")).json();
    $("ledger-tiles").replaceChildren(tile("Ledger chain", d.chain_valid ? "Intact" : "BROKEN", !d.chain_valid), tile("Entries", d.rows.length));
    const q = $("lg-q").value.trim().toLowerCase();
    let rows = d.rows.slice().reverse();
    if (q) rows = rows.filter((r) => (r.txn_id + " " + r.name + " " + r.actor + " " + r.stage).toLowerCase().indexOf(q) >= 0);
    const body = $("ledger");
    body.replaceChildren();
    if (!rows.length) emptyRow(body, "No entries match.", 6);
    for (const r of rows.slice(0, 300)) {
      const tr = document.createElement("tr");
      cell(tr, fmtTime(r.ts)); cell(tr, r.name || r.txn_id.slice(0, 8)); cell(tr, r.stage);
      cell(tr, r.outcome + (r.code ? " (" + r.code + ")" : "")); cell(tr, r.actor);
      cell(tr, r.hash.slice(0, 16) + "…", "mono");
      body.appendChild(tr);
    }
    $("lg-note").textContent = rows.length > 300 ? "Showing the newest 300 of " + rows.length + ". Export the CSV for everything." : "";
  }

  function tile(label, value, bad) {
    const d = document.createElement("div");
    d.className = "tile";
    const b = document.createElement("b");
    b.textContent = String(value);
    if (bad) b.className = "bad";
    d.appendChild(b);
    d.appendChild(document.createTextNode(label));
    return d;
  }

  async function loadDashboard() {
    const s = await (await api("/v1/monitoring/summary")).json();
    const by = s.by_status || {}, al = s.alerts || {};
    const tiles = $("tiles");
    tiles.replaceChildren(
      tile("Contracts in the register", s.total),
      tile("Compliance rate", s.compliance_rate_pct == null ? "–" : s.compliance_rate_pct + "%"),
      tile("Stamped", by.STAMPED || 0),
      tile("In progress", by.IN_PROGRESS || 0),
      tile("Unstamped, within 30 days", by.UNSTAMPED || 0),
      tile("Overdue", by.OVERDUE || 0, (by.OVERDUE || 0) > 0),
      tile("Penalty exposure (est., RM)", s.penalty_exposure_rm, Number(s.penalty_exposure_rm) > 0),
      tile("Due within 7 days", al.DUE_SOON || 0),
      tile("Expiring or expired", (al.EXPIRING || 0) + (al.EXPIRED || 0)),
      tile("Duty stamped (RM)", s.duty_stamped_rm));
    const a = await (await api("/v1/monitoring/alerts")).json();
    const body = $("alerts");
    body.replaceChildren();
    if (!a.items.length) {
      const tr = document.createElement("tr");
      cell(tr, "No alerts.").colSpan = 5;
      body.appendChild(tr);
    }
    for (const it of a.items) {
      const tr = document.createElement("tr");
      cell(tr, it.level, it.level === "OVERDUE" || it.level === "EXPIRED" ? "bad" : "");
      cell(tr, it.name); cell(tr, it.entity); cell(tr, it.date); cell(tr, it.message);
      body.appendChild(tr);
    }
  }

  async function loadRegister() {
    const q = new URLSearchParams();
    if ($("reg-q").value.trim()) q.set("q", $("reg-q").value.trim());
    if ($("reg-status").value) q.set("status", $("reg-status").value);
    const data = await (await api("/v1/register?" + q.toString())).json();
    const body = $("register");
    body.replaceChildren();
    if (!data.items.length) {
      const tr = document.createElement("tr");
      cell(tr, "No contracts match.").colSpan = 10;
      body.appendChild(tr);
    }
    for (const e of data.items) {
      const tr = document.createElement("tr");
      cell(tr, e.name); cell(tr, e.entity); cell(tr, e.doc_kind); cell(tr, e.contract_owner || "–");
      cell(tr, e.execution_date); cell(tr, e.duty_rm == null ? "–" : e.duty_rm);
      cell(tr, e.status + (e.attention.length ? " (" + e.attention[0] + ")" : ""), e.status === "OVERDUE" ? "bad" : e.status === "STAMPED" ? "good" : "");
      cell(tr, e.deadline); cell(tr, e.expiry_date || "–");
      cell(tr, e.penalty ? e.penalty.amount_rm + (e.status === "STAMPED" ? " (incurred)" : "") : "–");
      body.appendChild(tr);
    }
  }

  function csvDownload(name, blob) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }


  // ---- clients (step 0) -------------------------------------------------------------------
  let editingId = null;
  const CF = ["c-name", "c-id", "c-tin", "c-rm", "c-owner", "c-auth", "c-until", "c-limit", "c-addr", "c-contact", "c-crole", "c-sign"];

  function btn(label, fn, aria, secondary) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (secondary) b.className = "secondary";
    if (aria) b.setAttribute("aria-label", aria);
    b.addEventListener("click", fn);
    return b;
  }

  function resetClientForm() {
    editingId = null;
    for (const id of CF) $(id).value = "";
    $("c-type").value = "SDN_BHD";
    $("c-idtype").value = "BRN";
    $("cf-title").textContent = "Register a client";
    $("c-save").textContent = "Save as draft";
    $("c-cancel").hidden = true;
  }

  function prefill(p) {
    resetClientForm();
    $("c-name").value = p.name || "";
    $("c-id").value = p.id_number || "";
    $("c-tin").value = p.tin || "";
    if (p.id_type) $("c-idtype").value = p.id_type;
    $("c-rm").focus();
  }

  function parseSignatories(text) {
    return text.split(";").map((s) => s.trim()).filter(Boolean).map((s) => {
      const i = s.indexOf(",");
      return i < 0 ? { name: s } : { name: s.slice(0, i).trim(), designation: s.slice(i + 1).trim() || null };
    });
  }

  function clientBody() {
    const v = (id) => $(id).value.trim() || null;
    const body = { legal_name: v("c-name"), entity_type: $("c-type").value, id_type: $("c-idtype").value, id_number: v("c-id"),
      tin: v("c-tin"), relationship_manager: v("c-rm"), default_contract_owner: v("c-owner"), authority_ref: v("c-auth"),
      authority_valid_until: v("c-until"), approval_limit_rm: v("c-limit"), registered_address: v("c-addr"),
      contact_name: v("c-contact"), contact_role: v("c-crole"), signatories: parseSignatories($("c-sign").value) };
    const required = ["legal_name", "id_number", "tin", "relationship_manager"];
    for (const k of Object.keys(body)) if (body[k] === null && (!editingId || required.includes(k))) delete body[k];
    return body;
  }

  async function saveClient() {
    try {
      const r = await (await api(editingId ? "/v1/clients/" + editingId : "/v1/clients", {
        method: editingId ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(clientBody()) })).json();
      say(editingId ? (r.reapproval ? "Saved. A second person must approve the change before documents are stamped." : "Saved.")
        : "Saved as a draft. A second person must approve it.", false);
      resetClientForm();
      await loadClients();
    } catch (e) { say("Could not save: " + e.message, true); }
  }

  function editClient(c) {
    const d = c.data;
    resetClientForm();
    editingId = c.id;
    const set = (id, val) => { $(id).value = val == null ? "" : String(val); };
    set("c-name", d.legal_name); set("c-id", d.id_number); set("c-tin", d.tin); set("c-rm", d.relationship_manager);
    set("c-owner", d.default_contract_owner); set("c-auth", d.authority_ref); set("c-until", d.authority_valid_until);
    set("c-limit", d.approval_limit_rm); set("c-addr", d.registered_address); set("c-contact", d.contact_name);
    set("c-crole", d.contact_role);
    set("c-sign", (d.signatories || []).map((s) => s.name + (s.designation ? ", " + s.designation : "")).join("; "));
    $("c-type").value = d.entity_type;
    $("c-idtype").value = d.id_type;
    $("cf-title").textContent = "Edit client";
    $("c-save").textContent = "Save changes";
    $("c-cancel").hidden = false;
    $("c-name").focus();
  }

  async function approveClient(c) {
    try {
      const r = await (await api("/v1/clients/" + c.id + "/activate", { method: "POST" })).json();
      say("Approved." + (r.released.length ? " " + r.released.length + " held document(s) released." : ""), false);
      await loadClients();
    } catch (e) { say("Could not approve: " + e.message, true); }
  }

  async function suspendClient(c) {
    try {
      await api("/v1/clients/" + c.id + "/suspend", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Suspended from the console by an approver" }) });
      say("Suspended. Its documents will be held.", false);
      await loadClients();
    } catch (e) { say("Could not suspend: " + e.message, true); }
  }

  async function loadClients() {
    const pend = await (await api("/v1/clients/pending")).json();
    const pb = $("pending");
    pb.replaceChildren();
    if (!pend.items.length) {
      const tr = document.createElement("tr");
      cell(tr, "No documents are waiting for a client.").colSpan = 5;
      pb.appendChild(tr);
    }
    for (const p of pend.items) {
      const tr = document.createElement("tr");
      cell(tr, p.name); cell(tr, p.id_type + " " + p.id_number); cell(tr, p.tin || "–"); cell(tr, p.documents);
      const td = document.createElement("td");
      td.appendChild(btn("Register", () => prefill(p), "Register " + p.name));
      tr.appendChild(td);
      pb.appendChild(tr);
    }
    const q = new URLSearchParams();
    if ($("cl-q").value.trim()) q.set("q", $("cl-q").value.trim());
    if ($("cl-status").value) q.set("status", $("cl-status").value);
    const data = await (await api("/v1/clients?" + q.toString())).json();
    const body = $("clients");
    body.replaceChildren();
    if (!data.items.length) {
      const tr = document.createElement("tr");
      cell(tr, "No clients yet.").colSpan = 8;
      body.appendChild(tr);
    }
    for (const c of data.items) {
      const d = c.data, tr = document.createElement("tr");
      cell(tr, d.legal_name); cell(tr, d.id_type + " " + d.id_number); cell(tr, d.tin); cell(tr, d.relationship_manager);
      cell(tr, (d.authority_ref || "none") + (d.authority_valid_until ? " to " + d.authority_valid_until : ""));
      cell(tr, d.approval_limit_rm || "–");
      cell(tr, c.status + (c.status === "DRAFT" ? " (prepared by " + c.prepared_by + ")" : ""), c.status === "ACTIVE" ? "good" : "bad");
      const td = document.createElement("td");
      td.appendChild(btn("Edit", () => editClient(c), "Edit " + d.legal_name, true));
      const second = c.status !== "ACTIVE"
        ? btn(c.status === "SUSPENDED" ? "Reactivate" : "Approve", () => approveClient(c), "Approve " + d.legal_name)
        : btn("Suspend", () => suspendClient(c), "Suspend " + d.legal_name, true);
      if (!hasRole("checker")) { second.disabled = true; second.title = "Only a checker can approve or suspend a client"; }
      else if (c.status === "DRAFT" && c.prepared_by === me.name) { second.disabled = true; second.title = "You prepared this record, so a different checker must approve it"; }
      td.appendChild(second);
      tr.appendChild(td);
      body.appendChild(tr);
    }
  }

  async function addContract() {
    const v = (id) => $(id).value.trim() || null;
    const body = { name: v("a-name"), entity: v("a-entity"), doc_kind: $("a-kind").value, execution_date: v("a-exec"),
      duty_rm: v("a-duty"), contract_owner: v("a-owner"), expiry_date: v("a-expiry"), certificate_ref: v("a-cert") };
    for (const k of Object.keys(body)) if (body[k] === null) delete body[k];
    try {
      await api("/v1/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      for (const id of ["a-name", "a-entity", "a-exec", "a-duty", "a-owner", "a-expiry", "a-cert"]) $(id).value = "";
      say("Added to the register.", false);
      await loadRegister();
    } catch (e) { say("Could not add: " + e.message, true); }
  }

  async function importCsv() {
    const f = $("a-file").files[0];
    if (!f) { say("Choose a CSV file first.", true); return; }
    try {
      const r = await (await api("/v1/register/import", { method: "POST", headers: { "Content-Type": "text/csv" }, body: await f.text() })).json();
      say("Imported " + r.imported + " contracts.", false);
      await loadRegister();
    } catch (e) { say("Import failed: " + e.message, true); }
  }

  async function runReport() {
    const m = $("rep-month").value.trim() || new Date().toISOString().slice(0, 7);
    try {
      const r = await (await api("/v1/reports/monthly?month=" + encodeURIComponent(m))).json();
      $("rep-out").textContent = "Monthly review " + r.month + ": " + r.executed.length + " executed, " + r.stamped.length +
        " stamped, " + r.outstanding.length + " still outstanding, " + r.late_stamped.length + " stamped late, " +
        r.expiring.length + " expiring or expired. Estimated penalty exposure RM" + r.summary.penalty_exposure_rm + ".";
    } catch (e) { say("Report failed: " + e.message, true); }
  }


  // ---- wiring -------------------------------------------------------------------------------------
  for (const b of document.querySelectorAll("button.nav[data-page]")) b.addEventListener("click", () => { $("status").textContent = ""; showPage(b.dataset.page); });
  $("signout").addEventListener("click", () => { token = ""; me = null; location.reload(); });
  $("detail-back").addEventListener("click", () => { $("status").textContent = ""; showPage(backTo); });
  $("proof-back").addEventListener("click", () => { $("status").textContent = ""; showPage(backTo); });
  $("dash-refresh").addEventListener("click", () => loadDashboard().catch((e) => say(e.message, true)));
  $("rep-run").addEventListener("click", runReport);
  $("reg-go").addEventListener("click", () => loadRegister().catch((e) => say(e.message, true)));
  $("reg-csv").addEventListener("click", async () => {
    try { csvDownload("stsds-register.csv", await (await api("/v1/register.csv")).blob()); }
    catch (e) { say("Export needs the checker role: " + e.message, true); }
  });
  $("lg-go").addEventListener("click", () => loadLedger().catch((e) => say(e.message, true)));
  $("lg-csv").addEventListener("click", async () => {
    try { csvDownload("stsds-ledger.csv", await (await api("/v1/ledger.csv")).blob()); }
    catch (e) { say("Could not export: " + e.message, true); }
  });
  $("cl-go").addEventListener("click", () => loadClients().catch((e) => say(e.message, true)));
  $("c-save").addEventListener("click", saveClient);
  $("c-cancel").addEventListener("click", resetClientForm);
  $("a-add").addEventListener("click", addContract);
  $("a-import").addEventListener("click", importCsv);
  $("batches-refresh").addEventListener("click", () => loadBatches().catch((e) => say(e.message, true)));
  $("upload").addEventListener("click", upload);
  $("sg-add").addEventListener("click", addSignerRow);
  $("sg-send").addEventListener("click", sendSigning);
  $("sg-refresh").addEventListener("click", () => loadSigning().catch((e) => say(e.message, true)));
  $("approve").addEventListener("click", () => decide("approve"));
  $("reject").addEventListener("click", () => decide("reject"));
  $("approve-filing").addEventListener("click", approveFiling);
  $("viewpdf").addEventListener("click", viewPdf);

  $("signin").addEventListener("click", async () => {
    token = $("token").value.trim();
    $("token").value = "";
    try {
      me = await (await api("/v1/me")).json();
      enterApp();
    } catch (e) { token = ""; me = null; say("Sign-in failed: " + e.message, true); }
  });
  $("token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("signin").click(); });

  // While a batch is open on screen and still processing, refresh it quietly.
  setInterval(() => {
    if (me && page === "batches" && openBatchId && !document.hidden) openBatch(openBatchId, true).catch(() => {});
  }, 5000);
})();

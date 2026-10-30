// Reviewer console. Security notes: the token lives in a closure only (never storage, URL or
// cookies); every value from the server is inserted as a plain text node, never parsed as markup; the page
// is served with a CSP that forbids inline script and all third-party origins.
(() => {
  "use strict";
  let token = "";
  let current = null;
  const $ = (id) => document.getElementById(id);

  function say(msg, bad) {
    const s = $("status");
    s.textContent = msg;
    s.className = bad ? "bad" : "good";
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

  async function loadQueue() {
    const body = $("queue");
    const data = await (await api("/v1/review/queue")).json();
    body.replaceChildren();
    if (!data.items.length) {
      const tr = document.createElement("tr");
      const td = cell(tr, "Nothing is waiting for you.");
      td.colSpan = 6;
      body.appendChild(tr);
      return;
    }
    for (const it of data.items) {
      const tr = document.createElement("tr");
      cell(tr, it.name || it.txn_id.slice(0, 8));
      cell(tr, it.state);
      cell(tr, it.score == null ? "–" : it.score.toFixed(2), it.score != null && it.score < 0.95 ? "bad" : "");
      cell(tr, it.duty_rm || "–");
      cell(tr, (it.code ? it.code + ": " : "") + (it.message || ""));
      const td = document.createElement("td");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = "Open";
      b.setAttribute("aria-label", "Open " + (it.name || it.txn_id));
      b.addEventListener("click", () => openItem(it.txn_id));
      td.appendChild(b);
      tr.appendChild(td);
      body.appendChild(tr);
    }
  }

  async function openItem(txn) {
    const d = await (await api("/v1/review/" + txn)).json();
    current = d;
    const meta = $("meta");
    meta.replaceChildren();
    const lines = [
      "State: " + d.state,
      "Score: " + (d.assessment ? d.assessment.score.toFixed(2) + " (auto-process needs " + d.assessment.threshold.toFixed(2) + ")" : "n/a"),
      d.review ? "Held because: " + d.review.code + " – " + d.review.message : "",
      d.assessment && d.assessment.reasons.length ? "Reasons: " + d.assessment.reasons.join("; ") : "",
      d.assessment ? "Components: " + Object.entries(d.assessment.components).map(([k, v]) => k + " " + v).join(", ") : "",
      d.reviewed_by ? "Reviewed by: " + d.reviewed_by : "",
    ].filter(Boolean);
    for (const l of lines) { const p = document.createElement("p"); p.textContent = l; meta.appendChild(p); }
    $("extraction").value = d.extraction ? JSON.stringify(d.extraction, null, 2) : "";
    $("doctext").textContent = d.text + (d.text_truncated ? "\n… (truncated)" : "");
    $("attest").checked = false;
    $("note").value = "";
    const reviewable = d.state === "REVIEW" && d.review && d.review.resolvable;
    $("review-actions").hidden = d.state !== "REVIEW";
    $("approve").disabled = !reviewable;
    $("approve").title = reviewable ? "" : "This hold cannot be cleared by editing the extraction. It can only be rejected.";
    $("checker-actions").hidden = !(d.state === "AWAITING_APPROVAL" || d.state === "SUBMITTED");
    $("checker-note").textContent = d.needs_second_approval
      ? "A person edited this extraction, so a different person must approve the filing."
      : "The duty is above the single-approver limit.";
    $("detail").hidden = false;
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
      say("Saved. The document is now: " + r.state + (r.halt ? " (" + r.halt.code + ")" : ""), false);
      $("detail").hidden = true;
      await loadQueue();
    } catch (e) { say("Could not save: " + e.message, true); }
  }

  async function approveFiling() {
    if (!current) return;
    try {
      const r = await (await api("/v1/approvals/" + current.txn_id, { method: "POST" })).json();
      say("Approved. The document is now: " + r.state + (r.halt ? " (" + r.halt.code + ")" : ""), false);
      $("detail").hidden = true;
      await loadQueue();
    } catch (e) { say("Could not approve: " + e.message, true); }
  }

  async function viewPdf() {
    if (!current) return;
    try {
      const blob = await (await api("/v1/review/" + current.txn_id + "/document")).blob();
      window.open(URL.createObjectURL(blob), "_blank", "noopener");
    } catch (e) { say("The original is no longer available: " + e.message, true); }
  }

  function b64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] || "");
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
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
      const out = $("batch-result");
      out.replaceChildren();
      const p = document.createElement("p");
      p.textContent = "Batch " + r.batch_id + ": " + r.accepted + " accepted, " + r.rejected_at_intake + " rejected at intake. Processing has started; refresh the queue in a moment.";
      out.appendChild(p);
      say("Batch submitted.", false);
    } catch (e) { say("Upload failed: " + e.message, true); }
  }

  // ---- dashboard, register ------------------------------------------------------------------
  function showTab(name) {
    for (const t of ["work", "dashboard", "clients", "register"]) $("tab-" + t).hidden = t !== name;
    for (const b of document.querySelectorAll(".tab")) b.setAttribute("aria-pressed", String(b.dataset.tab === name));
    if (!token) return;
    if (name === "dashboard") loadDashboard().catch((e) => say(e.message, true));
    if (name === "clients") loadClients().catch((e) => say(e.message, true));
    if (name === "register") loadRegister().catch((e) => say(e.message, true));
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
      if (c.status !== "ACTIVE") td.appendChild(btn(c.status === "SUSPENDED" ? "Reactivate" : "Approve", () => approveClient(c), "Approve " + d.legal_name));
      else td.appendChild(btn("Suspend", () => suspendClient(c), "Suspend " + d.legal_name, true));
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

  for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => showTab(b.dataset.tab));
  $("dash-refresh").addEventListener("click", () => loadDashboard().catch((e) => say(e.message, true)));
  $("rep-run").addEventListener("click", runReport);
  $("reg-go").addEventListener("click", () => loadRegister().catch((e) => say(e.message, true)));
  $("reg-csv").addEventListener("click", async () => {
    try { csvDownload("stsds-register.csv", await (await api("/v1/register.csv")).blob()); }
    catch (e) { say("Export needs the checker role: " + e.message, true); }
  });
  $("cl-go").addEventListener("click", () => loadClients().catch((e) => say(e.message, true)));
  $("c-save").addEventListener("click", saveClient);
  $("c-cancel").addEventListener("click", resetClientForm);
  $("a-add").addEventListener("click", addContract);
  $("a-import").addEventListener("click", importCsv);

  $("signin").addEventListener("click", async () => {
    token = $("token").value.trim();
    $("token").value = "";
    try { await loadQueue(); say("Signed in.", false); }
    catch (e) { token = ""; say("Sign-in failed: " + e.message, true); }
  });
  $("approve").addEventListener("click", () => decide("approve"));
  $("reject").addEventListener("click", () => decide("reject"));
  $("approve-filing").addEventListener("click", approveFiling);
  $("viewpdf").addEventListener("click", viewPdf);
  $("upload").addEventListener("click", upload);
})();

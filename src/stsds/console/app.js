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

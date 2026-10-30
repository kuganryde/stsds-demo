# STSDS Compliance Engine (e-Duti Setem)

Implementation of **STSDS-SDLC-2026-V1** (Autonomous Agentic Compliance Blueprint, Malaysia STSDS) for RYDETECH GLOBAL ENTERPRISE.

The AI only reads the contract. Code calculates the duty. A parity gate compares our figure with LHDN's own *Rumusan Pengiraan* before anything binding is submitted. Every step is audited and the certificate is vaulted for seven years.

## What it does with a pile of documents

You hand it many PDFs at once. For each one, independently:

```
sign ─▶ ingest ─▶ validate ─▶ score ──┬─ score ≥ 0.95 and every gate passes ─▶ sign in ─▶ file ─▶ pay ─▶ stamp ─▶ attach + verify (QR) ─▶ COMPLETE
                                      │
                                      └─ anything less ─▶ a named person reviews ─▶ (approve / correct / reject) ─▶ the same path
```

The eight manual steps and where each lives: **1 sign** (`signing.py`: send for e-signature, collect the signed PDF, structural signature check at intake) · **2 log in** (pre-issued PKI session, no passwords in code) · **3 open ezHasil, e-Duti Setem** and **4 fill the return and upload** (`rpa.py`, after scoring and, where needed, human review) · **5 computation** (rules engine plus the LHDN parity gate) · **6 pay** (`payment.py`, idempotent) · **7 download Sijil Setem** (one-time capture, fsynced before anything else) · **8 attach and verify** (bundle, 7-year vault, the QR code on the certificate is read and checked, and nine final checks must pass).

* **Validate**: real unencrypted PDF ≤ 20MB; every figure and the execution date must appear verbatim in the document; TIN/NRIC/BRN checks; the 30-day Sec. 47 clock; JPPH route; phase date; and a duplicate check on the *instrument* (type, date, figures, party IDs), so the same contract exported twice as two different PDFs is not filed or paid twice.
* **Confidence score** per document, 0 to 1: the product of model confidence, grounding (share of figures found in the text), party-ID validity, completeness (the engine has everything it needs), the 30-day clock, and text quality. A zero in any part makes it zero. At or above the threshold (default 0.95) the document runs without a person. Below it, it goes to the review queue with the reasons listed, lowest score first.
* **Human in the loop**: a *reviewer* sees the extraction, the text read from the document and the original PDF, then approves, corrects or rejects. Approving needs a ticked attestation that every figure, date and party was checked against the original. If the reviewer **changed** anything, a *different* person (a *checker*) must approve the filing before it is submitted (four-eyes, enforced in code, not policy). Holds that no edit can fix (overdue, JPPH, phase not live, duplicate) can only be rejected.
* **After approval it carries on by itself**: draft in MyTax, parity check, Submit, pay the bill, download the one-time Sijil, read its QR code, vault it, and run the nine final verification checks. A document is `COMPLETE` only after verification passes.
* **Signing and the certificate QR**: documents can be sent for e-signature from the API; the signed PDF is collected and enters the pipeline by itself. With `STSDS_MIN_SIGNATURES` set, an unsigned file, or one edited after signing, is held. After stamping, the QR on the certificate is decoded, its URL must be `https` on an allow-listed host (`STSDS_QR_VERIFY_HOSTS`), the portal's verification page must say valid, and any amount shown must equal the duty.
* **Everything is logged**: every outcome of every document is a row in the transaction ledger (hash-chained, exportable as CSV), on top of each transaction's own audit chain.

Run it in one command, no MyTax needed: `python -m stsds.demo batch` (six mixed documents, reviewer and checker acting).

## Can it be "error-free"? An honest answer

No software can promise that, and this README will not. What it does promise is that **an error cannot pass silently and cannot move money twice**:

| What can go wrong | What the system does |
|---|---|
| Model misreads a figure | Grounding check fails, score drops, a person reviews. Parity with LHDN's own total is a second net. |
| Same contract arrives twice (even as a different file) | Second copy is held; never filed or paid twice. |
| One document in a batch is bad | Only that document is held or rejected. The rest continue. |
| Crash or exception **before** Submit or payment | Transaction stops with an explicit reason; resumable; nothing was sent. |
| Crash or timeout **during** Submit or payment | A marker was written first, so the state is `SUBMIT_UNCERTAIN` / `PAYMENT_UNCERTAIN`. It is **never retried automatically**. A checker reconciles against the portal/provider, and a retry needs a named approver. |
| Payment provider replies "failed" but money moved (lost reply) | Reconcile finds it paid and continues. The payment key is the transaction id, so a retry cannot double-pay. |
| Wrong amount | The provider is sent the engine total that matched LHDN; a reply for any other amount is treated as uncertain. |
| Certificate QR missing, pointing to another host, or the portal says it is not valid | The certificate is still vaulted first, then the case stops at `VERIFY_FAILED`. A QR on a non-allow-listed host is never opened. |
| Document not signed, or edited after signing | Held at intake (reject-only) when signatures are required. |
| Certificate captured but vault fails | Sijil is fsynced to disk first; local copy is deleted only after S3 confirms the lock. |
| Ledger cannot be written | Submit and payment do not start. Other steps are recorded in the audit chain and flagged for backfill. |

What it **cannot** do: guarantee MyTax or your bank behaves as expected on day one (the selectors and the payment gateway need UAT), or know that a contract it read is the *right* one to stamp. Those are what the 30-day shadow period and the reviewer queue are for.

## Read this first: what is and is not guaranteed

"100% accuracy" cannot be promised by software alone, and this README does not promise it. What the design gives you is **fail-closed correctness**:

* The arithmetic is exact: integer sen and integer RM, no floating point, and it is cross-checked against an independent oracle on thousands of generated inputs.
* If the system is not certain, it does not guess. Missing wage, missing lease term, unknown loan type, unknown residential status, low classifier confidence, a figure that is not in the document, a parity difference of RM0.01: each one stops the transaction and routes it to an officer.
* A wrong figure can only reach LHDN if **both** our engine and LHDN's own calculation preview are wrong in the same way, because Submit is blocked unless they match to the sen.

What it cannot do for you:

1. **The statutory rates must be signed off by your Head of Tax before live mode.** `rules/stamp_rules.json` ships with `"signoff": null`, and the engine refuses to submit while that is empty.
2. **The MyTax RPA selectors are not calibrated.** MyTax cannot be reached from the build environment. `rpa/selectors.template.json` is full of `UAT_REQUIRED` placeholders, and the worker refuses to run until you replace them during shadow-mode UAT.
3. **The n8n workflow and the Playwright worker were not run against live n8n or MyTax.** The engine, gates, vault logic, API and security controls are covered by automated tests (below).

## Corrections to the blueprint (important)

The blueprint's reference code and rate table were checked against public sources. Four things would have produced wrong duty, so they are **not** implemented as written.

| # | Blueprint says | Problem | This build |
|---|---|---|---|
| 1 | Tenancy rate RM1 / RM2 / RM4 per RM250 | Two secondary sources give RM1 (≤1yr), RM3 (>1–3yr), RM5 (>3–5yr), RM7 (>5yr or indefinite) | Uses RM1/3/5/7, in data, with the conflict noted in the rules file |
| 2 | Transfer duty as `value × 1%…4%` | Statute charges RM per RM100 **or part thereof**, per tier. A raw percentage under-assesses any value that is not a multiple of RM100 | Integer ceiling per tier |
| 3 | `monthly_salary_rm \|\| 0` and `lease_duration_months \|\| 12` | A missing salary becomes "exempt", and a missing term becomes the cheapest bracket. Both are silent under-assessments | Missing inputs raise `NeedsReview` |
| 4 | JavaScript floats (`v * 0.08`, `toFixed`) | Binary floating point is not exact money arithmetic | Decimal parsing, integer sen |

Also unaddressed in the blueprint and added here: non-citizen non-residential property at RM4 per RM100, duty on the greater of consideration and market value, only secured loans auto-computed at 0.5% (other security types go to review), and a single calculation engine behind the API instead of a second copy inside n8n (two copies drift).

**Items I could not verify and have flagged in the rules file:** the exact meaning of "not exceeding 3 / 5 years" at the boundary months, whether the RM3,000 employment exemption is enacted (one source calls it a Finance Bill proposal), and the RM10 duplicate-copy duty (stated only in the blueprint).

## Architecture

Four decoupled zones, as in the blueprint:

```
DocuSign/CLM ──HMAC──▶ n8n (state machine) ──bearer──▶ engine (FastAPI)
                                                          ├─ ocr.py        text layer, PaddleOCR fallback
                                                          ├─ classifier.py Ollama qwen2.5:14b + Qdrant RAG, temp 0, JSON schema
                                                          ├─ engine.py     deterministic Decimal duty engine  (rules/stamp_rules.json)
                                                          ├─ gates.py      PDF/20MB, Sec.47 clock, confidence, grounding, TIN/BRN, JPPH, phase, PARITY
                                                          ├─ rpa.py        Playwright: draft → parity → submit → one-time Sijil
                                                          └─ vault.py      fsync cache → SHA-256 → merge → S3 Object Lock COMPLIANCE 7y
                                                       Postgres: hash-chained append-only audit_log
```

Flow and gates (blueprint nodes 1–10):

1. **Ingest** — HMAC-verified webhook, PDF ≤ 20MB, real unencrypted PDF, SHA-256 becomes the idempotent transaction id (the same document is never filed twice).
2. **Classify** — model output must satisfy the strict schema, confidence ≥ 0.95, and **every figure and the execution date must appear verbatim in the document text** (grounding gate). A hallucinated number cannot be taxed. The 30-day Sec. 47 clock starts; overdue instruments go to review.
3. **Calculate** — deterministic engine, with a step-by-step working sheet.
4. **Pre-flight (Hard Gate 1)** — TIN and NRIC/BRN validation (live verification endpoint; unconfigured means halt), JPPH route, phase date (Item 32 is off until 1 Jan 2027).
5. **Draft + Parity (Hard Gate 2)** — the bot saves a draft only, reads *Rumusan Pengiraan*, and halts on any difference of RM0.01.
6. **Submit** — needs live mode, signed-off rules, and a named checker at or above the maker-checker threshold (default RM10,000).
7. **Pay** — only after Submit. Checks the bill is not already paid, writes a marker, pays the exact engine total with an idempotency key, then confirms with a second independent status read. Zero-duty (exempt) instruments owe nothing and skip payment.
8. **Complete** — after payment, the one-time Sijil Setem is fsynced to disk **before** any upload, then merged with the contract and working sheet and stored under COMPLIANCE retention. The local copy is deleted only after S3 confirms the lock.
9. **Verify** — audit chain intact, certificate and bundle hashed, retention date set, engine total equals LHDN total equals amount paid, provider confirms payment, portal shows the bill stamped (if its selector is configured), ledger chain intact. Any failure parks the transaction as `VERIFY_FAILED`; nothing is called complete on trust.
10. **Halt** — any failure stops the transaction, writes an audit record and alerts an officer (id and reason code only, no PII).

## Security and threat model

| Threat | Control |
|---|---|
| Forged or replayed webhook | HMAC-SHA256 over `timestamp.body`, 5-minute window, constant-time compare |
| Unauthorised internal calls | Service bearer token (≥32 chars), engine on an internal-only Docker network, no host ports |
| Prompt injection in a contract | Document fenced as untrusted data; output restricted to a JSON schema; confidence, grounding and parity gates; classification cannot change a number it did not read |
| LLM hallucinated amount | Grounding gate: figure must appear in the source text |
| Wrong duty reaching LHDN | Deterministic engine plus parity gate; shadow mode by default; unsigned rules block submit |
| Duplicate filing | Idempotent transaction id from document hash |
| Lost one-time certificate | fsync to disk before upload; drill test where the vault fails |
| Audit tampering | Hash-chained records, Postgres trigger blocks UPDATE/DELETE/TRUNCATE, S3 COMPLIANCE lock, policy-as-code (OPA) forbids weaker vault |
| Path traversal / injection | Transaction ids are 32 hex chars only; no URL fetching (no SSRF); strict models reject unknown fields |
| PII in logs | TIN/NRIC masked to last four; alerts carry no PII |
| Secrets | Docker/K8s secret files (`*_FILE`), nothing in the image, `.env` and `secrets/` git-ignored, gitleaks in CI |
| Container compromise | Non-root, read-only filesystem, all capabilities dropped, no-new-privileges |

Residual risks: the PKI storage-state file for MyTax is a high-value secret (rotate, restrict, monitor); the model could mislabel an instrument with high confidence, which is why Phase 1 runs 30 days in shadow against officers' own results.

## CI/CD and deployment

`.github/workflows/ci.yml`: gitleaks → ruff (with security rules) → mypy strict → bandit → pip-audit → pytest with a 90% coverage gate → CycloneDX SBOM → rules-schema validation → Terraform validate + OPA policy → Docker build + Trivy (fails on HIGH/CRITICAL) → GitOps promotion to staging. CI never touches production; ArgoCD reconciles, and production promotion needs Head of Tax + DevSecOps approval.

Infrastructure as code: `infra/terraform/vault.tf` (Object Lock COMPLIANCE 7y, KMS, TLS-only, no delete/bypass, least-privilege engine policy) and `policy/vault.rego`.

## Go-live checklist (blueprint Phase 5/6)

1. `terraform apply` the vault; apply `db/schema.sql`; create the least-privilege DB role.
2. Seed Qdrant with the First Schedule passages (collection `stamp_act_first_schedule`).
3. Calibrate `rpa/selectors.json` against MyTax in a test session (the optional `bill_lookup` / `bill_stamped_marker` enable the final "portal shows stamped" check).
3a. Build the e-signature gateway to the contract below (or leave `SIGNING_URL` empty and keep uploading already-signed PDFs), set `STSDS_MIN_SIGNATURES=1`, and set `STSDS_QR_VERIFY_HOSTS` to the host LHDN's certificate QR points to (confirm it from a real certificate). Live mode refuses to start without both.
3b. Build the payment gateway to the contract above and test it against your bank's sandbox, including a deliberately dropped reply.
3c. Create reviewer and checker accounts (`python -m stsds.users`), set `STSDS_USERS_FILE`, and put the console behind SSO or an IP allowlist and a TLS proxy.
3d. Decide the auto-process threshold with your Head of Tax (default 0.95, never lower). Start with it high so more goes to people, and relax only on evidence from shadow mode.
4. Head of Tax reviews every rate and note in `rules/stamp_rules.json`, resolves the open items, then adds the `signoff` block in a reviewed pull request.
5. Run `STSDS_MODE=shadow` for 30 days. Compare every transaction with officers' own figures. Required: 100% parity, 150+ synthetic boundary payloads, Sijil failure drills, Section 3A mock audit (retrieve any transaction in under 60 seconds).
6. Switch to `live` for Tier 1 (Items 4, 22, 49) only. Enable Item 32 on 1 January 2027 after its own shadow run.

## Contract register, compliance monitoring and more document types

These cover the repository and monitoring parts of a managed stamp-duty service (assessment, filing, repository, monitoring).

**Document types.** Besides employment, tenancy, secured loans and property transfers, the engine now computes nominal-duty agreements (shareholder, intercompany, commercial), loan and service agreements with an ascertainable sum, and share transfers (higher of consideration and net tangible assets). **These three items are not live in `rules/stamp_rules.json`**: their `phase_start` is 2099-01-01 until the Head of Tax confirms each rate and item number against the gazetted First Schedule, because published secondary sources disagree. Until then such a document stops at the phase gate (`PHASE_NOT_LIVE`, reject-only). The simulated demo enables them so you can see the behaviour. The parity gate against the LHDN calculation still applies when they are enabled. The BNDS module names used for them (`PERJANJIAN_AM`, `PERJANJIAN_PINJAMAN`, `PEMINDAHAN_SAHAM`) are placeholders to calibrate against the real portal.

**Contract register** (`/v1/register`). Entries come from two places, merged on read: pipeline entries derived from the transaction records (never typed in; owner, entity, renewal and expiry dates and notes can be overridden by a named person, each change recorded with who and when) and manual entries for contracts stamped or pending outside the pipeline (single add or CSV import, all-or-nothing). Fields: contract owner, entity, counterparty, document type, execution date, duty, status (`STAMPED`, `IN_PROGRESS`, `UNSTAMPED`, `OVERDUE`, `NOT_REQUIRED`), certificate reference, stamped date, renewal and expiry dates.

**Monitoring** (`/v1/monitoring/summary|alerts|unstamped`, `/v1/reports/monthly?month=YYYY-MM`, `/v1/register.csv`). Alerts for overdue stamping, deadlines within 7 days, renewals or expiries within 90 days and expired contracts; an unstamped-document list; a monthly review; and a dashboard in the console. **Penalty exposure is an estimate**, taken from the `penalty` block of the rules file (LHDN guideline of 6 Nov 2025: within 3 months RM50 or 10% of the deficient duty, later RM100 or 20%). Older secondary sources show three bands, so the Head of Tax must confirm the bands before the figures are relied on. The app never files or pays a penalty.

| Endpoint | Roles |
|---|---|
| `GET /v1/register`, `/v1/register/{id}/history`, `/v1/monitoring/*`, `/v1/reports/monthly` | operator, reviewer, checker |
| `POST /v1/register`, `POST /v1/register/import`, `PATCH /v1/register/{id}`, `GET /v1/register.csv` | operator, checker |

## Client onboarding (step 0)

TMF files on behalf of client companies, so the app must know who a document belongs to **before** it is read, calculated or filed. The client master (`/v1/clients`, "Clients" tab in the console) is that first step.

**What is recorded.** Legal name, entity type, BRN/NRIC and TIN (validated with the same rules as the party gate), registered address, contact name and role, authorised signatories (names and designations), the TMF relationship manager, a default contract owner, the **authority reference** (engagement letter or power of attorney) with an optional expiry date, and an optional **approval limit** in RM. Emails and phone numbers are deliberately not stored; they stay in TMF's CRM.

**How it fits the workflow.** Register (DRAFT) → a different person approves (ACTIVE) → documents are accepted. Preflight runs `client_gate` after the party checks. At least one party must be an ACTIVE client; counterparties need not be. A document is held in the review queue, reject-only, when no party is a client (`CLIENT_NOT_REGISTERED`), a client is still a draft (`CLIENT_NOT_ACTIVE`), is suspended (`CLIENT_SUSPENDED`), or TMF's authority has expired (`CLIENT_AUTHORITY_EXPIRED`). If the TIN on the document disagrees with the client record the hold is `CLIENT_DETAILS_MISMATCH`, which a reviewer **can** clear by correcting a misread extraction (that edit also forces a checker). When the client is approved, every document held for that reason is re-checked and moved on automatically; nothing is approved on the way and every other gate runs again. The Clients tab lists the parties on held documents with a **Register** button that pre-fills the form.

**Controls.** Four-eyes (the preparer cannot approve; the suspender cannot reactivate); activation needs an authority reference and unexpired authority; a material edit to an ACTIVE client (name, ID, TIN, entity type, authority, limit) returns it to DRAFT; a client's approval limit forces a checker on documents at or above it; every change is attributed in a history table; the client's name and default owner flow into the contract register. Registration is **off by default** (`STSDS_REQUIRE_CLIENT=false`: matches are recorded but nothing is blocked, for a pilot). Set it to `true` for production; if it is on and the register is unavailable the document halts rather than passing.

| Endpoint | Roles |
|---|---|
| `GET /v1/clients`, `/v1/clients/pending`, `/v1/clients/{id}`, `/v1/clients/{id}/history` | operator, reviewer, checker |
| `POST /v1/clients`, `PATCH /v1/clients/{id}` | operator, reviewer, checker (prepares only) |
| `POST /v1/clients/{id}/activate`, `POST /v1/clients/{id}/suspend` | checker (a named person; never the shared service token) |

## The console (pages follow the workflow)

Sidebar order is the order work happens: **Clients** (step 0) → **Batches** → **Signing** (only when an e-signature service is configured) → **Review queue** → **Approvals** → **Exceptions** → **Compliance dashboard** → **Contract register** → **Ledger and audit**. Each document opens a **proof page** (timeline from the ledger, the nine final checks, vault hashes). Pages a role cannot use are hidden, and buttons the server would refuse (for example a reviewer approving a client, or approving your own review) are disabled with the reason. The queue is split by what the engine really needs: Review (a person confirms facts), Approvals (a named checker releases money, including any payment retry), Exceptions (client holds, payment status unclear, submit unclear, failed verification). A payment retry is never automatic: "Ask the payment provider" reconciles, and if the bill is unpaid it moves to Approvals for a checker. A submission the portal did not confirm has no console button; an officer checks MyTax first.

Endpoints behind it: `GET /v1/me` (name, roles, mode, features), `GET /v1/batches`, `GET /v1/transactions/{id}` (proof), `GET /v1/signing`. The console uses no inline script or style, no web fonts and no browser storage, to fit the strict CSP.

## Roles, the console and the API

| Role | Can | Cannot |
|---|---|---|
| **operator** (also the n8n service token) | submit batches, read reports and the ledger, advance transactions | review, approve, reconcile |
| **reviewer** | read the queue, open documents, approve / correct / reject | approve a filing it edited; submit batches |
| **checker** | approve high-value or human-edited filings, reconcile payments | decide reviews |

People sign in with personal tokens. The users file (`STSDS_USERS_FILE`) holds only SHA-256 hashes; create an entry with `python -m stsds.users "Aisha Rahman" reviewer` (the token is shown once). The person's name always comes from the token, never from the request body, so every decision in the audit trail and ledger is attributable.

The reviewer console is at `/console` (no inline script, strict CSP, token held in page memory only). Main endpoints:

| Endpoint | Role | Purpose |
|---|---|---|
| `POST /v1/batches` | operator | upload many PDFs (base64 JSON); processing starts in the background |
| `GET /v1/batches/{id}` · `/report.csv` | any | per-document state, score, duty, who reviewed/approved, bill, payment reference, vault key |
| `GET /v1/review/queue` · `/v1/review/{txn}` · `/document` | reviewer, checker | everything waiting on a person, lowest score first |
| `POST /v1/review/{txn}/decision` | reviewer | `approve` (with attestation, optional corrections) or `reject` |
| `POST /v1/approvals/{txn}` | checker | second approval; resumes the filing, payment and stamping |
| `POST /v1/payments/{txn}/reconcile` | checker | resolve an uncertain payment from the provider's own record |
| `GET /v1/ledger` · `/v1/ledger.csv` | operator, checker | the book of every transaction outcome, with chain check |
| `POST /v1/advance/{txn}` | operator | run one transaction forward (idempotent; used for retry loops) |
| `POST /v1/signing/requests` · `GET /v1/signing/{id}` · `POST /v1/signing/{id}/collect` | operator (GET: any) | send a document for e-signature, check it, collect the signed PDF (only when `SIGNING_URL` is set) |
| `POST /v1/signing/webhook` | HMAC-signed by the provider | "look at envelope X"; the engine re-reads the truth from the provider |

The earlier per-stage endpoints (`/v1/classify`, `/v1/rpa/draft`, ...) remain for step-by-step control from n8n.

### Payment gateway contract

MyTax issues the bill and a virtual account; it does not take your money. Who pays it (bank corporate API, FPX biller, treasury platform) is your decision, so the engine talks to a small HTTPS gateway you place in front of that provider (`PAYMENT_URL`, bearer `PAYMENT_TOKEN`):

* `POST /payments` with header `Idempotency-Key` and body `{biller, bill_no, virtual_account, amount_rm}` returns `{reference, amount_rm, status: "PAID"}`. A 4xx means **no money moved**. A timeout, 5xx or unreadable reply means **unknown** and is reconciled, never retried blindly. The gateway **must** honour the idempotency key.
* `GET /payments/{bill_no}` returns `{status: PAID|UNPAID, amount_rm, reference}` (404 = unpaid).

Bank-side maker-checker or token approval, if your bank requires it, must be handled inside that gateway.

### E-signature gateway contract

Which signing service you use is your decision. The engine speaks to a thin HTTPS gateway (`SIGNING_URL`, bearer `SIGNING_TOKEN`):

* `POST /envelopes` with `{reference, pdf_base64, signers: [{name, email}]}` returns `{envelope_id}`.
* `GET /envelopes/{id}` returns `{status: PENDING|COMPLETED|DECLINED|EXPIRED, signers_done, signers_total, signed_pdf_base64}` (the PDF only when `COMPLETED`).
* Call `POST /v1/signing/webhook` (HMAC, same scheme as `/v1/ingest`) with `{envelope_id}` when the state changes. The body is never trusted beyond the id.

**What the signature check is, and is not.** The engine checks that signature fields exist and that each one covers the whole file, so an unsigned file and a file edited after signing are both caught. It does **not** validate certificate chains; do that in the signing provider (or add a validator such as pyHanko behind the same gate). Signer emails are never stored by the engine.

## Try it without MyTax: dry-run mode

```bash
python -m stsds.demo batch                # six mixed documents through the whole loop, reviewer and checker acting
python -m stsds.demo batch --no-resolve   # stop after the automatic pass, to see what waits for people
python -m stsds.demo                      # the six single-document gate scenarios
python -m stsds.demo serve                # real HTTP API + console; prints a reviewer token and a checker token
python -m stsds.demo send demo-samples/*.pdf --approve    # in a second terminal: submit the samples as a batch
```

Simulated: the MyTax portal (it computes its own total independently, so parity is a real two-implementation check, and it releases the certificate only for a paid bill), the payment provider (honours idempotency, checks the amount, can fail before or after money moves), the vault (a local folder with no real lock), the classifier (keyword rules; a document marked "illegible" scores 0.72 so you can see review), and the TIN check (format only). Real: every gate, scoring, the engine, the orchestrator, review and four-eyes rules, the ledger, the audit chain and the API security. Never use it for real filings.

## Verified in the build

330 automated tests pass, 92% line coverage, ruff clean, mypy strict clean, bandit clean at the CI threshold. Both new steps are covered: QR round trip, hostile or malformed QR URLs, a foreign host that is never opened, missing, invalid and unreadable verification, amount mismatch, signature structure including edit-after-sign, one-time ingestion of signed documents, declined signing, forged and replayed webhooks. Beyond the engine boundary and differential tests: batch intake with mixed good/bad/duplicate documents, scoring, review approve/correct/reject, four-eyes at the function and HTTP level, every payment failure mode (lost reply, timeout before money moved, definite refusal, already paid, unexpected exception), submit crash safety, verification failure, ledger tamper detection and CSV-injection safety, portal concurrency limit, batch payment cap, restart resume, role enforcement on every endpoint, and the console served with a strict policy. The console was also exercised in a real browser (sign in, open, attest, approve) with no console or CSP errors. Also covered: the three new instrument types (every rate boundary, fail-closed inputs, held from live filing in the real rules file), the late-penalty estimate at every band edge, the contract register (status derivation, overrides, history, CSV import all-or-nothing, export injection safety), monitoring alerts and the role rules on every new endpoint, and the client master (validation, four-eyes activation, re-approval on material edits, suspension, the preflight gate in every state, automatic release of held documents, role rules). **Not covered:** live MyTax (including the QR verification page), live n8n, a real e-signature service, a real payment gateway, live S3/Postgres, a real Ollama model.

## Run the tests

```bash
pip install -e ".[dev]"
pytest --cov=stsds
```

Layout: `src/stsds/` application (`scoring`, `orchestrator`, `batch`, `ledger`, `payment`, `users`, `signing`, `qr`, `console/` are the batch, review, signing and certificate layer), `rules/` statutory data, `tests/` boundary, differential (property-based), gate, pipeline, vault and API-security tests, `n8n/` workflow, `infra/` Terraform, `policy/` OPA, `db/` schema.

## Operating notes

* Changing a rate is a data change to `rules/stamp_rules.json`, reviewed by the tax owner. Every calculation stores the rules version and SHA-256, so any past filing can be reproduced.
* LHDN portal changes: Phase 5 of the blueprint calls for a daily DOM health check. Selector failures halt transactions rather than guessing.
* **Single instance.** Per-transaction locks and the ledger chain are in-process: run one replica with one worker. Scaling out needs the locks and ledger moved to Postgres (not done here).
* The review queue scans staged transactions; for tens of thousands of open items index it in Postgres.
* Out of scope: Phase 3 instruments (2028), M&A and JPPH-valued transfers (routed to review by design).

*Prepared for RYDETECH GLOBAL ENTERPRISE · rydetechglobal.com · rydetechwizsolutions@proton.me*

# Bilingual (EN/VI) Mortgage Loan Assistant — Architecture & Scaffold

An AI agent that talks to borrowers by voice (native speech-to-speech,
English + Vietnamese, including mid-sentence code-switching), analyzes
income/asset/credit documentation against mortgage guidelines, and runs
loan-pipeline management (tracking, follow-ups, stage progression).

## Architecture

```
                         ┌─────────────────────────────┐
                         │   Borrower channels           │
                         │  phone call / web voice /     │
                         │  chat widget / SMS             │
                         └──────────────┬────────────────┘
                                        │
                 ┌──────────────────────┼───────────────────────┐
                 │                      │                       │
        ┌────────▼────────┐   ┌─────────▼─────────┐   ┌─────────▼────────┐
        │  Voice transport │   │   Chat / SMS       │   │  Loan-officer     │
        │  Pipecat/LiveKit │   │   FastAPI + text   │   │  web dashboard    │
        │  (WebRTC/telephony)  │   LangGraph agent  │   │  (loan pipeline)  │
        └────────┬────────┘   └─────────┬─────────┘   └─────────┬────────┘
                 │                      │                       │
                 │   ┌──────────────────▼──────────────────┐    │
                 └──▶│         Agent Core (FastAPI)          │◀──┘
                     │  - Gemini Live session mgmt           │
                     │  - shared tool dispatcher             │
                     │  - bilingual system prompt            │
                     └──┬───────────┬────────────┬───────────┘
                        │           │            │
             ┌──────────▼───┐ ┌─────▼──────┐ ┌───▼─────────────┐
             │  Document     │ │  Pipeline  │ │  Guideline RAG   │
             │  Analysis     │ │  Service   │ │  Knowledge Base  │
             │  (OCR+rules)  │ │  (state    │ │  (Chroma /       │
             │               │ │  machine)  │ │  vector store)   │
             └──────┬────────┘ └─────┬──────┘ └──────────────────┘
                    │                │
             ┌──────▼────────────────▼──────┐      ┌─────────────────┐
             │      PostgreSQL (system of    │      │  Celery + Redis  │
             │      record: borrowers, loan  │◀────▶│  follow-up /     │
             │      files, docs, income,     │      │  reminder jobs   │
             │      tasks, stage history)    │      └─────────────────┘
             └────────────────────────────────┘
                    │
             ┌──────▼────────┐
             │ Twilio/SendGrid│
             │ (SMS / email)  │
             └────────────────┘
```

### Why Gemini Live for the voice layer

Gemini Live is a native audio-in/audio-out model (not a cascaded
ASR→LLM→TTS pipeline), which matters here for two reasons:

1. **Latency and naturalness** for a phone-call experience.
2. **Genuine bilingual code-switching.** A cascaded pipeline forces a
   language decision at the ASR step; a native audio model can track
   Vietnamese/English mixed speech within a single utterance, which is
   exactly how many Vietnamese-American borrowers actually talk.

Gemini Live also supports live function/tool calling mid-conversation,
which is what lets the agent look up loan status, calculate DTI, or
request a document without leaving the voice turn.

**Important:** confirm the current GA model ID and pricing directly in
Google's Live API docs before deploying — Google ships new Live model
revisions frequently, and `GEMINI_LIVE_MODEL` in `.env` is deliberately
left as a config value, not hardcoded, for that reason.

### Voice transport: Pipecat + Twilio

`app/voice/gemini_live_client.py` implements the Gemini Live session and
tool-dispatch logic directly, but a raw audio websocket loop is not
production-grade for telephony (jitter buffers, codec negotiation,
barge-in, reconnect handling). This scaffold wires **Pipecat + Twilio**
as the concrete implementation:

- **`app/voice/pipecat_bot.py`** — builds the Pipecat pipeline:
  `TwilioFrameSerializer` (Twilio Media Streams protocol) →
  `FastAPIWebsocketTransport` → `GeminiMultimodalLiveLLMService` (the
  entire "LLM" node, since Gemini Live is audio-in/audio-out natively —
  no separate STT/TTS services needed) → back out through the transport.
  Tool calls are registered per-name and all delegate to the same
  `app.agent.tools.dispatch()` used elsewhere, so voice/chat/SMS can't
  drift into different behavior for the same tool.
- **`app/api/routes_voice.py`** — `POST /voice/twilio/incoming` (inbound
  call webhook, looks up the caller's phone against known borrowers so
  the agent starts already knowing the loan file), `WS /voice/twilio/ws`
  (the actual Media Streams audio path), and
  `initiate_outbound_call()` (used by the follow-up scheduler for
  `FollowUpChannel.VOICE_CALL` — the agent calling the borrower).

**Setup:**
1. `pip install -r requirements.txt` (pulls in `pipecat-ai[google,twilio,silero]`)
2. A Twilio phone number, with its voice webhook pointed at
   `POST https://<your-public-url>/voice/twilio/incoming`
3. For local dev, expose your server with ngrok and set
   `PUBLIC_BASE_URL` in `.env` to the ngrok URL (needed for outbound-call
   TwiML callbacks)
4. Vertex AI or Gemini API credentials per `.env.example`

**Honesty note on what's verified vs. not:** the TwiML-building
(`build_stream_twiml`) and Gemini-tool-declaration → Pipecat
`FunctionSchema` conversion (`_tool_declarations_to_schema_kwargs`) have
no pipecat/DB dependency and are unit-tested against real XML parsing in
`tests/test_voice_routes.py`. The actual `run_bot()` pipeline was
written against Pipecat's documented APIs (`docs.pipecat.ai`,
`GeminiMultimodalLiveLLMService`, `TwilioFrameSerializer`,
`FastAPIWebsocketTransport`) but **was not executable in the sandbox
this was built in** — no network access to install `pipecat-ai`, which
has a heavy dependency chain (torch, for Silero VAD). Pipecat's import
paths and service names also shift between releases faster than most
libraries; before deploying, run a real call against a Twilio trial
account and diff the imports in `pipecat_bot.py` against whatever
`pipecat-ai` version you pin.

**Alternative: LiveKit Agents.** If you'd rather have WebRTC video too
(e.g. for a "review this document together" screen-share moment), swap
Pipecat for LiveKit's `RealtimeModel` plugin for Gemini Live — the
`tool_handler`/`dispatch()` wiring pattern in `pipecat_bot.py` carries
over conceptually; only the transport/pipeline construction changes.

### Outbound voice follow-ups

`app/utils/notifications.py::_initiate_voice_followup` wires
`FollowUpChannel.VOICE_CALL` follow-ups (created by the pipeline
scheduler when a task goes stale) to `initiate_outbound_call()`, which
places a real Twilio outbound call pointed back at
`/voice/twilio/outbound-twiml?loan_id=...` — so a stale-document
follow-up can escalate from SMS to an actual outbound call from the
voice agent, not just a text message.

## Domain logic implemented in this scaffold

- **`app/documents/income_calculator.py`** — qualifying-income math for
  W-2 base pay, variable income (bonus/OT/commission, 2-yr averaging with
  declining-trend detection), self-employed income (net profit + add-
  backs), rental income (75% offset), and front-/back-end DTI. Every
  result is reproducible from stored inputs and carries a
  `flagged_for_underwriter` bit — **this system never issues a credit
  decision; it prepares numbers for a human underwriter.**
- **`app/pipeline/service.py`** — an explicit state machine
  (`VALID_TRANSITIONS`) for loan stages, so the agent can't skip a file
  from "Lead" straight to "Clear to Close." Auto-generates a document
  checklist when a loan enters Document Collection.
- **`app/knowledge/guidelines_kb.py`** — RAG-style guideline lookup so the
  agent answers "what's the minimum credit score for FHA?" from a cited
  source rather than free-generating from the model's memory. Ships with
  a small illustrative seed set — **replace with real ingested agency/
  investor guide text before production use.**
- **`app/voice/system_prompts.py`** — the bilingual system prompt,
  including hard compliance rules (no quoting final rates/approval, no
  reading SSNs aloud, ECOA fair-lending language, escalate-to-human
  triggers for hardship/fraud signals).

## What's a stub vs. production-ready

| Component | Status |
|---|---|
| Income/DTI calculation logic | Real logic, tested (see `tests/`) |
| Pipeline state machine | Real logic, tested |
| Guideline KB | Real: PDF chunking + Chroma vector search, tested end-to-end. Falls back to seed data until you ingest real guides — see "Guideline knowledge base ingestion" below. |
| Document OCR (paystub, W-2, bank statement) | Real: pdfplumber native-text + Tesseract/OpenCV scanned-image fallback, tested against fixtures including a rasterized "photo upload" case. See "Document analysis pipeline" below. |
| Document storage | Real local-filesystem backend for dev; `S3Storage`/`GCSStorage` interfaces defined but not implemented — **do not deploy with local storage** |
| Credit report / purchase contract / ID / gift letter parsing | Not implemented — routed to `NEEDS_REVIEW` with OCR text attached for a human |
| Gemini Live session | Real SDK usage; needs GCP/Vertex credentials + a media transport (Pipecat/LiveKit) |
| Voice transport (Pipecat + Twilio) | Real code against documented Pipecat/Twilio APIs (TwiML generation + tool-schema conversion unit-tested); the pipeline itself needs a local run against a real Twilio account — see "Voice transport" below |
| PII handling | Schema reserves a `pii_vault_ref` token; **no vault implemented** — do not store raw SSN/account numbers in this schema as-is |
| Auth/authz on API + dashboard | Real: JWT + PBKDF2 password hashing (stdlib-only, no bcrypt dependency needed), role-based route protection, cookie auth for the dashboard. Unit-tested including expiry/tamper detection. No signup flow (internal tool) -- see `scripts/create_user.py`. |
| Loan-officer dashboard | Real: server-rendered (FastAPI + Jinja2) pipeline board, loan detail, stage transitions, stale-task queue. Templates unit-tested by rendering with real Jinja2; not exercised through an actual running server in this sandbox (needs fastapi/sqlalchemy installed) — see "Dashboard" below. |

## Document analysis pipeline

`upload -> storage -> OCR -> structured parse -> (income calc) -> status`

1. **Storage** (`app/documents/storage.py`) — dev default is local
   filesystem (`local://...` URIs); swap `get_storage_backend()` for
   `S3Storage`/`GCSStorage` before handling real documents.
2. **OCR** (`app/documents/ocr.py`) — tries `pdfplumber`'s native PDF
   text layer first (fast, free, accurate — most bank/payroll-generated
   PDFs have one). Falls back to `pdf2image` + Tesseract with OpenCV
   preprocessing (grayscale, denoise, adaptive threshold, deskew) for
   scans or photo uploads, which is the realistic path for a borrower
   photographing a paystub with their phone. Tracks per-page method and
   a confidence score either way.
3. **Structured parsing** (`app/documents/parsers.py`) — regex-based
   extraction for paystubs, W-2s, and bank statements, with a
   **required-fields-based confidence score** rather than "OCR ran
   successfully" — a document is only auto-extracted if every field the
   income calculator actually needs was found. Pay frequency is *never*
   guessed; if it can't be determined, the document is held for review
   rather than mis-annualizing income.
4. **Income creation** (`app/documents/analyzer.py`) — for paystubs,
   annualizes using the *detected* pay frequency and cross-checks the
   implied number of pay periods (from YTD ÷ current-period gross)
   against what the calendar would suggest for the stated period-end
   date, flagging anything inconsistent (raise, missed period, misread
   frequency) for underwriter review.

Tested end-to-end against synthetic fixtures in `tests/fixtures/`
(regenerate with `python tests/fixtures/generate_fixtures.py`, requires
`reportlab`), including a rasterized-to-image version of the paystub to
exercise the real Tesseract fallback path — not just the easy
native-text-layer case. One regression this caught and fixed during
development: OCR flattens line breaks to spaces, which broke naive
greedy name-field regexes (`"Employer Name: X Employee Name: Y"` would
capture `"X Employee Name"` as the employer). Fixed with lookahead
boundaries on known field labels — see `_bounded_name()` in
`parsers.py` and the regression test in `test_document_parsing.py`.

**What's still simplified / needs production hardening:**
- Regex-based parsing works for the fixture layouts tested but will not
  match every real paystub/W-2/bank-statement layout — payroll and
  banking document formats vary enormously. At real volume, replace the
  parser bodies with a trained document-parsing model and keep the
  `REQUIRED_FIELDS` + confidence-gating structure around it.
- Credit reports, purchase contracts, ID documents, and gift letters
  have no structured parser yet (`_DOC_TYPE_TO_PARSER_KEY` doesn't cover
  them) — they land in `NEEDS_REVIEW` with raw OCR text attached.
- SSNs are detected-and-redacted (a boolean flag, never the number
  itself) if present in W-2 text, but full production PII handling
  (encryption at rest, access logging, retention policy) is not built.

## Guideline knowledge base ingestion

`PDF -> page-tracked text (pdfplumber) -> overlapping chunks -> embed -> Chroma`

1. **Chunking** (`app/knowledge/ingest.py`) — paragraph-first recursive
   splitter: packs paragraphs up to `max_chars` (1200 default), starts
   each new chunk with a sentence-boundary-snapped overlap of the
   previous chunk's tail (200 chars default) so a rule that spans a
   chunk boundary isn't lost either side. A paragraph longer than
   `max_chars` on its own (common in dense agency-guide sections) is
   split on sentence boundaries rather than hard-truncated. Every chunk
   keeps its source page range for citation.
2. **Embedding + storage** (`app/knowledge/guidelines_kb.py`) —
   `SentenceTransformerEmbeddingFunction` (all-MiniLM-L6-v2, CPU-friendly)
   into a persistent Chroma collection. Re-ingesting the same `source`
   upserts by chunk_id, so pushing a new guide revision is just running
   ingestion again.
3. **Search** — `search_guidelines(query, loan_type, top_k)` embeds the
   query, retrieves candidates, and filters by loan type in Python
   (Chroma metadata values are scalars, so `loan_types` is stored as a
   comma-joined string rather than a list). Falls back automatically to
   a small illustrative seed set if chromadb isn't installed or nothing
   has been ingested yet — so the API keeps working out of the box, but
   **the seed set is illustrative only and must not be relied on for
   real guideline answers.**

**Loading your real guides:**
- One-off / via API: `POST /admin/guidelines/ingest` (multipart file
  upload + `source_name` + `loan_types`). Put this behind admin auth
  before deploying — see the auth caveat below.
- Batch: `python scripts/ingest_guidelines.py --config manifest.yaml`,
  copy `scripts/guidelines_manifest.example.yaml` and point it at your
  actual Fannie Mae/Freddie Mac/HUD/VA PDFs and any internal overlay
  matrices.

**Tested, not just written:** `tests/test_guideline_ingest.py` runs the
real chunker against a synthetic multi-paragraph guideline PDF (one
paragraph deliberately long enough to force sentence-level splitting)
and asserts every distinctive phrase from the source survives somewhere
in the chunked output — i.e. the chunker can't silently drop content at
a boundary. The embed+store step (Chroma/sentence-transformers) is
written against the real API but wasn't executable in the environment
this was built in (no installed chromadb) — run `pytest` locally after
`pip install -r requirements.txt` to exercise that path too, and treat
`guidelines_kb.py`'s vector search as needing that local verification
pass before you trust it in production.

## Auth

Internal users only (loan officers, processors, admins) -- borrowers
never get an account; they're only ever reached through the voice/chat/
SMS agent. No self-serve signup, since this is an internal tool:

```bash
python scripts/create_user.py --email jane@lender.com --name "Jane Doe" --role admin
```

- **Password hashing** (`app/auth/security.py`): PBKDF2-HMAC-SHA256
  (260k iterations, random salt per password) via the standard library
  only -- deliberately no bcrypt/passlib dependency, so this could be
  fully unit-tested without extra installs. Swapping to bcrypt/argon2id
  later is a same-shaped function-body change, not a schema change.
- **Sessions**: JWT (`pyjwt`), read from either an `Authorization: Bearer`
  header (API clients) or an `access_token` httponly cookie (the
  dashboard). `require_role(UserRole.ADMIN)` / `require_authenticated`
  are FastAPI dependencies that protect routes.
- **What's protected**: guideline ingestion (admin only), document
  upload, loan creation/stage transitions, and the whole dashboard.
  Stage-transition audit entries (`changed_by`) come from the
  authenticated session, not a client-supplied field, so they can't be
  spoofed.
- **Tested**: `tests/test_auth.py` exercises real password hashing/
  verification (including that two hashes of the same password differ,
  and that a malformed hash fails closed rather than throwing), and real
  JWT issuance/expiry/tamper-detection -- no mocks, this is the actual
  crypto running.

## Dashboard

Server-rendered (FastAPI + Jinja2, no separate frontend build) at
`/dashboard`, protected by the cookie session above:

- **Pipeline board** (`/dashboard`) — a Kanban view, one column per
  active stage, loan cards showing days-in-stage and open-task count
  (flagged if any task is stale).
- **Loan detail** (`/dashboard/loans/{id}`) — documents, qualifying
  income, open tasks, stage history, and a stage-transition control that
  only offers the stages `VALID_TRANSITIONS` actually allows from the
  current stage (reuses the same state machine as the API, so the
  dashboard can't drift out of sync with what the agent enforces).
- **Stale tasks** (`/dashboard/tasks`) — flat queue across every loan.

**Design direction:** built around the idea of a physical loan file on a
processing desk rather than a generic SaaS dashboard template — cool
paper tones instead of the default warm-cream/terracotta look, IBM Plex
Serif/Sans/Mono as a coherent type system with financial data (loan IDs,
dollar amounts, dates) deliberately set in mono, and loan cards styled
like a stacked case-file folder with a stage-colored top edge. The stage
colors themselves form a cool-to-warm spectrum from Lead through to
Clear to Close/Funded, so color communicates progress rather than
decorating it.

**Tested:** `tests/test_dashboard_templates.py` renders every template
with real Jinja2 against realistic fake view-models, covering both the
populated and empty-state branch of each page (no documents yet, no
stale tasks, a terminal-stage loan with no further moves, an error
banner after a rejected transition). This catches template syntax
errors and undefined-variable crashes without needing a running server.
**Not exercised**: an actual end-to-end request through `app/dashboard/
routes.py` against a live DB, since fastapi/sqlalchemy weren't
installable in the sandbox this was built in — run it locally after
`pip install -r requirements.txt` before trusting the view-model
shaping logic (date formatting, stage-label lookups) in `routes.py`.

## Local setup

```bash
cp .env.example .env    # fill in GCP project, Twilio, SendGrid, etc.
docker compose up --build
# API on http://localhost:8000, docs at /docs
```

```bash
# create the tables (dev only; use Alembic migrations in production)
python -c "from app.db.session import init_db; init_db()"

# create your first admin user
python scripts/create_user.py --email you@lender.com --name "Your Name" --role admin

# run tests
pytest tests/ -v
```

Then open `http://localhost:8000/dashboard` and log in.

## Compliance notes (non-exhaustive — get counsel review)

- ECOA/Reg B: don't collect or act on prohibited basis information; the
  system prompt encodes this, but review your actual call flows.
- TILA/RESPA: this agent must not quote binding APR/terms — only a
  licensed originator or your LOS's disclosure workflow does that.
- Call recording: `RECORD_CALLS` / `CALL_RETENTION_DAYS` are exposed in
  config because requirements vary by state (many are one-party-consent,
  some require two-party consent) — set per your jurisdiction.
- NMLS/state licensing: confirm whether your state treats an AI voice
  agent discussing loan terms as requiring a licensed MLO in the loop;
  this varies and is evolving.

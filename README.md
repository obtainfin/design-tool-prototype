# Loan Assistant

A local, single-user web app that helps a solo Australian mortgage broker triage
a loan file: read documents, extract data, spot missing paperwork and
conflicting figures, draft client outreach, and prepare a Salestrekker
comparison pack -- all with the broker approving every step.

**This tool never sends anything, never writes to a CRM, never lodges an
application, and never states a final NCCP/BID, credit, servicing or product
conclusion.** Every AI output is a draft for the broker to review.

## Setup

Requires Python 3.10+.

```bash
pip install -r requirements.txt   # installs pypdf>=4.0, the only dependency
python3 launch.py                # starts the server and opens your browser
```

The app serves at `http://127.0.0.1:8789` and binds only to localhost.

Deal folders are created under `../Deals/<Deal Name>/` (a sibling of this
project folder), each with `Documents/Inbox`, `Documents/Filed`, `Notes`, and
`outputs`. Original documents you drop in `Documents/Inbox` are never
modified or moved -- filing a document copies it into
`Documents/Filed/<Category>/`.

### Optional `.env` file

Create a `.env` file in the project root to enable AI-assisted extraction and
per-step guidance:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
BROKER_AI_DISABLED=0
```

- `OPENAI_API_KEY` -- read only from this local `.env` file, never logged,
  never echoed, and scrubbed from any error message.
- `OPENAI_MODEL` -- optional; defaults to a current mini-tier model.
- `BROKER_AI_DISABLED=1` -- forces the whole app into local-only mode even if
  a key is present.

**With no `.env` file (or `BROKER_AI_DISABLED=1`), every feature still works.**
Document classification and field extraction fall back to keyword/regex
rules, and each assistant step falls back to a rule-based summary built from
the same extracted data. The header badge always shows the current AI status
("AI not connected..." or "AI connected (model)").

## Running the tests

```bash
python3 -m unittest discover -s tests
```

All tests run offline -- nothing in the test suite makes a network call.

## Safety model

- **No credentials, ever.** Every POST payload (call notes, file notes,
  free-text questions, deal names, lender codes) is checked against
  `security.validate_no_credentials` and rejected if it looks like a
  password, MFA/OTP code, API key, bearer token or private key block.
- **Tax identifiers are stripped before anything reaches the AI provider.**
  TFNs (labelled or bare 3-3-3 digit groups) and Medicare numbers are
  replaced with `[REMOVED_TAX_IDENTIFIER]` before any text is sent to the
  Responses API. Dollar amounts, dates, names and balances pass through --
  they're the actual work.
- **Drafts only.** SMS and email "sends" are `sms:`/`mailto:` links the
  broker opens in their own Messages/Mail app and sends themselves; the app
  never transmits anything on your behalf.
- **No CRM writes, no lodgement.** The Salestrekker comparison pack is a
  read-only, copy-pasteable text block. It is explicitly headed:
  *"Compare only. Do not enter or save anything in Salestrekker without
  broker approval per field. Verify client identity with name plus one more
  identifier before touching any record."*
- **No final conclusions.** Every AI prompt restates the guardrails: draft
  only, never a final NCCP/BID/credit/servicing/product conclusion, lender
  policy is placeholder data to be verified, not fact.
- **Atomic, audited state.** Each deal's 7-stage pipeline
  (`intake -> servicing -> policy -> lender_select -> docs -> st_prep ->
  submit`) is persisted atomically to `deal_state.json`, with every mutation
  appended to an audit log (`audit_log.jsonl`) as
  `{timestamp, event, actor, detail}`.

## Compliance checklist -- please read

`config/compliance_controls.json` and the Final Check step's NCCP/BID
checklist are a **reasonable default control list, not your aggregator's
audit document**. Verify against your aggregator's current audit checklist
before relying on this tool for compliance sign-off. The same warning is
shown in the UI whenever the checklist is displayed.

Similarly, `config/lenders.json` lists ~10 major Australian lenders as
shortcodes only -- every lender note is an explicit placeholder ("verify all
policy from current lender sources; not live policy data"). The assistant
never states lender policy as settled fact; it only ever phrases policy
points as questions for the broker to confirm with the lender/BDM.

## Project layout

```
launch.py                      starts the server, opens the browser
app.py                         HTTP handler, routes, inline UI page
src/broker/deal_state.py       7-stage state machine + audit log
src/broker/text_extractors.py  pdf/docx/xlsx/csv/txt/eml readers
src/broker/classifier.py       keyword document classification
src/broker/extraction.py       field schema, regex + AI evidence pipeline
src/broker/ai_client.py        OpenAI Responses API client
src/broker/checklists.py       per-deal-type missing document checklists
src/broker/compliance.py       NCCP/BID audit checklist
src/broker/security.py         credential blocking, TFN/Medicare stripping
src/broker/board.py            deal board + Salestrekker comparison pack
src/broker/workflow.py         the Run pipeline
config/*.json                  checklists, classification keywords, lenders,
                                compliance controls
tests/                         unittest suite, zero network
```

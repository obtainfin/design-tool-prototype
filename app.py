"""Loan Assistant HTTP handler: routes + the single-page UI.

Serves everything from 127.0.0.1:8789 via http.server.ThreadingHTTPServer.
The page itself is inline HTML/CSS/JS (no build step). All mutating actions
go through JSON POST endpoints under /api/. Every POST body is checked with
security.validate_no_credentials before it's used for anything.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import sys

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from broker import checklists, compliance, deal_state, security, workflow  # noqa: E402
from broker.ai_client import AIClient, AIRequestError  # noqa: E402
from broker.deal_state import DealState, InvalidTransitionError  # noqa: E402
from broker.extraction import FieldEvidence, parse_ai_reply  # noqa: E402
from broker import board as board_mod  # noqa: E402
from broker import extraction  # noqa: E402

HOST = "127.0.0.1"
PORT = 8789
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB per upload request

DEAL_TYPES = [
    "payg_purchase", "payg_refinance", "investment_loan",
    "self_employed_full_doc", "self_employed_alt_doc",
    "commercial_property_purchase", "commercial_refinance",
    "smsf_residential_property_loan", "smsf_commercial_property_loan",
    "company_trust_borrower", "business_lending", "lite_doc_scenario",
]

STAGE_BUTTON_LABELS = {
    "intake": "Start Triage",
    "servicing": "Check Servicing",
    "policy": "Check Policy",
    "lender_select": "Pick Lender",
    "docs": "Request Docs",
    "st_prep": "Prepare Salestrekker",
    "submit": "Final Check",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9 &'_.-]")
_INVALID_UPLOAD_CHARS = re.compile(r'[\\/:*?"<>|]')


def deals_root() -> Path:
    override = os.environ.get("BROKER_DEALS_ROOT")
    if override:
        return Path(override)
    return PROJECT_ROOT.parent / "Deals"


def sanitize_deal_name(name: str) -> str:
    name = (name or "").strip()
    name = _SAFE_NAME_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def deal_dir_for(name: str) -> Path:
    safe = sanitize_deal_name(name)
    if not safe or set(safe) <= {"."}:
        raise ValueError("Deal name is empty or invalid after sanitisation.")
    root = deals_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / safe).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("Invalid deal name.")
    return candidate


_deal_locks: Dict[str, threading.Lock] = {}
_deal_locks_guard = threading.Lock()


def _lock_for_deal(deal_dir: Path) -> threading.Lock:
    """One lock per deal directory, so concurrent requests against the same
    deal (ThreadingHTTPServer runs each request in its own thread) serialize
    their load-mutate-save cycle instead of racing and silently dropping
    one side's mutation.
    """
    key = str(deal_dir)
    with _deal_locks_guard:
        lock = _deal_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _deal_locks[key] = lock
        return lock


def list_deal_names() -> List[str]:
    root = deals_root()
    if not root.exists():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


_lenders_cache: Optional[List[Dict[str, str]]] = None


def load_lenders() -> List[Dict[str, str]]:
    """Process-lifetime cached lender list -- static config, read once."""
    global _lenders_cache
    if _lenders_cache is None:
        path = PROJECT_ROOT / "config" / "lenders.json"
        with open(path, "r", encoding="utf-8") as fh:
            _lenders_cache = json.load(fh)["lenders"]
    return _lenders_cache


def read_board(deal_dir: Path) -> Optional[Dict[str, Any]]:
    path = deal_dir / workflow.OUTPUTS_DIRNAME / board_mod.BOARD_JSON_FILENAME
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def evidence_pool_from_board(board: Dict[str, Any]) -> Dict[str, List[FieldEvidence]]:
    pool: Dict[str, List[FieldEvidence]] = {}
    for field_name, items in board.get("fields", {}).items():
        pool[field_name] = [FieldEvidence.from_dict(item) for item in items]
    return pool


# -- assistant prompts & offline fallbacks ------------------------------------

COMMON_GUARDRAILS = (
    "You are a supervised assistant for a solo Australian mortgage broker. "
    "You NEVER send SMS or email yourself -- you only propose drafts for the "
    "broker to review and send personally. You NEVER write to a CRM, never "
    "lodge anything, and never state a final NCCP/BID, credit, servicing or "
    "product conclusion -- those are the broker's decisions alone, not yours. "
    "Lender policy is placeholder data unless the broker has verified it -- "
    "phrase policy points as questions to confirm, not settled facts. "
    "Every reply you give is a DRAFT pending broker review.\n\n"
    'Return ONLY JSON of this shape, no prose outside JSON: '
    '{"reply": "markdown-lite text: **bold**, \\"- \\" bullets, paragraphs", '
    '"sms_draft": "optional short sms text", '
    '"email_subject": "optional email subject", '
    '"email_draft": "optional email body", '
    '"flags": ["optional short strings"]}'
)

JOB_INSTRUCTIONS = {
    "intake": (
        "Job: TRIAGE. Identify the 3 fastest potential deal-killers for this file "
        "and the missing basic documents/information. Be concise and specific, "
        "citing the source document for any claim you make."
    ),
    "servicing": (
        "Job: SERVICING. List which servicing inputs are evidenced (name the "
        "source) versus merely assumed or declared, and the gaps still open."
    ),
    "policy": (
        "Job: POLICY. List the specific policy questions the broker should verify "
        "with the lender or BDM before proceeding. Never state a lender policy as "
        "settled fact -- always phrase it as a question to confirm."
    ),
    "lender_select": (
        "Job: LENDER SHORTLIST. Given the deal profile, suggest 2-3 lenders from "
        "the provided shortlist worth comparing and what to check with each. "
        "Note that lender policy must be verified from current lender sources."
    ),
    "docs": (
        "Job: DOCUMENT REQUEST. Write a plain-language client document request, "
        "grouped logically, a maximum of 8 items, ready to send as SMS/email drafts."
    ),
    "st_prep": (
        "Job: SALESTREKKER PREP. Summarise the comparison list that will go into "
        "the Salestrekker prep pack, calling out anything not yet at high confidence."
    ),
    "submit": (
        "Job: FINAL CHECK. Cross-check declared vs evidenced values with quotes, "
        "and list any blockers before this file can be submitted."
    ),
}

_CONF_RANK = board_mod.CONFIDENCE_RANK


def _board_context(board: Dict[str, Any], state: DealState) -> Dict[str, Any]:
    fields_summary = {}
    for field_name, items in board.get("fields", {}).items():
        fields_summary[field_name] = [
            {
                "value": item["value"],
                "source_document": item["source_document"],
                "confidence": item["confidence"],
                "quote": item["quote"],
            }
            for item in items
        ]
    return {
        "deal_type": state.deal_type,
        "stage": state.stage,
        "selected_lender": state.selected_lender,
        "fields": fields_summary,
        "conflicts": board.get("conflicts", []),
        "missing_documents": board.get("missing_documents", {}),
        "compliance": board.get("compliance", {}),
        "documents": board.get("documents", []),
    }


def _clamp_assistant_reply(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    out: Dict[str, Any] = {"reply": reply.strip()[:4000]}
    for key in ("sms_draft", "email_subject", "email_draft"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:2000]
    flags = parsed.get("flags")
    if isinstance(flags, list):
        out["flags"] = [str(f)[:200] for f in flags if isinstance(f, (str, int, float))][:10]
    return out


def generate_step_reply(ai_client: AIClient, state: DealState, board: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    stage = state.stage
    if board is None:
        return {
            "reply": "Run the pipeline first (use the button above) so I can read the documents in the inbox.",
            "source": "fallback",
        }
    if ai_client.enabled:
        try:
            instructions = COMMON_GUARDRAILS + "\n\n" + JOB_INSTRUCTIONS.get(stage, "")
            context = _board_context(board, state)
            user_content = f"Deal data (JSON):\n{json.dumps(context, indent=2, ensure_ascii=False)}"
            user_content = security.strip_tax_identifiers(user_content)
            raw = ai_client.respond(instructions, user_content)
            parsed = parse_ai_reply(raw)
            clamped = _clamp_assistant_reply(parsed)
            if clamped:
                clamped["source"] = "ai"
                return clamped
        except AIRequestError:
            pass
    fallback = _offline_step_fallback(stage, board, state)
    fallback["source"] = "fallback"
    return fallback


def generate_free_text_reply(ai_client: AIClient, question: str, board: Optional[Dict[str, Any]], state: DealState) -> Dict[str, Any]:
    if ai_client.enabled and board is not None:
        try:
            instructions = COMMON_GUARDRAILS + "\n\nJob: answer the broker's free-text question using ONLY the deal context given. If the context doesn't cover it, say so plainly."
            context = _board_context(board, state)
            user_content = f"Question: {question}\n\nDeal data (JSON):\n{json.dumps(context, indent=2, ensure_ascii=False)}"
            user_content = security.strip_tax_identifiers(user_content)
            raw = ai_client.respond(instructions, user_content)
            parsed = parse_ai_reply(raw)
            clamped = _clamp_assistant_reply(parsed)
            if clamped:
                clamped["source"] = "ai"
                return clamped
        except AIRequestError:
            pass
    if board is None:
        reply = "Run the pipeline first (use the button above) so I have deal data to answer from."
    else:
        reply = _offline_summary_text(board, state)
    return {"reply": reply, "source": "fallback"}


def _offline_step_fallback(stage: str, board: Dict[str, Any], state: DealState) -> Dict[str, Any]:
    missing = board.get("missing_documents", {})
    conflicts = board.get("conflicts", [])

    if stage == "intake":
        lines = ["**Fastest deal-killers to check first:**"]
        top_missing = missing.get("required_now", [])[:3]
        if top_missing:
            for item in top_missing:
                lines.append(f"- Missing now: {item}")
        else:
            lines.append("- No required-now items missing.")
        if conflicts:
            lines.append(f"- {len(conflicts)} conflicting value(s) need review.")
        return {"reply": "\n".join(lines)}

    if stage == "servicing":
        lines = ["**Servicing inputs -- evidenced vs assumed:**"]
        for full_field in (
            "servicing_targets.gross_income_annual",
            "servicing_targets.base_income_noa",
            "servicing_targets.declared_living_expenses",
        ):
            items = board.get("fields", {}).get(full_field)
            if items:
                best = min(items, key=lambda e: _CONF_RANK.get(e["confidence"], 2))
                lines.append(
                    f"- {full_field}: {best['value']} ({best['confidence']} confidence, {best['source_document']})"
                )
            else:
                lines.append(f"- {full_field}: not yet evidenced -- ask the client or check documents.")
        return {"reply": "\n".join(lines)}

    if stage == "policy":
        lines = [
            "**Questions to verify with the lender before proceeding:**",
            "- Confirm current policy for this deal type and security property directly with the lender/BDM.",
            f"- Selected lender: {state.selected_lender or '(none yet)'}",
        ]
        return {"reply": "\n".join(lines)}

    if stage == "lender_select":
        lines = ["**Lender shortlist (placeholder -- verify all policy from current lender sources):**"]
        for lender in load_lenders():
            lines.append(f"- {lender['code']}: {lender['name']}")
        return {"reply": "\n".join(lines)}

    if stage == "docs":
        lines = ["**Documents still needed from the client:**"]
        count = 0
        for bucket in ("required_now", "required_before_submission"):
            for item in missing.get(bucket, []):
                if count >= 8:
                    break
                lines.append(f"- {item}")
                count += 1
        if count == 0:
            lines.append("- Nothing outstanding right now.")
        return {"reply": "\n".join(lines)}

    if stage == "st_prep":
        return {
            "reply": "Use the Salestrekker comparison pack below. Compare only -- do not enter "
            "or save anything without broker approval."
        }

    if stage == "submit":
        lines = ["**Declared vs evidenced cross-check:**"]
        if conflicts:
            for conflict in conflicts:
                lines.append(f"- BLOCKER: {conflict['message']}")
        else:
            lines.append("- No conflicts detected in the evidence pool.")
        missing_before_submit = missing.get("required_before_submission", [])
        if missing_before_submit:
            lines.append(f"- BLOCKER: {len(missing_before_submit)} item(s) still required before submission.")
        return {"reply": "\n".join(lines)}

    return {"reply": "No guidance available for this stage yet."}


def _offline_summary_text(board: Dict[str, Any], state: DealState) -> str:
    missing_count = sum(len(v) for v in board.get("missing_documents", {}).values())
    conflict_count = len(board.get("conflicts", []))
    return (
        f"AI is not connected, so here's the rule-based summary: deal type "
        f"{state.deal_type}, stage {state.stage}, {missing_count} outstanding "
        f"checklist item(s), {conflict_count} conflict(s) to review. Open the "
        f"technical detail section for the full deal board."
    )


def try_route_keyword_action(text: str, state: DealState, deal_dir: Path) -> Optional[str]:
    """Keyword routing for state actions. Returns a confirmation message if a
    keyword action was taken (and persists the mutation), else None.

    A message containing '?' is treated as a genuine question (e.g. "should
    I mark step done now or wait for the NOA?") and is never routed to a
    state-mutating action -- only unambiguous literal commands are.
    """
    if "?" in text:
        return None

    lowered = text.strip().lower()

    if re.search(r"\bmark\s+step\s+done\b", lowered) or re.search(r"\bdone\b.{0,12}\bnext\s*step\b", lowered):
        try:
            new_stage = state.advance(actor="broker (free text)")
        except InvalidTransitionError as exc:
            return f"Could not advance: {exc}"
        deal_state.save(deal_dir, state)
        return f"Marked done -- moved to stage '{new_stage}'."

    lender_match = re.search(r"\bselect\s+lender\s+([a-zA-Z]+)\b", text, re.IGNORECASE)
    if lender_match:
        code = lender_match.group(1).upper()
        valid_codes = {lender["code"] for lender in load_lenders()}
        if code not in valid_codes:
            return f"'{code}' is not a recognised lender shortcode. Options: {', '.join(sorted(valid_codes))}."
        _select_lender(state, code, actor="broker (free text)")
        deal_state.save(deal_dir, state)
        return (
            f"Selected lender {code}. Reminder: lender policy notes are placeholders -- "
            "verify all policy from current lender sources."
        )

    if re.search(r"\bsign\s*off\s*nccp\b", lowered):
        all_ids = [c["id"] for c in compliance.load_config()["controls"]]
        state.sign_off_nccp(all_ids, actor="broker (free text)")
        deal_state.save(deal_dir, state)
        # Rebuild the board so the NCCP/BID compliance section (and its
        # "reviewed" counts shown in the UI badge) reflect the sign-off
        # immediately, instead of only on the next manual Run.
        workflow.run(deal_dir, deal_type=state.deal_type, ai_client=AIClient())
        return "Broker sign-off recorded for all NCCP/BID controls. " + compliance.VERIFY_WARNING

    return None


def _select_lender(state: DealState, code: str, actor: str) -> None:
    """Set the active lender, auto-advancing past lender_select if that's
    the gate it was blocking. Shared by the /lender endpoint and the
    'select lender X' free-text command so both stay in sync.
    """
    state.set_lender(code, actor=actor)
    if state.stage == "lender_select":
        try:
            state.advance(actor=actor)
        except InvalidTransitionError:
            pass


# -- request handling ---------------------------------------------------------


class LoanAssistantHandler(BaseHTTPRequestHandler):
    server_version = "LoanAssistant/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    # -- helpers -----------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Invalid JSON body: {exc}")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        security.validate_no_credentials(payload)
        return payload

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    # -- GET -----------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", ""):
                self._send_html(INDEX_HTML)
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/deals":
                self._send_json(200, {"deals": self._deal_summaries()})
                return
            if path == "/api/lenders":
                self._send_json(200, {"lenders": load_lenders()})
                return

            match = re.match(r"^/api/deal/([^/]+)/st_comparison$", path)
            if match:
                self._handle_deal_detail(unquote(match.group(1)), include_st=True)
                return

            match = re.match(r"^/api/deal/([^/]+)$", path)
            if match:
                self._handle_deal_detail(unquote(match.group(1)))
                return

            self._error(404, "Not found.")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"Internal error: {exc}")

    def _deal_summaries(self) -> List[Dict[str, Any]]:
        summaries = []
        for name in list_deal_names():
            state = deal_state.load(deal_dir_for(name))
            summaries.append(
                {
                    "name": name,
                    "deal_type": state.deal_type,
                    "stage": state.stage,
                    "ui_step_name": deal_state.ui_step_name(state.stage),
                }
            )
        return summaries

    def _handle_deal_detail(self, name: str, include_st: bool = False) -> None:
        deal_dir = deal_dir_for(name)
        if not deal_dir.exists():
            self._error(404, f"Deal '{name}' not found.")
            return
        self._send_json(200, self._build_deal_payload(name, deal_dir))

    def _build_deal_payload(
        self,
        name: str,
        deal_dir: Path,
        assistant: Optional[Dict[str, Any]] = None,
        ai_client: Optional[AIClient] = None,
        lenders: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        state = deal_state.load(deal_dir, deal_name=name)
        board = read_board(deal_dir)
        if ai_client is None:
            ai_client = AIClient()
        if lenders is None:
            lenders = load_lenders()

        st_comparison = None
        if board is not None:
            pool = evidence_pool_from_board(board)
            rows = board_mod.build_st_comparison(pool)
            st_comparison = {
                "header": board_mod.ST_COMPARISON_HEADER,
                "rows": rows,
                "text": board_mod.render_st_comparison_text(rows),
            }

        payload = {
            "name": name,
            "deal_type": state.deal_type,
            "stage": state.stage,
            "ui_steps": deal_state.UI_STEPS,
            "ui_step_index": deal_state.ui_step_index(state.stage),
            "ui_step_name": deal_state.ui_step_name(state.stage),
            "primary_label": STAGE_BUTTON_LABELS[state.stage],
            "selected_lender": state.selected_lender,
            "st_prep_recorded": state.st_prep_recorded,
            "can_advance": state.can_advance(),
            "ai_status": ai_client.status_label(),
            "ai_enabled": ai_client.enabled,
            "lenders": lenders,
            "deal_types": DEAL_TYPES,
            "board": board,
            "st_comparison": st_comparison,
            "audit_log_tail": state.audit_log[-25:],
            "show_nccp_badge": state.stage == "submit",
        }
        if assistant is not None:
            payload["assistant"] = assistant
        return payload

    # -- POST ----------------------------------------------------------------

    def do_POST(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/deals":
                self._handle_create_deal()
                return

            match = re.match(r"^/api/deal/([^/]+)/([a-zA-Z_]+)$", path)
            if not match:
                self._error(404, "Not found.")
                return
            name, action = unquote(match.group(1)), match.group(2)
            deal_dir = deal_dir_for(name)

            if action == "upload":
                with _lock_for_deal(deal_dir):
                    self._handle_upload(name, deal_dir)
                return

            if not deal_dir.exists():
                self._error(404, f"Deal '{name}' not found.")
                return

            handlers = {
                "run": self._handle_run,
                "advance": self._handle_advance,
                "lender": self._handle_lender,
                "st_prep": self._handle_st_prep,
                "step_action": self._handle_step_action,
                "ask": self._handle_ask,
                "call_note": self._handle_call_note,
                "file_note": self._handle_file_note,
                "open_inbox": self._handle_open_inbox,
            }
            handler = handlers.get(action)
            if handler is None:
                self._error(404, "Unknown action.")
                return
            # Serialize every load-mutate-save cycle per deal so two
            # concurrent requests against the same deal can't silently
            # clobber each other's state (ThreadingHTTPServer runs each
            # request on its own thread).
            with _lock_for_deal(deal_dir):
                handler(name, deal_dir)
        except security.CredentialDetectedError as exc:
            self._error(400, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"Internal error: {exc}")

    def _handle_create_deal(self) -> None:
        payload = self._read_json_body()
        name = payload.get("name", "")
        deal_type = payload.get("deal_type", "")
        if deal_type not in DEAL_TYPES:
            self._error(400, f"Unknown deal_type '{deal_type}'.")
            return
        deal_dir = deal_dir_for(name)
        with _lock_for_deal(deal_dir):
            safe_name = deal_dir.name
            is_new = not deal_dir.exists()
            workflow.ensure_deal_folders(deal_dir)
            state = deal_state.load(deal_dir, deal_name=safe_name, deal_type=deal_type)
            deal_state.save(deal_dir, state)
        self._send_json(201 if is_new else 200, self._build_deal_payload(safe_name, deal_dir))

    def _handle_run(self, name: str, deal_dir: Path) -> None:
        ai_client = AIClient()
        state = deal_state.load(deal_dir, deal_name=name)
        workflow.run(deal_dir, deal_type=state.deal_type, ai_client=ai_client)
        self._send_json(200, self._build_deal_payload(name, deal_dir, ai_client=ai_client))

    def _handle_advance(self, name: str, deal_dir: Path) -> None:
        state = deal_state.load(deal_dir, deal_name=name)
        try:
            state.advance(actor="broker")
        except InvalidTransitionError as exc:
            self._error(409, str(exc))
            return
        deal_state.save(deal_dir, state)
        self._send_json(200, self._build_deal_payload(name, deal_dir))

    def _handle_lender(self, name: str, deal_dir: Path) -> None:
        payload = self._read_json_body()
        code = str(payload.get("lender_code", "")).strip().upper()
        lenders = load_lenders()
        valid_codes = {lender["code"] for lender in lenders}
        if code not in valid_codes:
            self._error(400, f"Unknown lender_code '{code}'.")
            return
        state = deal_state.load(deal_dir, deal_name=name)
        _select_lender(state, code, actor="broker")
        deal_state.save(deal_dir, state)
        self._send_json(200, self._build_deal_payload(name, deal_dir, lenders=lenders))

    def _handle_st_prep(self, name: str, deal_dir: Path) -> None:
        state = deal_state.load(deal_dir, deal_name=name)
        state.record_st_prep(actor="broker")
        deal_state.save(deal_dir, state)
        self._send_json(200, self._build_deal_payload(name, deal_dir))

    def _handle_step_action(self, name: str, deal_dir: Path) -> None:
        state = deal_state.load(deal_dir, deal_name=name)
        board = read_board(deal_dir)
        ai_client = AIClient()
        result = generate_step_reply(ai_client, state, board)
        if state.stage == "st_prep" and board is not None and not state.st_prep_recorded:
            state.record_st_prep(actor="broker")
        state.log("assistant_reply", result.get("source", "assistant"), result["reply"][:300])
        deal_state.save(deal_dir, state)
        payload = self._build_deal_payload(name, deal_dir, assistant=result, ai_client=ai_client)
        self._send_json(200, payload)

    def _handle_ask(self, name: str, deal_dir: Path) -> None:
        payload_in = self._read_json_body()
        question = str(payload_in.get("question", "")).strip()
        if not question:
            self._error(400, "Question is empty.")
            return
        state = deal_state.load(deal_dir, deal_name=name)

        action_message = try_route_keyword_action(question, state, deal_dir)
        if action_message is not None:
            # `state` already reflects the mutation try_route_keyword_action
            # made in place (and it has already been persisted) -- no need
            # to reload it from disk.
            result = {"reply": action_message, "source": "action"}
            state.log("assistant_reply", "action", result["reply"][:300])
            deal_state.save(deal_dir, state)
            self._send_json(200, self._build_deal_payload(name, deal_dir, assistant=result))
            return

        board = read_board(deal_dir)
        ai_client = AIClient()
        result = generate_free_text_reply(ai_client, question, board, state)
        state.log("assistant_reply", result.get("source", "assistant"), result["reply"][:300])
        deal_state.save(deal_dir, state)
        self._send_json(200, self._build_deal_payload(name, deal_dir, assistant=result, ai_client=ai_client))

    def _handle_call_note(self, name: str, deal_dir: Path) -> None:
        payload_in = self._read_json_body()
        text = str(payload_in.get("text", "")).strip()
        if not text:
            self._error(400, "Call note text is empty.")
            return
        state = deal_state.load(deal_dir, deal_name=name)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        source_name = f"broker call note {today}"

        workflow.append_file_note(deal_dir, text, actor="broker", heading=f"Call note -- {today}")

        new_evidence = list(extraction.extract_regex_fields(text, source_name))
        ai_client = AIClient()
        if ai_client.enabled:
            _doc_type, _doc_conf, ai_evidence, ai_failure = extraction.ai_extract_document(
                ai_client, text, source_name
            )
            new_evidence.extend(ai_evidence)
            if ai_failure:
                state.log("ai_extraction_failed", "system", f"{source_name}: {ai_failure}")
        workflow.append_extra_evidence(deal_dir, new_evidence)

        state.log("call_note_added", "broker", f"Call note added ({len(new_evidence)} field(s) extracted).")
        deal_state.save(deal_dir, state)

        workflow.run(deal_dir, deal_type=state.deal_type, ai_client=ai_client)
        self._send_json(200, self._build_deal_payload(name, deal_dir, ai_client=ai_client))

    def _handle_file_note(self, name: str, deal_dir: Path) -> None:
        payload_in = self._read_json_body()
        text = str(payload_in.get("text", "")).strip()
        if not text:
            self._error(400, "File note text is empty.")
            return
        state = deal_state.load(deal_dir, deal_name=name)
        workflow.append_file_note(deal_dir, text, actor="broker")
        state.log("file_note_added", "broker", text[:300])
        deal_state.save(deal_dir, state)
        self._send_json(200, self._build_deal_payload(name, deal_dir))

    def _handle_open_inbox(self, name: str, deal_dir: Path) -> None:
        paths = workflow.deal_paths(deal_dir)
        inbox = paths["inbox"]
        inbox.mkdir(parents=True, exist_ok=True)
        opener = shutil.which("open")
        if opener:
            try:
                subprocess.run([opener, str(inbox)], check=False, timeout=5)
                self._send_json(200, {"opened": True, "path": str(inbox)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(200, {"opened": False, "path": str(inbox), "message": str(exc)})
                return
        self._send_json(
            200,
            {
                "opened": False,
                "path": str(inbox),
                "message": "Automatic open is only wired up for macOS `open`. Folder path shown above.",
            },
        )

    def _handle_upload(self, name: str, deal_dir: Path) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._error(400, "Expected multipart/form-data upload.")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_UPLOAD_BYTES:
            self._error(413, f"Upload too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per request).")
            return
        raw_body = self.rfile.read(length) if length else b""

        workflow.ensure_deal_folders(deal_dir)
        inbox = workflow.deal_paths(deal_dir)["inbox"]

        files = _parse_multipart(raw_body, content_type)
        saved = []
        for filename, data in files:
            safe_name = _safe_upload_filename(filename)
            if not safe_name:
                continue
            security.validate_no_credentials(safe_name)
            dest = _unique_path(inbox / safe_name)
            dest.write_bytes(data)
            saved.append(dest.name)

        state = deal_state.load(deal_dir, deal_name=name)
        if saved:
            state.log("documents_uploaded", "broker", f"Uploaded: {', '.join(saved)}")
            deal_state.save(deal_dir, state)

        self._send_json(200, {"saved": saved, **self._build_deal_payload(name, deal_dir)})


def _safe_upload_filename(filename: str) -> str:
    name = Path(filename or "").name
    name = _INVALID_UPLOAD_CHARS.sub("-", name).strip()
    if not name or set(name) <= {"."}:
        return ""
    return name


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _parse_multipart(body: bytes, content_type: str) -> List[tuple]:
    from email.parser import BytesParser
    from email import policy as email_policy

    header_bytes = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser(policy=email_policy.default).parsebytes(header_bytes + body)
    results = []
    if msg.is_multipart():
        for part in msg.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            payload = part.get_payload(decode=True)
            results.append((filename, payload or b""))
    return results


# -- the inline single-page UI -------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Loan Assistant</title>
<style>
  :root {
    --bg: #f6f7f5;
    --panel: #ffffff;
    --border: #dfe3df;
    --text: #23291f;
    --muted: #6b7266;
    --accent: #2f6f5e;
    --accent-dark: #245647;
    --warn: #b45309;
    --danger: #b3261e;
    --radius: 10px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 980px; margin: 0 auto; padding: 20px 16px 60px; }
  header.top {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 0 18px;
  }
  header.top h1 { font-size: 20px; margin: 0; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
    border: 1px solid var(--border); background: #eef2ec; color: var(--muted);
  }
  .badge.on { background: #e4f3ec; color: var(--accent-dark); border-color: #bfe0d0; }
  .badge.off { background: #f3ede4; color: #8a6d3b; border-color: #e6d9bd; }
  .badge.nccp { background: #fdf1ee; color: var(--danger); border-color: #f3c9c3; }
  .grid { display: grid; grid-template-columns: 280px 1fr; gap: 18px; align-items: start; }
  @media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
  .panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px;
  }
  .panel + .panel { margin-top: 14px; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; font-weight: 600; }
  select, input[type=text], textarea {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
    font: inherit; background: #fff; color: var(--text);
  }
  textarea { resize: vertical; min-height: 70px; }
  button {
    font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--border);
    background: #fff; padding: 8px 14px; color: var(--text);
  }
  button:hover { background: #f1f3f0; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
  button.primary:hover { background: var(--accent-dark); }
  button.secondary { background: #fff; }
  button.small { padding: 4px 10px; font-size: 12px; }
  .dropzone {
    border: 2px dashed var(--border); border-radius: var(--radius); padding: 22px 12px;
    text-align: center; color: var(--muted); font-size: 13px; margin-top: 10px; cursor: pointer;
  }
  .dropzone.drag { border-color: var(--accent); background: #eef6f1; color: var(--accent-dark); }
  .steps { display: flex; gap: 6px; margin-bottom: 14px; }
  .step-pill {
    flex: 1; text-align: center; font-size: 11px; padding: 6px 4px; border-radius: 999px;
    background: #eef2ec; color: var(--muted); border: 1px solid var(--border);
  }
  .step-pill.active { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 700; }
  .step-pill.done { background: #dcefe4; color: var(--accent-dark); }
  .answer { background: #fafaf8; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; min-height: 60px; }
  .answer p { margin: 0 0 8px; }
  .answer ul { margin: 0 0 8px 18px; padding: 0; }
  .actions-row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  .muted { color: var(--muted); font-size: 12px; }
  .draft-block { border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-top: 10px; background: #fff; }
  .draft-block textarea { min-height: 50px; }
  details { margin-top: 12px; }
  summary { cursor: pointer; font-size: 13px; font-weight: 600; color: var(--muted); }
  pre { white-space: pre-wrap; word-break: break-word; font-size: 12.5px; background: #fafaf8; border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
  .checklist-bucket h4 { margin: 10px 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  .checklist-bucket ul { margin: 0 0 6px 18px; padding: 0; }
  footer.foot { text-align: center; color: var(--muted); font-size: 12px; margin-top: 28px; }
  .toast {
    position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
    background: #23291f; color: #fff; padding: 10px 16px; border-radius: 8px; font-size: 13px;
    opacity: 0; pointer-events: none; transition: opacity .2s ease;
  }
  .toast.show { opacity: 1; }
  .st-header { background: #fdf1ee; border: 1px solid #f3c9c3; color: var(--danger); border-radius: 8px; padding: 10px 12px; font-size: 13px; margin-bottom: 10px; font-weight: 600; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Loan Assistant</h1>
    <span id="aiBadge" class="badge">AI status unknown</span>
  </header>

  <div class="grid">
    <div>
      <div class="panel">
        <label for="dealPicker">Deal</label>
        <select id="dealPicker"></select>

        <label for="loanTypePicker">Loan type</label>
        <select id="loanTypePicker"></select>

        <div id="dropzone" class="dropzone">Drag files here to add to the inbox, or click to browse</div>
        <input type="file" id="fileInput" multiple style="display:none" />

        <div class="actions-row">
          <button id="openInboxBtn" class="small">Open Inbox Folder</button>
          <button id="runBtn" class="small">Run</button>
        </div>

        <details>
          <summary>Create a new deal</summary>
          <label for="newDealName">Deal name</label>
          <input type="text" id="newDealName" placeholder="e.g. Smith Purchase" />
          <label for="newDealType">Loan type</label>
          <select id="newDealType"></select>
          <div class="actions-row"><button id="createDealBtn" class="small">Create deal</button></div>
        </details>
      </div>
    </div>

    <div>
      <div class="panel">
        <div class="steps" id="stepsStrip"></div>

        <div id="nccpBadgeRow"></div>

        <div class="answer" id="answerPanel">Select or create a deal, then click the button below to get started.</div>

        <div class="actions-row">
          <button id="primaryBtn" class="primary">Start Triage</button>
          <button id="doneNextBtn" class="secondary">Done — Next Step</button>
        </div>

        <div id="draftArea"></div>

        <div class="actions-row">
          <button id="saveFileNoteBtn" class="small" disabled>Save As File Note</button>
        </div>

        <label for="askBox">Ask a question</label>
        <textarea id="askBox" placeholder="e.g. what's still missing before I can submit?"></textarea>
        <div class="actions-row"><button id="askBtn">Ask</button></div>

        <div id="lenderRow"></div>
        <div id="callNoteRow"></div>
        <div id="stPackRow"></div>

        <details>
          <summary>Step checklist</summary>
          <div id="checklistBody" class="muted">No deal board yet -- click Run.</div>
        </details>

        <details>
          <summary>Technical detail</summary>
          <pre id="technicalBody">(nothing yet)</pre>
        </details>
      </div>
    </div>
  </div>

  <footer class="foot">You approve before anything is saved or sent.</footer>
</div>
<div class="toast" id="toast"></div>

<script>
const DEAL_TYPES = [
  ["payg_purchase", "PAYG purchase"],
  ["payg_refinance", "PAYG refinance"],
  ["investment_loan", "Investment loan"],
  ["self_employed_full_doc", "Self-employed (full doc)"],
  ["self_employed_alt_doc", "Self-employed (alt doc)"],
  ["commercial_property_purchase", "Commercial property purchase"],
  ["commercial_refinance", "Commercial refinance"],
  ["smsf_residential_property_loan", "SMSF residential property loan"],
  ["smsf_commercial_property_loan", "SMSF commercial property loan"],
  ["company_trust_borrower", "Company/trust borrower"],
  ["business_lending", "Business lending"],
  ["lite_doc_scenario", "Lite doc scenario"],
];

let currentDeal = null;

function $(id) { return document.getElementById(id); }
function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function boldify(s) { return s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"); }
function renderMarkdownLite(text) {
  const esc = escapeHtml(text);
  const lines = esc.split(/\r?\n/);
  let html = ""; let inList = false;
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line.startsWith("- ")) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${boldify(line.slice(2))}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (line !== "") html += `<p>${boldify(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html || "<p></p>";
}
function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

function populateSelect(select, options, valueKey, labelKey) {
  select.innerHTML = "";
  for (const opt of options) {
    const o = document.createElement("option");
    if (Array.isArray(opt)) { o.value = opt[0]; o.textContent = opt[1]; }
    else { o.value = opt[valueKey]; o.textContent = opt[labelKey]; }
    select.appendChild(o);
  }
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

async function refreshDealList(selectName) {
  const data = await api("/api/deals");
  const picker = $("dealPicker");
  picker.innerHTML = "";
  for (const deal of data.deals) {
    const o = document.createElement("option");
    o.value = deal.name;
    o.textContent = `${deal.name} — ${deal.ui_step_name}`;
    picker.appendChild(o);
  }
  if (data.deals.length === 0) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "(no deals yet -- create one)";
    picker.appendChild(o);
    currentDeal = null;
    render();
    return;
  }
  const target = selectName && data.deals.some(d => d.name === selectName) ? selectName : data.deals[0].name;
  picker.value = target;
  await loadDeal(target);
}

async function loadDeal(name) {
  if (!name) { currentDeal = null; render(); return; }
  currentDeal = await api(`/api/deal/${encodeURIComponent(name)}`);
  render();
}

function render() {
  renderAiBadge();
  renderSteps();
  renderAnswer();
  renderDrafts();
  renderLender();
  renderCallNote();
  renderStPack();
  renderChecklist();
  renderTechnical();
  renderButtons();
  $("loanTypePicker").disabled = !currentDeal;
  if (currentDeal) $("loanTypePicker").value = currentDeal.deal_type;
}

function renderAiBadge() {
  const badge = $("aiBadge");
  if (!currentDeal) { badge.textContent = "AI status unknown"; badge.className = "badge"; return; }
  badge.textContent = currentDeal.ai_status;
  badge.className = "badge " + (currentDeal.ai_enabled ? "on" : "off");
}

function renderSteps() {
  const strip = $("stepsStrip");
  strip.innerHTML = "";
  const nccpRow = $("nccpBadgeRow");
  nccpRow.innerHTML = "";
  if (!currentDeal) return;
  currentDeal.ui_steps.forEach((label, i) => {
    const pill = document.createElement("div");
    pill.className = "step-pill" + (i === currentDeal.ui_step_index ? " active" : (i < currentDeal.ui_step_index ? " done" : ""));
    pill.textContent = label;
    strip.appendChild(pill);
  });
  if (currentDeal.show_nccp_badge && currentDeal.board) {
    const controls = currentDeal.board.compliance.controls;
    const reviewed = controls.filter(c => c.status === "reviewed").length;
    const badge = document.createElement("span");
    badge.className = "badge nccp";
    badge.textContent = `NCCP/BID: ${reviewed}/${controls.length} reviewed -- verify against your aggregator checklist`;
    nccpRow.appendChild(badge);
  }
}

function renderAnswer() {
  const panel = $("answerPanel");
  if (!currentDeal) { panel.innerHTML = "Select or create a deal, then click the button below to get started."; return; }
  const assistant = currentDeal.assistant;
  if (!assistant) {
    panel.innerHTML = "Click the button below to get started on this step.";
    return;
  }
  panel.innerHTML = renderMarkdownLite(assistant.reply);
  $("saveFileNoteBtn").disabled = false;
}

function renderDrafts() {
  const area = $("draftArea");
  area.innerHTML = "";
  const assistant = currentDeal && currentDeal.assistant;
  if (!assistant) return;
  if (assistant.sms_draft) {
    const block = document.createElement("div");
    block.className = "draft-block";
    block.innerHTML = `<label>SMS draft</label><textarea readonly>${escapeHtml(assistant.sms_draft)}</textarea>
      <div class="actions-row">
        <a href="sms:?body=${encodeURIComponent(assistant.sms_draft)}"><button class="small" type="button">Open in Messages</button></a>
        <button class="small" type="button" data-copy="sms">Copy</button>
      </div>`;
    area.appendChild(block);
    block.querySelector('[data-copy="sms"]').onclick = () => { navigator.clipboard.writeText(assistant.sms_draft); showToast("SMS draft copied."); };
  }
  if (assistant.email_draft || assistant.email_subject) {
    const subject = assistant.email_subject || "";
    const bodyText = assistant.email_draft || "";
    const block = document.createElement("div");
    block.className = "draft-block";
    block.innerHTML = `<label>Email draft</label>
      <input type="text" readonly value="${escapeHtml(subject)}" />
      <textarea readonly>${escapeHtml(bodyText)}</textarea>
      <div class="actions-row">
        <a href="mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(bodyText)}"><button class="small" type="button">Open in Mail</button></a>
        <button class="small" type="button" data-copy="email">Copy</button>
      </div>`;
    area.appendChild(block);
    block.querySelector('[data-copy="email"]').onclick = () => { navigator.clipboard.writeText(subject + "\n\n" + bodyText); showToast("Email draft copied."); };
  }
}

function renderLender() {
  const row = $("lenderRow");
  row.innerHTML = "";
  if (!currentDeal || currentDeal.stage !== "lender_select") return;
  const label = document.createElement("label");
  label.textContent = "Select lender";
  const select = document.createElement("select");
  for (const lender of currentDeal.lenders) {
    const o = document.createElement("option");
    o.value = lender.code; o.textContent = `${lender.code} -- ${lender.name}`;
    select.appendChild(o);
  }
  const btn = document.createElement("button");
  btn.className = "small"; btn.textContent = "Select Lender";
  btn.onclick = async () => {
    currentDeal = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/lender`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lender_code: select.value }),
    });
    render();
    showToast(`Lender ${select.value} selected (placeholder policy -- verify with lender).`);
  };
  const wrap = document.createElement("div"); wrap.className = "actions-row";
  wrap.appendChild(btn);
  row.appendChild(label); row.appendChild(select); row.appendChild(wrap);
}

function renderCallNote() {
  const row = $("callNoteRow");
  row.innerHTML = "";
  if (!currentDeal || currentDeal.ui_step_index !== 0) return;
  const label = document.createElement("label");
  label.textContent = "Paste call notes";
  const textarea = document.createElement("textarea");
  textarea.placeholder = "Paste or type what the client told you on the call...";
  const btn = document.createElement("button");
  btn.className = "small"; btn.textContent = "Save call note";
  btn.onclick = async () => {
    if (!textarea.value.trim()) return;
    btn.disabled = true; btn.textContent = "Working…";
    try {
      currentDeal = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/call_note`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: textarea.value }),
      });
      showToast("Call note saved and board rebuilt.");
      render();
    } catch (e) { showToast(e.message); }
    finally { btn.disabled = false; btn.textContent = "Save call note"; }
  };
  const wrap = document.createElement("div"); wrap.className = "actions-row"; wrap.appendChild(btn);
  row.appendChild(label); row.appendChild(textarea); row.appendChild(wrap);
}

function renderStPack() {
  const row = $("stPackRow");
  row.innerHTML = "";
  if (!currentDeal || currentDeal.stage !== "st_prep" || !currentDeal.st_comparison) return;
  const st = currentDeal.st_comparison;
  const header = document.createElement("div");
  header.className = "st-header";
  header.textContent = st.header;
  const pre = document.createElement("pre");
  pre.textContent = st.text;
  const btn = document.createElement("button");
  btn.className = "small"; btn.textContent = "Copy comparison pack";
  btn.onclick = () => { navigator.clipboard.writeText(st.text); showToast("Comparison pack copied."); };
  row.appendChild(header); row.appendChild(pre);
  const wrap = document.createElement("div"); wrap.className = "actions-row"; wrap.appendChild(btn);
  row.appendChild(wrap);
}

function renderChecklist() {
  const body = $("checklistBody");
  if (!currentDeal || !currentDeal.board) { body.innerHTML = "No deal board yet -- click Run."; return; }
  const missing = currentDeal.board.missing_documents;
  let html = "";
  for (const bucket of Object.keys(missing)) {
    html += `<div class="checklist-bucket"><h4>${escapeHtml(bucket.replace(/_/g, " "))}</h4><ul>`;
    if (missing[bucket].length === 0) html += "<li>(none outstanding)</li>";
    for (const item of missing[bucket]) html += `<li>${escapeHtml(item)}</li>`;
    html += "</ul></div>";
  }
  body.innerHTML = html;
}

function renderTechnical() {
  const body = $("technicalBody");
  if (!currentDeal) { body.textContent = "(nothing yet)"; return; }
  body.textContent = JSON.stringify({ state: {
    stage: currentDeal.stage, selected_lender: currentDeal.selected_lender,
    st_prep_recorded: currentDeal.st_prep_recorded, audit_log_tail: currentDeal.audit_log_tail,
  }, board: currentDeal.board }, null, 2);
}

function renderButtons() {
  const primary = $("primaryBtn");
  const doneNext = $("doneNextBtn");
  if (!currentDeal) { primary.disabled = true; doneNext.disabled = true; return; }
  primary.disabled = false;
  primary.textContent = currentDeal.primary_label;
  doneNext.disabled = !currentDeal.can_advance;
  doneNext.title = currentDeal.can_advance ? "" : "This step has a gate that isn't satisfied yet.";
}

async function withBusyButton(btn, fn) {
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "Working…";
  try { await fn(); }
  catch (e) { showToast(e.message); }
  finally { btn.disabled = false; btn.textContent = original; }
}

$("dealPicker").addEventListener("change", (e) => loadDeal(e.target.value));

$("loanTypePicker").addEventListener("change", async (e) => {
  if (!currentDeal) return;
  // Loan type is set at deal creation; changing it here is informational only.
});

$("primaryBtn").addEventListener("click", () => withBusyButton($("primaryBtn"), async () => {
  currentDeal = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/step_action`, { method: "POST" });
  render();
}));

$("doneNextBtn").addEventListener("click", () => withBusyButton($("doneNextBtn"), async () => {
  currentDeal = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/advance`, { method: "POST" });
  render();
  showToast(`Moved to stage '${currentDeal.stage}'.`);
}));

$("runBtn").addEventListener("click", () => withBusyButton($("runBtn"), async () => {
  if (!currentDeal) return;
  currentDeal = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/run`, { method: "POST" });
  render();
  showToast("Pipeline run complete.");
}));

$("openInboxBtn").addEventListener("click", async () => {
  if (!currentDeal) return;
  const res = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/open_inbox`, { method: "POST" });
  showToast(res.opened ? "Inbox folder opened." : (res.message || res.path));
});

$("askBtn").addEventListener("click", () => withBusyButton($("askBtn"), async () => {
  if (!currentDeal) return;
  const question = $("askBox").value.trim();
  if (!question) return;
  currentDeal = await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/ask`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }),
  });
  $("askBox").value = "";
  render();
}));

$("saveFileNoteBtn").addEventListener("click", () => withBusyButton($("saveFileNoteBtn"), async () => {
  if (!currentDeal || !currentDeal.assistant) return;
  await api(`/api/deal/${encodeURIComponent(currentDeal.name)}/file_note`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: currentDeal.assistant.reply }),
  });
  showToast("Saved as file note.");
}));

$("createDealBtn").addEventListener("click", () => withBusyButton($("createDealBtn"), async () => {
  const name = $("newDealName").value.trim();
  const deal_type = $("newDealType").value;
  if (!name) { showToast("Enter a deal name."); return; }
  await api("/api/deals", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, deal_type }) });
  $("newDealName").value = "";
  await refreshDealList(name);
  showToast("Deal created.");
}));

function handleFiles(fileList) {
  if (!currentDeal || !fileList || fileList.length === 0) return;
  const form = new FormData();
  for (const file of fileList) form.append("file", file, file.name);
  api(`/api/deal/${encodeURIComponent(currentDeal.name)}/upload`, { method: "POST", body: form })
    .then((data) => { showToast(`${data.saved.length} file(s) added to inbox.`); })
    .catch((e) => showToast(e.message));
}

const dz = $("dropzone");
dz.addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => handleFiles(e.target.files));
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("drag"); handleFiles(e.dataTransfer.files); });

populateSelect($("loanTypePicker"), DEAL_TYPES);
populateSelect($("newDealType"), DEAL_TYPES);
refreshDealList();
</script>
</body>
</html>
"""


def run_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), LoanAssistantHandler)
    return server


if __name__ == "__main__":
    httpd = run_server()
    print(f"Loan Assistant serving on http://{HOST}:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

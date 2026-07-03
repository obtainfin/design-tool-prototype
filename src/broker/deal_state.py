"""The 7-stage deal state machine, persisted atomically with an audit log.

Stages: intake -> servicing -> policy -> lender_select -> docs -> st_prep
-> submit. Only forward single-step transitions are allowed. Two gates:
a deal cannot enter ``docs`` without a selected lender, and cannot enter
``submit`` without Salestrekker prep recorded. Every mutation appends an
audit event ``{timestamp, event, actor, detail}`` and the whole state is
rewritten atomically (write to a temp file, then os.replace) so a crash
mid-write never corrupts ``deal_state.json``.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

STAGES = [
    "intake",
    "servicing",
    "policy",
    "lender_select",
    "docs",
    "st_prep",
    "submit",
]

STATE_FILENAME = "deal_state.json"
AUDIT_LOG_FILENAME = "audit_log.jsonl"


class InvalidTransitionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DealState:
    deal_name: str
    deal_type: str
    stage: str = STAGES[0]
    selected_lender: Optional[str] = None
    st_prep_recorded: bool = False
    compliance_reviewed_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    audit_log: List[Dict[str, Any]] = field(default_factory=list)

    # -- audit -----------------------------------------------------------

    def log(self, event: str, actor: str, detail: str = "") -> None:
        self.audit_log.append(
            {"timestamp": _now(), "event": event, "actor": actor, "detail": detail}
        )

    # -- transitions -------------------------------------------------------

    def _check_gate(self, target_stage: str) -> None:
        if target_stage == "docs" and not self.selected_lender:
            raise InvalidTransitionError(
                "Cannot enter 'docs' stage: select a lender first."
            )
        if target_stage == "submit" and not self.st_prep_recorded:
            raise InvalidTransitionError(
                "Cannot enter 'submit' stage: record Salestrekker prep first."
            )

    def can_advance(self) -> bool:
        idx = STAGES.index(self.stage)
        if idx >= len(STAGES) - 1:
            return False
        try:
            self._check_gate(STAGES[idx + 1])
        except InvalidTransitionError:
            return False
        return True

    def advance(self, actor: str = "broker", detail: str = "") -> str:
        idx = STAGES.index(self.stage)
        if idx >= len(STAGES) - 1:
            raise InvalidTransitionError("Deal is already at the final stage.")
        target = STAGES[idx + 1]
        self._check_gate(target)
        previous = self.stage
        self.stage = target
        self.log(
            "stage_advanced",
            actor,
            detail or f"Moved from '{previous}' to '{target}'",
        )
        return target

    def set_lender(self, lender_code: str, actor: str = "broker") -> None:
        self.selected_lender = lender_code
        self.log("lender_selected", actor, f"Selected lender {lender_code}")

    def record_st_prep(self, actor: str = "broker", detail: str = "Salestrekker comparison pack prepared") -> None:
        self.st_prep_recorded = True
        self.log("st_prep_recorded", actor, detail)

    def sign_off_nccp(self, control_ids: List[str], actor: str = "broker") -> None:
        """Broker self-attestation that they have reviewed the listed compliance
        controls. This never represents an AI conclusion — it only records that
        the broker (a human) says they reviewed these controls themselves.
        """
        for control_id in control_ids:
            if control_id not in self.compliance_reviewed_ids:
                self.compliance_reviewed_ids.append(control_id)
        self.log("nccp_signoff", actor, f"Broker signed off {len(control_ids)} compliance control(s).")

    # -- (de)serialisation -------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deal_name": self.deal_name,
            "deal_type": self.deal_type,
            "stage": self.stage,
            "selected_lender": self.selected_lender,
            "st_prep_recorded": self.st_prep_recorded,
            "compliance_reviewed_ids": self.compliance_reviewed_ids,
            "created_at": self.created_at,
            "audit_log": self.audit_log,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DealState":
        return cls(
            deal_name=data["deal_name"],
            deal_type=data["deal_type"],
            stage=data.get("stage", STAGES[0]),
            selected_lender=data.get("selected_lender"),
            st_prep_recorded=data.get("st_prep_recorded", False),
            compliance_reviewed_ids=data.get("compliance_reviewed_ids", []),
            created_at=data.get("created_at", _now()),
            audit_log=data.get("audit_log", []),
        )


def state_path(deal_dir: Path) -> Path:
    return Path(deal_dir) / STATE_FILENAME


def load(deal_dir: Path, deal_name: Optional[str] = None, deal_type: Optional[str] = None) -> DealState:
    """Load deal_state.json, or create a fresh intake-stage state if absent."""
    path = state_path(deal_dir)
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return DealState.from_dict(json.load(fh))
    if deal_name is None:
        deal_name = Path(deal_dir).name
    state = DealState(deal_name=deal_name, deal_type=deal_type or "payg_purchase")
    state.log("deal_created", "broker", f"Deal '{deal_name}' created")
    return state


def save(deal_dir: Path, state: DealState) -> None:
    """Atomically persist state to deal_state.json inside deal_dir.

    Also rewrites audit_log.jsonl (one JSON object per line, in order) so
    the audit trail is always readable independently of the state file.
    """
    deal_dir = Path(deal_dir)
    deal_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(deal_dir)
    fd, tmp_path = tempfile.mkstemp(prefix=".deal_state_", suffix=".tmp", dir=str(deal_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    audit_path = deal_dir / AUDIT_LOG_FILENAME
    audit_fd, audit_tmp = tempfile.mkstemp(prefix=".audit_log_", suffix=".tmp", dir=str(deal_dir))
    try:
        with os.fdopen(audit_fd, "w", encoding="utf-8") as fh:
            for event in state.audit_log:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        os.replace(audit_tmp, audit_path)
    finally:
        if os.path.exists(audit_tmp):
            os.remove(audit_tmp)


# Mapping from the 7 backend stages to the 5-step UI strip (Phase 2).
UI_STEPS = ["Triage", "Servicing & policy", "Docs", "Salestrekker", "Final check"]

_STAGE_TO_UI_INDEX = {
    "intake": 0,
    "servicing": 1,
    "policy": 1,
    "lender_select": 1,
    "docs": 2,
    "st_prep": 3,
    "submit": 4,
}


def ui_step_index(stage: str) -> int:
    return _STAGE_TO_UI_INDEX[stage]


def ui_step_name(stage: str) -> str:
    return UI_STEPS[ui_step_index(stage)]

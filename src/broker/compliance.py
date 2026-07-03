"""NCCP/BID compliance checklist.

BROKER MUST VERIFY: this control list is a reasonable default, not the
broker's aggregator audit document. See config/compliance_controls.json
and the UI note for the same warning surfaced to the broker.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "compliance_controls.json"

VERIFY_WARNING = (
    "Verify against your aggregator's current audit checklist before relying on this."
)


def load_config(config_path: Path = _CONFIG_PATH) -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def is_nccp_applicable(deal_type: str, config_path: Path = _CONFIG_PATH) -> bool:
    config = load_config(config_path)
    if deal_type in config.get("nccp_applicable_deal_types", []):
        return True
    return False


def build_compliance_checklist(
    deal_type: str,
    combined_text: str,
    reviewed_ids: Optional[Iterable[str]] = None,
    config_path: Path = _CONFIG_PATH,
) -> Dict[str, Any]:
    """Build the NCCP/BID audit checklist structure for a deal.

    ``combined_text`` is the concatenation of filed document text and file
    notes, used only to detect keyword evidence of a control (never proof
    that the control is actually satisfied — that always needs broker sign-off).
    """
    config = load_config(config_path)
    reviewed = set(reviewed_ids or [])
    applicable = deal_type in config.get("nccp_applicable_deal_types", [])
    text_lower = (combined_text or "").lower()

    controls: List[Dict[str, Any]] = []
    for control in config.get("controls", []):
        control_id = control["id"]
        if control_id in reviewed:
            status = "reviewed"
        elif any(kw.lower() in text_lower for kw in control.get("keywords", [])):
            status = "evidence_found_review_required"
        else:
            status = "missing"
        controls.append(
            {
                "id": control_id,
                "name": control["name"],
                "status": status,
                "broker_action": control["broker_action"],
            }
        )

    return {
        "applicable": applicable,
        "applicability_note": (
            "NCCP applies to this consumer/residential deal type."
            if applicable
            else "Exempt by default for this deal type (commercial, SMSF, company/trust, "
                 "business lending) — broker to confirm."
        ),
        "verify_warning": VERIFY_WARNING,
        "controls": controls,
    }

"""Deal board construction and the Salestrekker comparison pack."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .extraction import FIELD_SECTIONS, FieldEvidence

BOARD_JSON_FILENAME = "deal_board.json"
BOARD_MD_FILENAME = "deal_board.md"

CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

ST_COMPARISON_HEADER = (
    "Compare only. Do not enter or save anything in Salestrekker without broker "
    "approval per field. Verify client identity with name plus one more "
    "identifier before touching any record."
)

ST_SECTION_ORDER = ["client", "loan", "employment_income", "liabilities", "security_property"]


def build_deal_board(
    state,
    evidence_pool: Dict[str, List[FieldEvidence]],
    conflicts: List[Dict[str, Any]],
    missing_docs: Dict[str, List[str]],
    compliance_result: Dict[str, Any],
    doc_records: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "deal_name": state.deal_name,
        "deal_type": state.deal_type,
        "stage": state.stage,
        "selected_lender": state.selected_lender,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fields": {
            field: [evidence.to_dict() for evidence in evidences]
            for field, evidences in evidence_pool.items()
        },
        "conflicts": conflicts,
        "missing_documents": missing_docs,
        "compliance": compliance_result,
        "documents": doc_records,
        "warnings": warnings,
    }


def _best_evidence(evidences: List[FieldEvidence]) -> FieldEvidence:
    return min(evidences, key=lambda e: CONFIDENCE_RANK.get(e.confidence, 2))


def render_deal_board_markdown(board: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Deal board: {board['deal_name']}")
    lines.append("")
    lines.append(f"- Deal type: {board['deal_type']}")
    lines.append(f"- Stage: {board['stage']}")
    lines.append(f"- Selected lender: {board['selected_lender'] or '(none yet)'}")
    lines.append(f"- Generated: {board['generated_at']}")
    lines.append("")

    lines.append("## Missing documents")
    for bucket, items in board["missing_documents"].items():
        lines.append(f"### {bucket.replace('_', ' ')}")
        if not items:
            lines.append("- (none outstanding)")
        for item in items:
            lines.append(f"- {item}")
    lines.append("")

    lines.append("## Conflicts requiring review")
    if not board["conflicts"]:
        lines.append("- (none detected)")
    for conflict in board["conflicts"]:
        lines.append(f"- **{conflict['field']}**: {conflict['message']}")
        for src in conflict["sources"]:
            lines.append(f"  - {src['source_document']}: {src['value']}")
    lines.append("")

    lines.append("## Compliance (NCCP/BID)")
    lines.append(f"> {board['compliance']['verify_warning']}")
    lines.append(f"- {board['compliance']['applicability_note']}")
    for control in board["compliance"]["controls"]:
        lines.append(f"- [{control['status']}] {control['name']}")
    lines.append("")

    lines.append("## Documents processed")
    for doc in board["documents"]:
        lines.append(f"- {doc['filename']} -> {doc['document_type']} ({doc['confidence']})")
        for warning in doc.get("warnings", []):
            lines.append(f"  - warning: {warning}")
    lines.append("")

    if board["warnings"]:
        lines.append("## Warnings")
        for warning in board["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Extracted fields")
    for section, field_names in FIELD_SECTIONS.items():
        section_rows = [f"{section}.{name}" for name in field_names if f"{section}.{name}" in board["fields"]]
        if not section_rows:
            continue
        lines.append(f"### {section.replace('_', ' ')}")
        for full_field in section_rows:
            evidences = board["fields"][full_field]
            best = min(evidences, key=lambda e: CONFIDENCE_RANK.get(e["confidence"], 2))
            flag = " (review required)" if best["manual_review_required"] else ""
            lines.append(
                f"- {full_field}: {best['value']} — {best['source_document']}, "
                f"{best['confidence']} confidence{flag}"
            )
        lines.append("")

    return "\n".join(lines)


def write_deal_board(outputs_dir: Path, board: Dict[str, Any]) -> None:
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    with open(outputs_dir / BOARD_JSON_FILENAME, "w", encoding="utf-8") as fh:
        json.dump(board, fh, indent=2, ensure_ascii=False)
    with open(outputs_dir / BOARD_MD_FILENAME, "w", encoding="utf-8") as fh:
        fh.write(render_deal_board_markdown(board))


# -- Salestrekker comparison pack (Phase 3) -----------------------------------


def build_st_comparison(evidence_pool: Dict[str, List[FieldEvidence]]) -> List[Dict[str, Any]]:
    """Build ordered comparison rows: client, loan, income, liabilities, security."""
    rows: List[Dict[str, Any]] = []
    for section in ST_SECTION_ORDER:
        for field_name in FIELD_SECTIONS.get(section, []):
            full_field = f"{section}.{field_name}"
            evidences = evidence_pool.get(full_field)
            if not evidences:
                continue
            best = _best_evidence(evidences)
            rows.append(
                {
                    "field": full_field,
                    "value": best.value,
                    "source_document": best.source_document,
                    "quote": best.quote,
                    "confidence": best.confidence,
                    "review_required": best.confidence != "high",
                }
            )
    return rows


def render_st_comparison_text(rows: List[Dict[str, Any]]) -> str:
    lines = [ST_COMPARISON_HEADER, ""]
    if not rows:
        lines.append("(no evidenced fields yet)")
        return "\n".join(lines)
    field_width = max(len(r["field"]) for r in rows) + 2
    value_width = max(len(r["value"]) for r in rows) + 2
    for row in rows:
        flag = " [REVIEW]" if row["review_required"] else ""
        lines.append(
            f"{row['field']:<{field_width}}{row['value']:<{value_width}}"
            f"src: {row['source_document']:<24} conf: {row['confidence']:<7}{flag}"
        )
        if row["quote"]:
            lines.append(f"{'':<{field_width}}\"{row['quote']}\"")
    return "\n".join(lines)

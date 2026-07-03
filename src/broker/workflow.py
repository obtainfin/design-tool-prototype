"""The Run pipeline: read inbox docs -> classify -> AI-read -> extract/merge ->
detect conflicts -> missing docs -> compliance -> build deal board -> file
copies -> audit log.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import board as board_mod
from . import checklists, compliance, deal_state
from . import extraction
from . import text_extractors
from .ai_client import AIClient
from .classifier import Classifier
from .extraction import FieldEvidence

DOCUMENTS_DIRNAME = "Documents"
INBOX_DIRNAME = "Inbox"
FILED_DIRNAME = "Filed"
NOTES_DIRNAME = "Notes"
OUTPUTS_DIRNAME = "outputs"
FILE_NOTES_FILENAME = "file_notes.md"
EXTRA_EVIDENCE_FILENAME = "extra_evidence.jsonl"

_INVALID_FOLDER_CHARS = re.compile(r'[\\/:*?"<>|]')


def _safe_category(document_type: str) -> str:
    return _INVALID_FOLDER_CHARS.sub("-", document_type).strip() or "unknown document"


def deal_paths(deal_dir: Path) -> Dict[str, Path]:
    deal_dir = Path(deal_dir)
    return {
        "deal_dir": deal_dir,
        "inbox": deal_dir / DOCUMENTS_DIRNAME / INBOX_DIRNAME,
        "filed": deal_dir / DOCUMENTS_DIRNAME / FILED_DIRNAME,
        "notes": deal_dir / NOTES_DIRNAME,
        "outputs": deal_dir / OUTPUTS_DIRNAME,
    }


def ensure_deal_folders(deal_dir: Path) -> Dict[str, Path]:
    paths = deal_paths(deal_dir)
    for key in ("inbox", "filed", "notes", "outputs"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def append_extra_evidence(deal_dir: Path, evidence_list: List[FieldEvidence]) -> None:
    """Persist evidence that didn't come from an Inbox document (e.g. a call
    note), so it survives and re-merges on every subsequent Run.
    """
    if not evidence_list:
        return
    notes_dir = deal_paths(deal_dir)["notes"]
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / EXTRA_EVIDENCE_FILENAME
    with open(path, "a", encoding="utf-8") as fh:
        for evidence in evidence_list:
            fh.write(json.dumps(evidence.to_dict(), ensure_ascii=False) + "\n")


def load_extra_evidence(deal_dir: Path) -> List[FieldEvidence]:
    path = deal_paths(deal_dir)["notes"] / EXTRA_EVIDENCE_FILENAME
    if not path.exists():
        return []
    evidence: List[FieldEvidence] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        evidence.append(FieldEvidence(**data))
    return evidence


def append_file_note(deal_dir: Path, text: str, actor: str = "broker", heading: Optional[str] = None) -> None:
    """Append a timestamped note to Notes/file_notes.md."""
    notes_dir = deal_paths(deal_dir)["notes"]
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / FILE_NOTES_FILENAME
    timestamp = datetime.now(timezone.utc).isoformat()
    title = heading or f"Note — {timestamp}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"## {title}\n\n{text.strip()}\n\n")


def _process_document(
    doc_path: Path, classifier: Classifier, ai_client: AIClient
) -> Dict[str, Any]:
    extraction_result = text_extractors.extract_text(doc_path)
    text = extraction_result.text
    doc_warnings = list(extraction_result.warnings)

    classification = classifier.classify(doc_path.name, text)
    regex_evidence = extraction.extract_regex_fields(text, doc_path.name)

    ai_doc_type = ai_doc_confidence = None
    ai_evidence: List[FieldEvidence] = []
    ai_failure: Optional[str] = None
    if ai_client.enabled:
        ai_doc_type, ai_doc_confidence, ai_evidence, ai_failure = extraction.ai_extract_document(
            ai_client, text, doc_path.name
        )
        if ai_failure:
            doc_warnings.append(f"AI extraction failed: {ai_failure}")

    final_type, final_confidence = extraction.reconcile_document_type(
        classification.document_type, classification.confidence, ai_doc_type, ai_doc_confidence
    )

    return {
        "filename": doc_path.name,
        "text": text,
        "document_type": final_type,
        "confidence": final_confidence,
        "warnings": doc_warnings,
        "evidence": regex_evidence + ai_evidence,
        "ai_failure": ai_failure,
    }


def run(deal_dir: Path, deal_type: Optional[str] = None, ai_client: Optional[AIClient] = None) -> Dict[str, Any]:
    """Run the full pipeline for one deal and write deal_board.json/.md."""
    deal_dir = Path(deal_dir)
    paths = ensure_deal_folders(deal_dir)
    state = deal_state.load(deal_dir, deal_name=deal_dir.name, deal_type=deal_type)
    if deal_type and state.deal_type != deal_type:
        state.deal_type = deal_type

    if ai_client is None:
        ai_client = AIClient()
    classifier = Classifier()

    all_evidence: List[FieldEvidence] = []
    doc_records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    combined_text_parts: List[str] = []

    inbox_files = sorted(p for p in paths["inbox"].iterdir() if p.is_file())
    for doc_path in inbox_files:
        result = _process_document(doc_path, classifier, ai_client)
        combined_text_parts.append(result["text"])
        all_evidence.extend(result["evidence"])
        doc_records.append(
            {
                "filename": result["filename"],
                "document_type": result["document_type"],
                "confidence": result["confidence"],
                "warnings": result["warnings"],
            }
        )
        warnings.extend(f"{result['filename']}: {w}" for w in result["warnings"])

        category_dir = paths["filed"] / _safe_category(result["document_type"])
        category_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(doc_path, category_dir / doc_path.name)
        rel_dest = (category_dir / doc_path.name).relative_to(deal_dir)
        state.log("document_filed", "system", f"Filed {doc_path.name} as '{result['document_type']}' -> {rel_dest}")
        if result["ai_failure"]:
            state.log("ai_extraction_failed", "system", f"{doc_path.name}: {result['ai_failure']}")

    all_evidence.extend(load_extra_evidence(deal_dir))

    evidence_pool = extraction.merge_evidence(all_evidence)
    conflicts = extraction.detect_conflicts(evidence_pool)

    present_types = {rec["document_type"] for rec in doc_records}
    missing_docs = checklists.missing_documents(state.deal_type, present_types)

    file_notes_path = paths["notes"] / FILE_NOTES_FILENAME
    if file_notes_path.exists():
        combined_text_parts.append(file_notes_path.read_text(encoding="utf-8"))
    combined_text = "\n".join(combined_text_parts)
    compliance_result = compliance.build_compliance_checklist(
        state.deal_type, combined_text, reviewed_ids=state.compliance_reviewed_ids
    )

    board = board_mod.build_deal_board(
        state=state,
        evidence_pool=evidence_pool,
        conflicts=conflicts,
        missing_docs=missing_docs,
        compliance_result=compliance_result,
        doc_records=doc_records,
        warnings=warnings,
    )
    board_mod.write_deal_board(paths["outputs"], board)

    state.log(
        "run_completed",
        "system",
        f"Processed {len(doc_records)} document(s); {len(conflicts)} conflict(s); "
        f"{sum(len(v) for v in missing_docs.values())} missing item(s) across all buckets.",
    )
    deal_state.save(deal_dir, state)
    return board

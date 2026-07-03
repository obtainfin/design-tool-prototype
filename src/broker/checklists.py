"""Per-deal-type document checklists, loaded from config/checklists.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "checklists.json"

BUCKETS = ["required_now", "required_before_submission", "likely_lender_specific", "nice_to_have"]


def load_checklists(config_path: Path = _CONFIG_PATH) -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_checklist(deal_type: str, config_path: Path = _CONFIG_PATH) -> Dict[str, List[dict]]:
    config = load_checklists(config_path)
    return config["deal_types"].get(deal_type, {bucket: [] for bucket in BUCKETS})


def missing_documents(
    deal_type: str, present_document_types: Iterable[str], config_path: Path = _CONFIG_PATH
) -> Dict[str, List[str]]:
    """Return, per bucket, the checklist items not yet covered by a filed/classified document.

    An item with a non-empty ``matches`` list is considered satisfied once any
    document of one of those classifier types is present. Items with no
    ``matches`` (lender-specific forms the classifier can't recognise) always
    show as outstanding until the broker confirms them manually.
    """
    checklist = get_checklist(deal_type, config_path)
    present = set(present_document_types)
    missing: Dict[str, List[str]] = {}
    for bucket in BUCKETS:
        items = checklist.get(bucket, [])
        outstanding = []
        for item in items:
            matches = item.get("matches", [])
            if matches and present & set(matches):
                continue
            outstanding.append(item["item"])
        missing[bucket] = outstanding
    return missing

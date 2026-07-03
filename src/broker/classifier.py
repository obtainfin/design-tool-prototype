"""Keyword-based document classification.

Loads ``config/classification_keywords.json`` and scores a document's
filename and extracted text against each known document type. Filename
hits are weighted 3x text hits so an obviously-named file ("payslip.pdf")
wins even if its body text is sparse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

FILENAME_WEIGHT = 3
TEXT_WEIGHT = 1

UNKNOWN_TYPE = "unknown document"

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "classification_keywords.json"


@dataclass
class ClassificationResult:
    document_type: str
    confidence: str  # "high" | "medium" | "low"
    score: int
    scores: Dict[str, int]


def _load_config(config_path: Path = _CONFIG_PATH) -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Classifier:
    def __init__(self, config_path: Path = _CONFIG_PATH):
        self._config = _load_config(config_path)
        self.document_types: List[str] = list(self._config["document_types"].keys())

    def classify(self, filename: str, text: str) -> ClassificationResult:
        filename_l = filename.lower()
        text_l = text.lower()
        scores: Dict[str, int] = {}
        for doc_type, keywords in self._config["document_types"].items():
            if doc_type == UNKNOWN_TYPE:
                continue
            score = 0
            for kw in keywords.get("filename_keywords", []):
                if kw.lower() in filename_l:
                    score += FILENAME_WEIGHT
            for kw in keywords.get("text_keywords", []):
                if kw.lower() in text_l:
                    score += TEXT_WEIGHT
            if score:
                scores[doc_type] = score

        if not scores:
            return ClassificationResult(UNKNOWN_TYPE, "low", 0, scores)

        best_type = max(scores, key=lambda k: (scores[k], -list(scores.keys()).index(k)))
        best_score = scores[best_type]
        if best_score >= 6:
            confidence = "high"
        elif best_score >= 3:
            confidence = "medium"
        else:
            confidence = "low"
        return ClassificationResult(best_type, confidence, best_score, scores)

"""The extracted-field schema plus the regex and AI evidence pipelines.

Every value pulled from a document becomes a ``FieldEvidence``: a value,
its source document, a confidence level, the reason for that confidence, a
supporting quote, whether it needs manual review, and whether it came from
regex or AI. Regex and AI evidence for the same field share one pool, so
conflict detection and the review queue see both uniformly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional, Tuple

from . import security
from .ai_client import AIClient, AIRequestError
from .classifier import UNKNOWN_TYPE, Classifier

CONFIDENCE_LEVELS = ("high", "medium", "low")

AI_TEXT_CHAR_CAP = 20_000

# -- field schema ------------------------------------------------------------

FIELD_SECTIONS: Dict[str, List[str]] = {
    "client": ["names", "dob", "contact", "address", "dependants", "residency", "marital_status"],
    "employment_income": [
        "type", "employer", "base_income", "overtime", "bonus", "commission",
        "allowances", "rental_income", "business_income", "trust_income", "dividends",
    ],
    "self_employed_company_trust": [
        "abn_acn", "entity_names", "accountant", "revenue", "net_profit",
        "addbacks", "ato_debt", "ato_payment_plan",
    ],
    "smsf": ["fund_name", "trustee", "members", "balance", "contributions", "liquidity", "bare_trust"],
    "liabilities": [
        "home_loan", "investment_loan", "commercial_loan", "business_loan",
        "personal_loan", "car_loan", "card_limits", "hecs", "ato_debt", "bnpl", "leases",
    ],
    "security_property": [
        "address", "purchase_price", "value", "existing_debt", "ownership", "zoning", "type", "rental",
    ],
    "loan": [
        "purpose", "amount", "refinance_cash_out", "deposit", "term",
        "repayment_type", "proposed_lender", "proposed_product", "lvr",
    ],
    "fact_find_objectives": ["objectives", "features", "fixed_variable", "timeframe", "future_plans"],
    "servicing_targets": ["gross_income_annual", "base_income_noa", "declared_living_expenses"],
}

ALLOWED_FIELDS: List[str] = [
    f"{section}.{name}" for section, names in FIELD_SECTIONS.items() for name in names
]
ALLOWED_FIELDS_SET = set(ALLOWED_FIELDS)

# Sensitive fields: capped at medium confidence, always review-required.
SENSITIVE_FIELDS = {"client.names", "client.dob", "client.address"}

# -- FieldEvidence -----------------------------------------------------------


@dataclass
class FieldEvidence:
    field: str
    value: str
    source_document: str
    confidence: str
    confidence_reason: str
    quote: str
    manual_review_required: bool
    category: str  # "regex" | "ai" | "call note"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "source_document": self.source_document,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "quote": self.quote,
            "manual_review_required": self.manual_review_required,
            "category": self.category,
        }


def normalize_confidence(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() in CONFIDENCE_LEVELS:
        return value.strip().lower()
    return "low"


def _cap_sensitive(field_name: str, confidence: str) -> Tuple[str, bool]:
    if field_name in SENSITIVE_FIELDS:
        if confidence == "high":
            confidence = "medium"
        return confidence, True
    return confidence, confidence == "low"


# -- regex extraction ---------------------------------------------------------

# field -> (label phrases, value type: "money" | "percent" | "date" | "text")
FIELD_PATTERNS: Dict[str, Tuple[List[str], str]] = {
    "client.names": (["client name", "applicant name", "borrower name", "full name"], "text"),
    "client.dob": (["date of birth", "dob"], "date"),
    "client.contact": (["mobile", "phone", "email", "contact number"], "text"),
    "client.address": (["residential address", "home address", "client address"], "text"),
    "client.dependants": (["dependants", "dependents"], "text"),
    "client.residency": (["residency status", "residency"], "text"),
    "client.marital_status": (["marital status"], "text"),

    "employment_income.type": (["employment type"], "text"),
    "employment_income.employer": (["employer name", "employer"], "text"),
    "employment_income.base_income": (["base income", "base salary"], "money"),
    "employment_income.overtime": (["overtime"], "money"),
    "employment_income.bonus": (["bonus"], "money"),
    "employment_income.commission": (["commission"], "money"),
    "employment_income.allowances": (["allowances"], "money"),
    "employment_income.rental_income": (["rental income"], "money"),
    "employment_income.business_income": (["business income"], "money"),
    "employment_income.trust_income": (["trust distribution", "trust income"], "money"),
    "employment_income.dividends": (["dividend income", "dividends"], "money"),

    "self_employed_company_trust.abn_acn": (["abn", "acn"], "text"),
    "self_employed_company_trust.entity_names": (["entity name", "trading name", "company name"], "text"),
    "self_employed_company_trust.accountant": (["accountant"], "text"),
    "self_employed_company_trust.revenue": (["revenue", "turnover"], "money"),
    "self_employed_company_trust.net_profit": (["net profit"], "money"),
    "self_employed_company_trust.addbacks": (["addbacks", "add-backs"], "money"),
    "self_employed_company_trust.ato_debt": (["ato debt"], "money"),
    "self_employed_company_trust.ato_payment_plan": (["ato payment plan"], "text"),

    "smsf.fund_name": (["fund name", "smsf name"], "text"),
    "smsf.trustee": (["trustee"], "text"),
    "smsf.members": (["members", "member names"], "text"),
    "smsf.balance": (["fund balance", "smsf balance"], "money"),
    "smsf.contributions": (["contributions"], "money"),
    "smsf.liquidity": (["liquidity"], "text"),
    "smsf.bare_trust": (["bare trust"], "text"),

    "liabilities.home_loan": (["home loan"], "money"),
    "liabilities.investment_loan": (["investment loan"], "money"),
    "liabilities.commercial_loan": (["commercial loan"], "money"),
    "liabilities.business_loan": (["business loan"], "money"),
    "liabilities.personal_loan": (["personal loan"], "money"),
    "liabilities.car_loan": (["car loan"], "money"),
    "liabilities.card_limits": (["credit card limit", "card limit"], "money"),
    "liabilities.hecs": (["hecs", "help debt"], "money"),
    "liabilities.ato_debt": (["ato debt"], "money"),
    "liabilities.bnpl": (["afterpay", "zip pay", "bnpl"], "money"),
    "liabilities.leases": (["lease repayment", "leases"], "money"),

    "security_property.address": (["security property address", "property address"], "text"),
    "security_property.purchase_price": (["purchase price"], "money"),
    "security_property.value": (["estimated value", "property value"], "money"),
    "security_property.existing_debt": (["existing debt"], "money"),
    "security_property.ownership": (["ownership"], "text"),
    "security_property.zoning": (["zoning"], "text"),
    "security_property.type": (["property type"], "text"),
    "security_property.rental": (["weekly rent", "rental appraisal"], "money"),

    "loan.purpose": (["loan purpose"], "text"),
    "loan.amount": (["loan amount"], "money"),
    "loan.refinance_cash_out": (["cash out amount", "refinance amount"], "money"),
    "loan.deposit": (["deposit"], "money"),
    "loan.term": (["loan term"], "text"),
    "loan.repayment_type": (["repayment type"], "text"),
    "loan.proposed_lender": (["proposed lender"], "text"),
    "loan.proposed_product": (["proposed product"], "text"),
    "loan.lvr": (["lvr"], "percent"),

    "fact_find_objectives.objectives": (["client objectives", "objectives"], "text"),
    "fact_find_objectives.features": (["required features"], "text"),
    "fact_find_objectives.fixed_variable": (["fixed or variable", "rate preference"], "text"),
    "fact_find_objectives.timeframe": (["timeframe"], "text"),
    "fact_find_objectives.future_plans": (["future plans"], "text"),

    "servicing_targets.gross_income_annual": (["gross annual income", "gross income"], "money"),
    "servicing_targets.base_income_noa": (["noa assessed income", "assessed taxable income"], "money"),
    "servicing_targets.declared_living_expenses": (["declared living expenses", "living expenses"], "money"),
}

_VALUE_REGEX_BY_TYPE = {
    "money": r"\$?\s?\d[\d,]*(?:\.\d{1,2})?",
    "percent": r"\d{1,3}(?:\.\d+)?\s?%",
    "date": r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
    "text": r"[^\n\r]{1,120}?",
}

_TYPE_CONFIDENCE = {"money": "high", "percent": "high", "date": "high", "text": "medium"}


def _compile_field_pattern(labels: List[str], value_type: str) -> "re.Pattern":
    label_alt = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    value_re = _VALUE_REGEX_BY_TYPE[value_type]
    return re.compile(rf"(?i)\b(?:{label_alt})\b\s*[:\-]?\s*(?P<value>{value_re})(?=\s|$|[,.;])")


_COMPILED_PATTERNS = {
    field_name: _compile_field_pattern(labels, value_type)
    for field_name, (labels, value_type) in FIELD_PATTERNS.items()
}


def extract_regex_fields(text: str, source_document: str) -> List[FieldEvidence]:
    """Baseline offline extraction: one labelled-value regex per field."""
    results: List[FieldEvidence] = []
    for field_name, (_labels, value_type) in FIELD_PATTERNS.items():
        pattern = _COMPILED_PATTERNS[field_name]
        match = pattern.search(text)
        if not match:
            continue
        value = match.group("value").strip().rstrip(",;:")
        if not value:
            continue
        quote = match.group(0).strip()[:180]
        confidence = _TYPE_CONFIDENCE[value_type]
        confidence, manual_review = _cap_sensitive(field_name, confidence)
        results.append(
            FieldEvidence(
                field=field_name,
                value=value,
                source_document=source_document,
                confidence=confidence,
                confidence_reason="Matched a labelled value in the document text (regex baseline).",
                quote=quote,
                manual_review_required=manual_review,
                category="regex",
            )
        )
    return results


# -- AI extraction -------------------------------------------------------------

_classifier_singleton: Optional[Classifier] = None


def _document_types() -> List[str]:
    global _classifier_singleton
    if _classifier_singleton is None:
        _classifier_singleton = Classifier()
    return _classifier_singleton.document_types


def build_ai_instructions() -> str:
    doc_types = ", ".join(f'"{t}"' for t in _document_types())
    allowed = ", ".join(ALLOWED_FIELDS)
    return (
        "You are extracting structured loan-file data from ONE source document for a "
        "mortgage broker's supervised assistant tool. Return ONLY valid JSON, no prose, "
        "no markdown fences, matching exactly this shape:\n"
        '{"document_type": "<one of the allowed types>", "document_confidence": '
        '"high|medium|low", "fields": [{"name": "section.field", "value": "...", '
        '"quote": "<=20 words from the source text", "confidence": "high|medium|low"}]}\n\n'
        f"Allowed document types: {doc_types}. Use \"unknown document\" if unsure.\n"
        f"Allowed field names (use ONLY these, in section.field form): {allowed}.\n"
        "Rules: only extract values explicitly stated in the text; never guess or infer; "
        "quote must be a short verbatim excerpt of 20 words or fewer supporting the value; "
        "dollar amounts, dates, names and balances may all be extracted, they are the work. "
        "This is a draft extraction for broker review only — it is not a final assessment "
        "of income, servicing, credit, or loan eligibility, and must not be phrased as one."
    )


def parse_ai_reply(raw_text: str) -> Dict[str, Any]:
    """Tolerantly parse the model's JSON reply, stripping markdown fences if present."""
    if not raw_text:
        return {}
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text.strip())
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def clamp_ai_reply(parsed: Dict[str, Any], source_document: str) -> Tuple[str, str, List[FieldEvidence]]:
    """Clamp a parsed AI reply: allowlisted fields only, size limits, normalised confidences."""
    doc_type = parsed.get("document_type")
    if not isinstance(doc_type, str) or doc_type not in _document_types():
        doc_type = UNKNOWN_TYPE
    doc_confidence = normalize_confidence(parsed.get("document_confidence"))

    fields_out: List[FieldEvidence] = []
    raw_fields = parsed.get("fields")
    if isinstance(raw_fields, list):
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name not in ALLOWED_FIELDS_SET:
                continue
            value = item.get("value")
            if value is None:
                continue
            value = str(value).strip()
            if not value or len(value) > 180:
                continue
            quote = item.get("quote", "")
            quote = " ".join(str(quote).split()[:20]) if quote else ""
            confidence = normalize_confidence(item.get("confidence"))
            confidence, manual_review = _cap_sensitive(name, confidence)
            fields_out.append(
                FieldEvidence(
                    field=name,
                    value=value,
                    source_document=source_document,
                    confidence=confidence,
                    confidence_reason="AI extraction from document text (broker must verify).",
                    quote=quote,
                    manual_review_required=manual_review,
                    category="ai",
                )
            )
    return doc_type, doc_confidence, fields_out


def ai_extract_document(
    client: AIClient, text: str, source_document: str
) -> Tuple[Optional[str], Optional[str], List[FieldEvidence], Optional[str]]:
    """Run one document through the AI pipeline. Never raises; returns a warning instead."""
    if not client.enabled:
        return None, None, [], None
    stripped = security.strip_tax_identifiers(text)
    capped = stripped[:AI_TEXT_CHAR_CAP]
    try:
        raw = client.respond(build_ai_instructions(), capped)
    except AIRequestError as exc:
        return None, None, [], str(exc)
    parsed = parse_ai_reply(raw)
    doc_type, doc_confidence, fields = clamp_ai_reply(parsed, source_document)
    return doc_type, doc_confidence, fields, None


def reconcile_document_type(
    local_type: str, local_confidence: str, ai_type: Optional[str], ai_confidence: Optional[str]
) -> Tuple[str, str]:
    """AI doc type overrides the keyword guess only when the guess is weak or AI is confident."""
    if ai_type is None:
        return local_type, local_confidence
    if local_type == ai_type:
        if local_confidence == "high" and ai_confidence == "high":
            return local_type, "high"
        return local_type, local_confidence
    if local_type == UNKNOWN_TYPE or local_confidence == "low":
        return ai_type, ai_confidence or "low"
    if ai_confidence == "high":
        return ai_type, "high"
    return local_type, local_confidence


# -- merge + conflict detection ------------------------------------------------


def merge_evidence(evidence_list: List[FieldEvidence]) -> Dict[str, List[FieldEvidence]]:
    """Group evidence (regex, AI, call notes alike) into one pool per field."""
    pool: Dict[str, List[FieldEvidence]] = {}
    for evidence in evidence_list:
        pool.setdefault(evidence.field, []).append(evidence)
    return pool


def _normalize_for_compare(field_name: str, value: str) -> str:
    value_type = FIELD_PATTERNS.get(field_name, ([], "text"))[1]
    if value_type == "money":
        digits = re.sub(r"[^\d.]", "", value)
        if digits:
            try:
                return f"{float(digits):.2f}"
            except ValueError:
                pass
        return value.strip().lower()
    return re.sub(r"\s+", " ", value.strip().lower())


def detect_conflicts(pool: Dict[str, List[FieldEvidence]]) -> List[Dict[str, Any]]:
    """Raise a review issue naming both sources when two sources disagree on a field."""
    issues: List[Dict[str, Any]] = []
    for field_name, evidences in pool.items():
        first_value_by_source: Dict[str, Tuple[str, str]] = {}
        for evidence in evidences:
            if evidence.source_document in first_value_by_source:
                continue
            norm = _normalize_for_compare(field_name, evidence.value)
            first_value_by_source[evidence.source_document] = (evidence.value, norm)
        distinct_norms = {norm for _value, norm in first_value_by_source.values()}
        if len(distinct_norms) > 1:
            issues.append(
                {
                    "field": field_name,
                    "message": f"Conflicting values found for {field_name} across sources.",
                    "sources": [
                        {"source_document": src, "value": value}
                        for src, (value, _norm) in first_value_by_source.items()
                    ],
                }
            )
    return issues

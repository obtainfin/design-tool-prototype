import unittest

import _pathfix  # noqa: F401

from src.broker import extraction
from src.broker.ai_client import AIClient
from src.broker.classifier import UNKNOWN_TYPE


class TestRegexExtraction(unittest.TestCase):
    def test_extracts_labelled_money_value(self):
        text = "Base Income: $85,000 per annum, reviewed annually."
        evidence = extraction.extract_regex_fields(text, "payslip.pdf")
        by_field = {e.field: e for e in evidence}
        self.assertIn("employment_income.base_income", by_field)
        self.assertEqual(by_field["employment_income.base_income"].value, "$85,000")
        self.assertEqual(by_field["employment_income.base_income"].confidence, "high")
        self.assertEqual(by_field["employment_income.base_income"].category, "regex")

    def test_sensitive_field_capped_at_medium_and_review_required(self):
        text = "Date of Birth: 12/05/1985"
        evidence = extraction.extract_regex_fields(text, "id.pdf")
        by_field = {e.field: e for e in evidence}
        self.assertIn("client.dob", by_field)
        self.assertEqual(by_field["client.dob"].confidence, "medium")
        self.assertTrue(by_field["client.dob"].manual_review_required)

    def test_no_match_returns_no_evidence_for_field(self):
        evidence = extraction.extract_regex_fields("nothing relevant here", "doc.pdf")
        self.assertEqual(evidence, [])


class TestAIReplyClamping(unittest.TestCase):
    def test_clamp_drops_disallowed_field(self):
        parsed = {
            "document_type": "payslip",
            "document_confidence": "high",
            "fields": [{"name": "not.a.real.field", "value": "x", "quote": "q", "confidence": "high"}],
        }
        doc_type, doc_conf, fields = extraction.clamp_ai_reply(parsed, "payslip.pdf")
        self.assertEqual(doc_type, "payslip")
        self.assertEqual(doc_conf, "high")
        self.assertEqual(fields, [])

    def test_clamp_drops_empty_and_overlong_values(self):
        parsed = {
            "document_type": "payslip",
            "document_confidence": "medium",
            "fields": [
                {"name": "employment_income.base_income", "value": "", "quote": "q", "confidence": "high"},
                {"name": "employment_income.employer", "value": "x" * 181, "quote": "q", "confidence": "high"},
                {"name": "employment_income.employer", "value": "Acme Pty Ltd", "quote": "q", "confidence": "high"},
            ],
        }
        _doc_type, _doc_conf, fields = extraction.clamp_ai_reply(parsed, "payslip.pdf")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].value, "Acme Pty Ltd")

    def test_clamp_normalises_bad_confidence(self):
        parsed = {
            "document_type": "payslip",
            "document_confidence": "extremely-sure",
            "fields": [{"name": "employment_income.employer", "value": "Acme", "quote": "q", "confidence": "super-high"}],
        }
        doc_type, doc_conf, fields = extraction.clamp_ai_reply(parsed, "payslip.pdf")
        self.assertEqual(doc_conf, "low")
        self.assertEqual(fields[0].confidence, "low")

    def test_clamp_unknown_document_type(self):
        parsed = {"document_type": "not a real type", "document_confidence": "high", "fields": []}
        doc_type, _doc_conf, _fields = extraction.clamp_ai_reply(parsed, "doc.pdf")
        self.assertEqual(doc_type, UNKNOWN_TYPE)

    def test_clamp_caps_sensitive_field_confidence(self):
        parsed = {
            "document_type": "ID document",
            "document_confidence": "high",
            "fields": [{"name": "client.address", "value": "1 Example St", "quote": "q", "confidence": "high"}],
        }
        _doc_type, _doc_conf, fields = extraction.clamp_ai_reply(parsed, "id.pdf")
        self.assertEqual(fields[0].confidence, "medium")
        self.assertTrue(fields[0].manual_review_required)

    def test_clamp_quote_truncated_to_20_words(self):
        long_quote = " ".join(f"word{i}" for i in range(40))
        parsed = {
            "document_type": "payslip",
            "document_confidence": "high",
            "fields": [{"name": "employment_income.employer", "value": "Acme", "quote": long_quote, "confidence": "high"}],
        }
        _doc_type, _doc_conf, fields = extraction.clamp_ai_reply(parsed, "doc.pdf")
        self.assertLessEqual(len(fields[0].quote.split()), 20)

    def test_parse_ai_reply_tolerates_markdown_fences(self):
        raw = '```json\n{"document_type": "payslip", "document_confidence": "high", "fields": []}\n```'
        parsed = extraction.parse_ai_reply(raw)
        self.assertEqual(parsed["document_type"], "payslip")

    def test_parse_ai_reply_invalid_json_returns_empty(self):
        self.assertEqual(extraction.parse_ai_reply("not json at all"), {})


class TestMergeAndConflicts(unittest.TestCase):
    def test_merge_groups_by_field(self):
        e1 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$85,000", source_document="a.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$85,000", source_document="b.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="ai",
        )
        pool = extraction.merge_evidence([e1, e2])
        self.assertEqual(len(pool["employment_income.base_income"]), 2)

    def test_detect_conflict_across_sources(self):
        e1 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$85,000", source_document="payslip.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$95,000", source_document="fact_find.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        pool = extraction.merge_evidence([e1, e2])
        conflicts = extraction.detect_conflicts(pool)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["field"], "employment_income.base_income")
        sources = {s["source_document"] for s in conflicts[0]["sources"]}
        self.assertEqual(sources, {"payslip.pdf", "fact_find.pdf"})

    def test_no_conflict_when_values_agree(self):
        e1 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$85,000", source_document="payslip.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="employment_income.base_income", value="85000", source_document="fact_find.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        pool = extraction.merge_evidence([e1, e2])
        self.assertEqual(extraction.detect_conflicts(pool), [])

    def test_no_conflict_when_same_source_repeats(self):
        e1 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$85,000", source_document="payslip.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="employment_income.base_income", value="$95,000", source_document="payslip.pdf",
            confidence="medium", confidence_reason="r", quote="q", manual_review_required=False, category="ai",
        )
        pool = extraction.merge_evidence([e1, e2])
        self.assertEqual(extraction.detect_conflicts(pool), [])

    def test_negative_money_values_are_not_conflated_with_positive(self):
        e1 = extraction.FieldEvidence(
            field="security_property.existing_debt", value="-$5,000", source_document="a.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="security_property.existing_debt", value="$5,000", source_document="b.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        pool = extraction.merge_evidence([e1, e2])
        conflicts = extraction.detect_conflicts(pool)
        self.assertEqual(len(conflicts), 1)

    def test_percent_values_with_different_precision_do_not_conflict(self):
        e1 = extraction.FieldEvidence(
            field="loan.lvr", value="80%", source_document="a.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="loan.lvr", value="80.00%", source_document="b.pdf",
            confidence="high", confidence_reason="r", quote="q", manual_review_required=False, category="regex",
        )
        pool = extraction.merge_evidence([e1, e2])
        self.assertEqual(extraction.detect_conflicts(pool), [])

    def test_dates_with_different_formats_do_not_conflict(self):
        e1 = extraction.FieldEvidence(
            field="client.dob", value="1/6/1985", source_document="a.pdf",
            confidence="medium", confidence_reason="r", quote="q", manual_review_required=True, category="regex",
        )
        e2 = extraction.FieldEvidence(
            field="client.dob", value="01/06/1985", source_document="b.pdf",
            confidence="medium", confidence_reason="r", quote="q", manual_review_required=True, category="regex",
        )
        pool = extraction.merge_evidence([e1, e2])
        self.assertEqual(extraction.detect_conflicts(pool), [])

    def test_field_evidence_from_dict_tolerates_missing_keys(self):
        evidence = extraction.FieldEvidence.from_dict({"field": "loan.amount", "value": "$1"})
        self.assertEqual(evidence.source_document, "")
        self.assertEqual(evidence.confidence, "low")
        self.assertFalse(evidence.manual_review_required)


class TestAIExtractionStripsTaxIds(unittest.TestCase):
    class _RecordingClient:
        def __init__(self):
            self.enabled = True
            self.received_content = None

        def respond(self, instructions, content):
            self.received_content = content
            return '{"document_type": "payslip", "document_confidence": "high", "fields": []}'

    def test_tfn_stripped_before_sending_to_ai(self):
        client = self._RecordingClient()
        text = "Employee TFN: 123 456 789. Base Income: $85,000."
        extraction.ai_extract_document(client, text, "payslip.pdf")
        self.assertNotIn("123 456 789", client.received_content)
        self.assertIn("[REMOVED_TAX_IDENTIFIER]", client.received_content)

    def test_ai_disabled_client_is_noop(self):
        client = AIClient(env={})
        doc_type, doc_conf, fields, warning = extraction.ai_extract_document(client, "some text", "doc.pdf")
        self.assertIsNone(doc_type)
        self.assertIsNone(doc_conf)
        self.assertEqual(fields, [])
        self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()

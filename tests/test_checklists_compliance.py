import unittest

import _pathfix  # noqa: F401

from src.broker import checklists, compliance


class TestChecklists(unittest.TestCase):
    def test_missing_documents_removes_satisfied_items(self):
        missing = checklists.missing_documents("payg_purchase", present_document_types=["payslip"])
        required_now = missing["required_now"]
        self.assertNotIn("2 most recent payslips", required_now)
        self.assertIn("Photo ID", required_now)

    def test_missing_documents_all_outstanding_when_nothing_present(self):
        missing = checklists.missing_documents("payg_purchase", present_document_types=[])
        self.assertIn("2 most recent payslips", missing["required_now"])
        self.assertIn("Photo ID", missing["required_now"])

    def test_buckets_present_for_every_deal_type(self):
        config = checklists.load_checklists()
        for deal_type in config["deal_types"]:
            checklist = checklists.get_checklist(deal_type)
            for bucket in checklists.BUCKETS:
                self.assertIn(bucket, checklist)


class TestCompliance(unittest.TestCase):
    def test_nccp_applicable_for_consumer_deal(self):
        result = compliance.build_compliance_checklist("payg_purchase", "")
        self.assertTrue(result["applicable"])

    def test_nccp_exempt_for_commercial_deal(self):
        result = compliance.build_compliance_checklist("commercial_property_purchase", "")
        self.assertFalse(result["applicable"])
        self.assertIn("broker to confirm", result["applicability_note"])

    def test_control_missing_when_no_keyword_evidence(self):
        result = compliance.build_compliance_checklist("payg_purchase", "irrelevant text with nothing useful")
        statuses = {c["id"]: c["status"] for c in result["controls"]}
        self.assertEqual(statuses["credit_guide_privacy_consent"], "missing")

    def test_control_flags_review_when_keyword_present(self):
        result = compliance.build_compliance_checklist(
            "payg_purchase", "Signed credit guide and privacy consent on file."
        )
        statuses = {c["id"]: c["status"] for c in result["controls"]}
        self.assertEqual(statuses["credit_guide_privacy_consent"], "evidence_found_review_required")

    def test_reviewed_ids_mark_control_reviewed(self):
        result = compliance.build_compliance_checklist(
            "payg_purchase", "", reviewed_ids=["credit_guide_privacy_consent"]
        )
        statuses = {c["id"]: c["status"] for c in result["controls"]}
        self.assertEqual(statuses["credit_guide_privacy_consent"], "reviewed")

    def test_verify_warning_present(self):
        result = compliance.build_compliance_checklist("payg_purchase", "")
        self.assertIn("aggregator", result["verify_warning"])


if __name__ == "__main__":
    unittest.main()

import unittest

import _pathfix  # noqa: F401

from src.broker import board as board_mod
from src.broker.extraction import FieldEvidence


def _evidence(field, value, source, confidence, category="regex", quote="q"):
    return FieldEvidence(
        field=field, value=value, source_document=source, confidence=confidence,
        confidence_reason="r", quote=quote, manual_review_required=(confidence != "high"),
        category=category,
    )


class TestSalestrekkerComparisonPack(unittest.TestCase):
    def setUp(self):
        self.pool = {
            "security_property.value": [_evidence("security_property.value", "$800,000", "rates_notice.pdf", "high")],
            "client.names": [_evidence("client.names", "John Smith", "id.pdf", "medium", quote="Name: John Smith")],
            "loan.amount": [_evidence("loan.amount", "$650,000", "fact_find.pdf", "high")],
            "employment_income.base_income": [
                _evidence("employment_income.base_income", "$85,000", "payslip.pdf", "high", category="ai", quote="Base Income: $85,000 per annum")
            ],
            "liabilities.car_loan": [_evidence("liabilities.car_loan", "$12,000", "bank_statement.pdf", "medium")],
        }

    def test_rows_ordered_client_loan_income_liabilities_security(self):
        rows = board_mod.build_st_comparison(self.pool)
        fields_in_order = [row["field"] for row in rows]
        sections_in_order = [f.split(".")[0] for f in fields_in_order]
        expected_section_order = ["client", "loan", "employment_income", "liabilities", "security_property"]
        seen_order = []
        for section in sections_in_order:
            if section not in seen_order:
                seen_order.append(section)
        self.assertEqual(seen_order, expected_section_order)

    def test_quotes_present_for_ai_values(self):
        rows = board_mod.build_st_comparison(self.pool)
        ai_row = next(r for r in rows if r["field"] == "employment_income.base_income")
        self.assertEqual(ai_row["quote"], "Base Income: $85,000 per annum")
        self.assertTrue(ai_row["quote"])

    def test_review_required_flag_correct(self):
        rows = board_mod.build_st_comparison(self.pool)
        by_field = {r["field"]: r for r in rows}
        self.assertFalse(by_field["security_property.value"]["review_required"])
        self.assertFalse(by_field["loan.amount"]["review_required"])
        self.assertFalse(by_field["employment_income.base_income"]["review_required"])
        self.assertTrue(by_field["client.names"]["review_required"])
        self.assertTrue(by_field["liabilities.car_loan"]["review_required"])

    def test_picks_best_confidence_evidence_when_multiple(self):
        pool = {
            "loan.amount": [
                _evidence("loan.amount", "$600,000", "email.eml", "low"),
                _evidence("loan.amount", "$650,000", "fact_find.pdf", "high"),
            ]
        }
        rows = board_mod.build_st_comparison(pool)
        self.assertEqual(rows[0]["value"], "$650,000")
        self.assertEqual(rows[0]["confidence"], "high")

    def test_render_text_includes_exact_header(self):
        rows = board_mod.build_st_comparison(self.pool)
        text = board_mod.render_st_comparison_text(rows)
        self.assertTrue(
            text.startswith(
                "Compare only. Do not enter or save anything in Salestrekker without broker "
                "approval per field. Verify client identity with name plus one more "
                "identifier before touching any record."
            )
        )

    def test_render_text_empty_rows(self):
        text = board_mod.render_st_comparison_text([])
        self.assertIn("Compare only.", text)
        self.assertIn("no evidenced fields yet", text)


if __name__ == "__main__":
    unittest.main()

import unittest

import _pathfix  # noqa: F401

from src.broker.classifier import Classifier, UNKNOWN_TYPE


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = Classifier()

    def test_payslip_detected_by_filename_and_text(self):
        result = self.classifier.classify(
            "june_payslip.pdf",
            "Employer: Acme Pty Ltd\nGross Pay: $3,200\nYear to date earnings: $41,600\nTax withheld: $600",
        )
        self.assertEqual(result.document_type, "payslip")
        self.assertEqual(result.confidence, "high")

    def test_bank_statement_detected(self):
        result = self.classifier.classify(
            "statement.pdf",
            "Opening balance $1,000 Closing balance $1,500 BSB 062-000 Account number 12345678",
        )
        self.assertEqual(result.document_type, "bank statement")

    def test_unknown_document_when_nothing_matches(self):
        result = self.classifier.classify("random.pdf", "Lorem ipsum dolor sit amet.")
        self.assertEqual(result.document_type, UNKNOWN_TYPE)
        self.assertEqual(result.confidence, "low")

    def test_eml_filename_hint_client_email(self):
        result = self.classifier.classify(
            "thread.eml", "From: client@example.com\nSubject: Re loan\nKind regards, John"
        )
        self.assertEqual(result.document_type, "client email")

    def test_document_types_include_required_set(self):
        required = {
            "payslip", "bank statement", "tax return", "notice of assessment",
            "ID document", "contract of sale", "rates notice", "credit report",
            "loan statement", "fact find", "client email", "broker note",
            "compliance document", "Client Profile and Recommendation", "unknown document",
        }
        self.assertTrue(required.issubset(set(self.classifier.document_types) | {UNKNOWN_TYPE}))


if __name__ == "__main__":
    unittest.main()

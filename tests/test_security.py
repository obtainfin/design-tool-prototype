import unittest

import _pathfix  # noqa: F401

from src.broker.security import (
    CredentialDetectedError,
    strip_tax_identifiers,
    validate_no_credentials,
)


class TestStripTaxIdentifiers(unittest.TestCase):
    def test_strips_labelled_tfn(self):
        text = "Client TFN: 123 456 789 confirmed on call."
        result = strip_tax_identifiers(text)
        self.assertNotIn("123 456 789", result)
        self.assertIn("[REMOVED_TAX_IDENTIFIER]", result)

    def test_strips_bare_tfn_shape(self):
        text = "Reference number 987 654 321 was noted in the file."
        result = strip_tax_identifiers(text)
        self.assertNotIn("987 654 321", result)
        self.assertIn("[REMOVED_TAX_IDENTIFIER]", result)

    def test_strips_medicare_number(self):
        text = "Medicare Number: 2234 56789 1 sighted."
        result = strip_tax_identifiers(text)
        self.assertNotIn("2234 56789 1", result)
        self.assertIn("[REMOVED_TAX_IDENTIFIER]", result)

    def test_dollar_amounts_dates_names_pass_through(self):
        text = "John Smith earns $85,000 base as of 12/05/2024, DOB 01-02-1990 balance $12,340.50"
        result = strip_tax_identifiers(text)
        self.assertIn("John Smith", result)
        self.assertIn("$85,000", result)
        self.assertIn("12/05/2024", result)
        self.assertIn("$12,340.50", result)

    def test_empty_text(self):
        self.assertEqual(strip_tax_identifiers(""), "")


class TestValidateNoCredentials(unittest.TestCase):
    def test_rejects_password(self):
        with self.assertRaises(CredentialDetectedError):
            validate_no_credentials({"note": "password: hunter2legit"})

    def test_rejects_api_key(self):
        with self.assertRaises(CredentialDetectedError):
            validate_no_credentials("api_key: sk-abcdef1234567890abcdef")

    def test_rejects_bearer_token(self):
        with self.assertRaises(CredentialDetectedError):
            validate_no_credentials("Authorization: Bearer abcdefghijklmnop123456")

    def test_rejects_otp_code(self):
        with self.assertRaises(CredentialDetectedError):
            validate_no_credentials("Your OTP code: 482913")

    def test_rejects_private_key_block(self):
        with self.assertRaises(CredentialDetectedError):
            validate_no_credentials("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END-----")

    def test_allows_normal_business_text(self):
        payload = {
            "note": "Client's base income is $85,000 and they want to refinance.",
            "nested": ["Bank statement shows steady deposits.", 12345],
        }
        validate_no_credentials(payload)  # should not raise


if __name__ == "__main__":
    unittest.main()

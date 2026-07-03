import json
import tempfile
import unittest
from pathlib import Path

import _pathfix  # noqa: F401

from src.broker import workflow
from src.broker.ai_client import AIClient


class TestWorkflowRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.deal_dir = Path(self._tmp.name) / "Test Deal"
        paths = workflow.ensure_deal_folders(self.deal_dir)
        self.inbox = paths["inbox"]

        (self.inbox / "payslip.txt").write_text(
            "PAYSLIP\nEmployer: Acme Pty Ltd\nGross Pay: $3,269.23\n"
            "Base Income: $85,000\nYear to date earnings: $41,600\nTax withheld: $600\n"
            "Employee TFN: 123 456 789\n",
            encoding="utf-8",
        )
        (self.inbox / "bank_statement.txt").write_text(
            "BANK STATEMENT\nOpening balance $1,000\nClosing balance $6,500\n"
            "BSB 062-000 Account number 12345678\nBase Income: $95,000 per fact find note\n",
            encoding="utf-8",
        )
        (self.inbox / "fact_find.txt").write_text(
            "FACT FIND\nClient objectives: buy first home\nTimeframe: 3 months\n"
            "Required features: offset account\n",
            encoding="utf-8",
        )
        (self.inbox / "client_email.eml").write_bytes(
            b"From: client@example.com\nDate: Mon, 1 Jul 2024 10:00:00 +1000\n"
            b"Subject: Documents attached\n\nHi, please find my payslip attached.\nRegards,\nJohn\n"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_produces_deal_board_and_files_copies(self):
        ai_client = AIClient(env={"BROKER_AI_DISABLED": "1"})
        board = workflow.run(self.deal_dir, deal_type="payg_purchase", ai_client=ai_client)

        outputs = self.deal_dir / "outputs"
        self.assertTrue((outputs / "deal_board.json").exists())
        self.assertTrue((outputs / "deal_board.md").exists())

        # originals are untouched
        self.assertTrue((self.inbox / "payslip.txt").exists())
        self.assertEqual(len(list(self.inbox.iterdir())), 4)

        # filed copies exist under Documents/Filed/<Category>/
        filed_root = self.deal_dir / "Documents" / "Filed"
        filed_files = [p.name for p in filed_root.rglob("*") if p.is_file()]
        self.assertIn("payslip.txt", filed_files)
        self.assertIn("bank_statement.txt", filed_files)

        self.assertEqual(len(board["documents"]), 4)
        doc_types = {d["filename"]: d["document_type"] for d in board["documents"]}
        self.assertEqual(doc_types["payslip.txt"], "payslip")
        self.assertEqual(doc_types["bank_statement.txt"], "bank statement")
        self.assertEqual(doc_types["client_email.eml"], "client email")

    def test_run_detects_income_conflict(self):
        ai_client = AIClient(env={"BROKER_AI_DISABLED": "1"})
        board = workflow.run(self.deal_dir, deal_type="payg_purchase", ai_client=ai_client)
        conflict_fields = {c["field"] for c in board["conflicts"]}
        self.assertIn("employment_income.base_income", conflict_fields)

    def test_run_reports_missing_documents(self):
        ai_client = AIClient(env={"BROKER_AI_DISABLED": "1"})
        board = workflow.run(self.deal_dir, deal_type="payg_purchase", ai_client=ai_client)
        self.assertIn("Photo ID", board["missing_documents"]["required_now"])

    def test_no_network_ai_disabled_still_completes(self):
        ai_client = AIClient(env={"BROKER_AI_DISABLED": "1"})
        self.assertFalse(ai_client.enabled)
        board = workflow.run(self.deal_dir, deal_type="payg_purchase", ai_client=ai_client)
        self.assertTrue(board["documents"])

    def test_tfn_never_appears_in_deal_board(self):
        ai_client = AIClient(env={"BROKER_AI_DISABLED": "1"})
        board = workflow.run(self.deal_dir, deal_type="payg_purchase", ai_client=ai_client)
        board_text = json.dumps(board)
        self.assertNotIn("123 456 789", board_text)

    def test_audit_log_written(self):
        ai_client = AIClient(env={"BROKER_AI_DISABLED": "1"})
        workflow.run(self.deal_dir, deal_type="payg_purchase", ai_client=ai_client)
        audit_path = self.deal_dir / "audit_log.jsonl"
        self.assertTrue(audit_path.exists())
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(len(lines) > 0)
        for line in lines:
            json.loads(line)  # each line is valid JSON


if __name__ == "__main__":
    unittest.main()

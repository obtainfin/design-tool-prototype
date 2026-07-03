import tempfile
import unittest
from pathlib import Path

import _pathfix  # noqa: F401

from src.broker import deal_state


class TestDealStateMachine(unittest.TestCase):
    def test_starts_at_intake(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        self.assertEqual(state.stage, "intake")

    def test_forward_single_step_transitions(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        state.advance()
        self.assertEqual(state.stage, "servicing")
        state.advance()
        self.assertEqual(state.stage, "policy")

    def test_cannot_enter_docs_without_lender(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        state.advance()  # servicing
        state.advance()  # policy
        state.advance()  # lender_select
        with self.assertRaises(deal_state.InvalidTransitionError):
            state.advance()  # attempting docs without a lender

    def test_selecting_lender_unblocks_docs(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        for _ in range(3):
            state.advance()
        self.assertEqual(state.stage, "lender_select")
        state.set_lender("CBA")
        state.advance()
        self.assertEqual(state.stage, "docs")

    def test_cannot_enter_submit_without_st_prep(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        for _ in range(3):
            state.advance()
        state.set_lender("CBA")
        state.advance()  # docs
        state.advance()  # st_prep
        with self.assertRaises(deal_state.InvalidTransitionError):
            state.advance()  # attempting submit without st_prep recorded

    def test_recording_st_prep_unblocks_submit(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        for _ in range(3):
            state.advance()
        state.set_lender("CBA")
        state.advance()  # docs
        state.advance()  # st_prep
        state.record_st_prep()
        state.advance()
        self.assertEqual(state.stage, "submit")

    def test_cannot_advance_past_final_stage(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        for _ in range(3):
            state.advance()
        state.set_lender("CBA")
        state.advance()
        state.advance()
        state.record_st_prep()
        state.advance()
        self.assertEqual(state.stage, "submit")
        with self.assertRaises(deal_state.InvalidTransitionError):
            state.advance()

    def test_every_mutation_appends_audit_event(self):
        state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
        state.advance(actor="broker")
        state.set_lender("CBA", actor="broker")
        for event in state.audit_log:
            self.assertIn("timestamp", event)
            self.assertIn("event", event)
            self.assertIn("actor", event)
            self.assertIn("detail", event)
        self.assertTrue(any(e["event"] == "stage_advanced" for e in state.audit_log))
        self.assertTrue(any(e["event"] == "lender_selected" for e in state.audit_log))

    def test_atomic_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            deal_dir = Path(tmp) / "Smith Purchase"
            state = deal_state.DealState(deal_name="Smith Purchase", deal_type="payg_purchase")
            state.advance()
            state.set_lender("NAB")
            deal_state.save(deal_dir, state)

            self.assertTrue((deal_dir / "deal_state.json").exists())
            self.assertTrue((deal_dir / "audit_log.jsonl").exists())

            reloaded = deal_state.load(deal_dir)
            self.assertEqual(reloaded.stage, "servicing")
            self.assertEqual(reloaded.selected_lender, "NAB")
            self.assertEqual(len(reloaded.audit_log), len(state.audit_log))

    def test_ui_step_mapping_collapses_7_to_5(self):
        self.assertEqual(deal_state.ui_step_index("intake"), 0)
        self.assertEqual(deal_state.ui_step_index("servicing"), 1)
        self.assertEqual(deal_state.ui_step_index("policy"), 1)
        self.assertEqual(deal_state.ui_step_index("lender_select"), 1)
        self.assertEqual(deal_state.ui_step_index("docs"), 2)
        self.assertEqual(deal_state.ui_step_index("st_prep"), 3)
        self.assertEqual(deal_state.ui_step_index("submit"), 4)
        self.assertEqual(len(deal_state.UI_STEPS), 5)


if __name__ == "__main__":
    unittest.main()

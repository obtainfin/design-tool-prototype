import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import _pathfix  # noqa: F401

import app as app_module


class _StubAIClient:
    def __init__(self, reply_json='{"reply": "AI test reply.", "flags": ["ok"]}'):
        self.enabled = True
        self.model = "stub-model"
        self._reply_json = reply_json
        self.last_content = None

    def status_label(self):
        return "AI connected (stub)"

    def respond(self, instructions, content):
        self.last_content = content
        return self._reply_json


class TestApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_deals_root = os.environ.get("BROKER_DEALS_ROOT")
        os.environ["BROKER_DEALS_ROOT"] = self.tmp
        os.environ["BROKER_AI_DISABLED"] = "1"
        self.server = app_module.run_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_deals_root is None:
            os.environ.pop("BROKER_DEALS_ROOT", None)
        else:
            os.environ["BROKER_DEALS_ROOT"] = self._old_deals_root

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return resp.status, parsed

    def _create_deal(self, name="Test Deal", deal_type="payg_purchase"):
        status, data = self._request("POST", "/api/deals", {"name": name, "deal_type": deal_type})
        self.assertEqual(status, 201)
        return data

    # -- ui_step mapping (7 -> 5) ------------------------------------------

    def test_ui_step_mapping_via_api(self):
        self._create_deal("Mapping Deal")
        status, data = self._request("GET", "/api/deal/Mapping%20Deal")
        self.assertEqual(status, 200)
        self.assertEqual(data["ui_step_index"], 0)
        self.assertEqual(data["ui_step_name"], "Triage")
        self.assertEqual(
            data["ui_steps"], ["Triage", "Servicing & policy", "Docs", "Salestrekker", "Final check"]
        )

    def test_run_endpoint_produces_board(self):
        self._create_deal("Run Deal")
        status, data = self._request("POST", "/api/deal/Run%20Deal/run")
        self.assertEqual(status, 200)
        self.assertIsNotNone(data["board"])

    # -- call note + file note endpoints -----------------------------------

    def test_call_note_endpoint_merges_evidence(self):
        self._create_deal("Note Deal")
        status, data = self._request(
            "POST",
            "/api/deal/Note%20Deal/call_note",
            {"text": "Base Income: $92,000 confirmed by client on call."},
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(data["board"])
        self.assertIn("employment_income.base_income", data["board"]["fields"])
        notes_path = Path(self.tmp) / "Note Deal" / "Notes" / "file_notes.md"
        self.assertTrue(notes_path.exists())
        self.assertIn("Base Income: $92,000", notes_path.read_text())

    def test_file_note_endpoint_appends_note(self):
        self._create_deal("FileNote Deal")
        status, data = self._request(
            "POST", "/api/deal/FileNote%20Deal/file_note", {"text": "Called client, left voicemail."}
        )
        self.assertEqual(status, 200)
        notes_path = Path(self.tmp) / "FileNote Deal" / "Notes" / "file_notes.md"
        self.assertIn("Called client, left voicemail.", notes_path.read_text())

    # -- no credential-looking payload accepted ------------------------------

    def test_call_note_rejects_credential_payload(self):
        self._create_deal("Cred Deal")
        status, data = self._request(
            "POST", "/api/deal/Cred%20Deal/call_note", {"text": "my password: hunter2legit"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_ask_rejects_credential_payload(self):
        self._create_deal("Ask Cred Deal")
        status, data = self._request(
            "POST", "/api/deal/Ask%20Cred%20Deal/ask", {"question": "api_key: sk-1234567890abcdef1234"}
        )
        self.assertEqual(status, 400)

    def test_file_note_rejects_credential_payload(self):
        self._create_deal("File Cred Deal")
        status, data = self._request(
            "POST", "/api/deal/File%20Cred%20Deal/file_note", {"text": "Bearer abcdefghijklmno123456"}
        )
        self.assertEqual(status, 400)

    # -- keyword routing for state actions -----------------------------------

    def test_ask_keyword_routes_mark_step_done(self):
        self._create_deal("Keyword Deal")
        status, data = self._request(
            "POST", "/api/deal/Keyword%20Deal/ask", {"question": "please mark step done"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["stage"], "servicing")
        self.assertEqual(data["assistant"]["source"], "action")

    def test_ask_keyword_routes_select_lender(self):
        self._create_deal("Lender Deal")
        for _ in range(3):
            self._request("POST", "/api/deal/Lender%20Deal/advance")
        status, data = self._request(
            "POST", "/api/deal/Lender%20Deal/ask", {"question": "select lender CBA please"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["selected_lender"], "CBA")
        self.assertEqual(data["stage"], "docs")  # auto-advanced past lender_select

    def test_ask_keyword_routes_sign_off_nccp(self):
        self._create_deal("NCCP Deal")
        self._request("POST", "/api/deal/NCCP%20Deal/run")
        status, data = self._request(
            "POST", "/api/deal/NCCP%20Deal/ask", {"question": "please sign off nccp for me"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["assistant"]["source"], "action")

    # -- free text: AI when configured, fallback when not --------------------

    def test_ask_falls_back_offline_when_ai_not_configured(self):
        self._create_deal("Fallback Deal")
        self._request("POST", "/api/deal/Fallback%20Deal/run")
        status, data = self._request(
            "POST", "/api/deal/Fallback%20Deal/ask", {"question": "what's missing?"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["assistant"]["source"], "fallback")

    def test_ask_routes_to_ai_when_configured(self):
        self._create_deal("AI Deal")
        self._request("POST", "/api/deal/AI%20Deal/run")
        with patch("app.AIClient", return_value=_StubAIClient()):
            status, data = self._request("POST", "/api/deal/AI%20Deal/ask", {"question": "what's missing?"})
        self.assertEqual(status, 200)
        self.assertEqual(data["assistant"]["source"], "ai")
        self.assertEqual(data["assistant"]["reply"], "AI test reply.")

    def test_step_action_routes_to_ai_when_configured(self):
        self._create_deal("Step AI Deal")
        self._request("POST", "/api/deal/Step%20AI%20Deal/run")
        with patch("app.AIClient", return_value=_StubAIClient()):
            status, data = self._request("POST", "/api/deal/Step%20AI%20Deal/step_action")
        self.assertEqual(status, 200)
        self.assertEqual(data["assistant"]["source"], "ai")

    def test_upload_rejects_non_multipart(self):
        self._create_deal("Upload Deal")
        status, data = self._request("POST", "/api/deal/Upload%20Deal/upload", {"not": "multipart"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

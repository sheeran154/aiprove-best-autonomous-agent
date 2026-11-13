"""Tests for the autonomous AIProve agent.

Runs the agent against a local mock board (no network, no real key):
- multi-bounty run: submits to every priceable bounty, skips the rest
- resume run: a slug already in state.json is NOT re-submitted
- dry-run: nothing is entered or submitted
- 429 abort: three consecutive rate limits stop the run
- secret hygiene: key and token never appear in stdout

Run:  python3 -m unittest test_agent -v
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402

TEST_KEY = "0x" + "22" * 32  # throwaway key, test-only, never a real wallet

BOUNTIES = [
    {"slug": "btc-tomorrow", "title": "Will BTC close higher tomorrow?",
     "lane": "AutoGrade", "metric": "Brier score", "status": "open"},
    {"slug": "spy-close-1dollar", "title": "Predict tomorrow's SPY close",
     "lane": "AutoGrade", "metric": "Absolute error, $", "status": "open"},
    {"slug": "forecast-eth-24h", "title": "Call ETH's 24-hour direction",
     "lane": "AutoGrade", "metric": "Brier score", "status": "open"},
    {"slug": "make-meme-aiprove", "title": "Make a meme",
     "lane": "PosterJudge", "metric": "Poster rubric", "status": "open"},
    {"slug": "top5-pons-24h", "title": "Predict the top 5 new pons tokens",
     "lane": "AutoGrade", "metric": "Overlap with realized top 5", "status": "open"},
]


def make_mock_server(fail_429_on_submit=False):
    state = {"calls": [], "submissions": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["calls"].append(("GET", self.path))
            if self.path == "/skill.md":
                body = ("# skill.md\nAuthenticate\n/api/auth/challenge\n"
                        "/api/bounties\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/auth/challenge"):
                self._send(200, {"challenge": "mock-challenge-456"})
            elif self.path == "/api/bounties?status=open":
                self._send(200, {"bounties": BOUNTIES})
            elif self.path == "/api/me/submissions":
                self._send(200, {"submissions": state["submissions"]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                payload = {}
            state["calls"].append(("POST", self.path, payload))
            if fail_429_on_submit and self.path.endswith("/submit"):
                self._send(429, {"error": "slow down"})
                return
            if self.path == "/api/auth/verify":
                self._send(200, {"token": "mock-token-def", "expiresAt": 9999999999999})
            elif self.path.endswith("/enter"):
                self._send(200, {"ok": True})
            elif self.path.endswith("/submit"):
                sub = {"id": f"sub-{self.path.split('/')[3]}",
                       "bountySlug": self.path.split("/")[3],
                       "prediction": payload.get("prediction"),
                       "status": "provenance_ok"}
                state["submissions"].append(sub)
                self._send(201, sub)
            else:
                self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["AGENT_PRIVATE_KEY"] = TEST_KEY
        self._orig_btc = agent.coingecko_closes
        self._orig_yahoo = agent.yahoo_closes
        # BTC down ~5%, ETH up ~3%, SPY flat-ish.
        agent.coingecko_closes = lambda coin_id, days=7: (
            [75000, 74000, 73000, 72000, 71000] if coin_id == "bitcoin"
            else [1800, 1820, 1830, 1840, 1855])
        agent.yahoo_closes = lambda symbol, range_="5d": [750.0, 752.0, 751.0]
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self._tmp.name, "state.json")

    def tearDown(self):
        agent.coingecko_closes = self._orig_btc
        agent.yahoo_closes = self._orig_yahoo
        self._tmp.cleanup()

    def run_agent(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = agent.main(list(argv))
        return code, out.getvalue()

    @staticmethod
    def report_of(out):
        return json.loads(out[out.rindex("\n{") + 1:])

    def test_multi_bounty_run(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--state", self.state_path)
            self.assertEqual(code, 0, out)
            report = self.report_of(out)
            submitted = {s["slug"] for s in report["submitted"]}
            self.assertEqual(submitted, {"btc-tomorrow", "spy-close-1dollar",
                                         "forecast-eth-24h"})
            # Skips are recorded with reasons.
            self.assertIn("make-meme-aiprove", report["skipped"])
            self.assertIn("top5-pons-24h", report["skipped"])
            self.assertEqual(report["failed"], {})
            self.assertIsNone(report["aborted"])
            # State file persists the submissions (resume-safe).
            saved = json.load(open(self.state_path))
            self.assertEqual(set(saved["submitted"]), submitted)
            # Direction follows momentum: BTC down -> no, ETH up -> yes.
            preds = {s["bountySlug"]: s for s in state["submissions"]}
            self.assertEqual(preds["btc-tomorrow"]["prediction"]["answer"], "no")
            self.assertEqual(preds["forecast-eth-24h"]["prediction"]["answer"], "yes")
        finally:
            server.shutdown()

    def test_resume_does_not_resubmit(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            # First run.
            code, _ = self.run_agent("--api-base", base, "--state", self.state_path)
            self.assertEqual(code, 0)
            first_count = len(state["submissions"])
            self.assertEqual(first_count, 3)
            # Second run with the same state file: nothing new submitted.
            code, out = self.run_agent("--api-base", base, "--state", self.state_path)
            self.assertEqual(code, 0, out)
            report = self.report_of(out)
            self.assertEqual(report["submitted"], [])
            self.assertEqual(len(state["submissions"]), first_count)
            self.assertIn("btc-tomorrow", report["skipped"])
        finally:
            server.shutdown()

    def test_dry_run_submits_nothing(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--state", self.state_path,
                                       "--dry-run")
            self.assertEqual(code, 0, out)
            report = self.report_of(out)
            self.assertEqual(len(report["dry_run_ready"]), 3)
            self.assertEqual(report["submitted"], [])
            self.assertEqual(state["submissions"], [])
        finally:
            server.shutdown()

    def test_429_abort(self):
        server, state = make_mock_server(fail_429_on_submit=True)
        orig_retries = agent.MAX_RETRIES
        agent.MAX_RETRIES = 1  # no sleeping: test the abort logic, not backoff
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--state", self.state_path)
            self.assertEqual(code, 0, out)
            report = self.report_of(out)
            self.assertEqual(report["aborted"], "too many consecutive rate limits")
            self.assertEqual(report["submitted"], [])
            self.assertEqual(len(report["failed"]), 3)
        finally:
            agent.MAX_RETRIES = orig_retries
            server.shutdown()

    def test_max_submissions_cap(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--state", self.state_path,
                                       "--max-submissions", "1")
            self.assertEqual(code, 0, out)
            report = self.report_of(out)
            self.assertEqual(len(report["submitted"]), 1)
        finally:
            server.shutdown()

    def test_secrets_never_in_output(self):
        server, state = make_mock_server()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, out = self.run_agent("--api-base", base, "--state", self.state_path)
            self.assertNotIn(TEST_KEY, out)
            self.assertNotIn("mock-token-def", out)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()

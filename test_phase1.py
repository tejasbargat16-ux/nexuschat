"""
Unit tests for Phase 1: Core & Stability.
Verifies models.json loading, provider health checks, fallback mechanisms,
and API endpoints.
"""

import json
import os
import unittest
from unittest.mock import patch, MagicMock

import providers
import api


class TestPhase1Core(unittest.TestCase):

    def test_models_json_exists_and_loads(self):
        """Verify models.json exists, is valid JSON, and populates PROVIDERS."""
        models_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
        self.assertTrue(os.path.exists(models_path), "models.json must exist")

        with open(models_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("providers", data)
        self.assertIn("fallback_order", data)
        self.assertIn("groq", data["providers"])
        self.assertIn("gemini", data["providers"])
        self.assertIn("openrouter", data["providers"])

        # Check in-memory providers
        self.assertIn("groq", providers.PROVIDERS)
        self.assertIn("openrouter", providers.PROVIDERS)
        self.assertTrue(len(providers.FALLBACK_ORDER) >= 4)

    def test_check_health_unconfigured(self):
        """Verify health check returns 'unconfigured' for providers without keys."""
        res = providers.check_health("cohere")
        self.assertEqual(res["status"], "unconfigured")
        self.assertFalse(res["ok"])
        self.assertIn("Missing", res["error"])
        self.assertIsNotNone(res["model"])
        self.assertIsNotNone(res["default_model"])

    @patch("requests.get")
    def test_check_health_mock_online(self, mock_get):
        """Verify health check accurately parses healthy endpoints."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        with patch.object(providers, "load_key", return_value="dummy-key"):
            res = providers.check_health("groq")
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "healthy")
            self.assertIsNone(res["error"])
            self.assertIsNotNone(res["latency_ms"])

    @patch("providers.call")
    def test_call_with_fallback_success_on_fallback(self, mock_call):
        """Verify call_with_fallback tries next provider if primary fails."""
        # First call (gemini) fails, second call (openrouter) succeeds
        mock_call.side_effect = [
            RuntimeError("Gemini 401 Unauthorized"),
            "Hello from OpenRouter!",
        ]

        messages = [{"role": "user", "content": "Ping"}]
        with patch.object(providers, "load_key", side_effect=lambda p, s=None: "dummy_key" if p in ["gemini", "openrouter"] else None):
            reply, used_p, used_m, attempts = providers.call_with_fallback(
                messages, preferred="gemini", model="gemini-flash"
            )

            self.assertEqual(reply, "Hello from OpenRouter!")
            self.assertEqual(used_p, "openrouter")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0][0], "gemini")
            self.assertIn("Gemini 401", attempts[0][1])

    def test_api_health_endpoints(self):
        """Verify API health and health/providers endpoints respond with 200."""
        client = api.app.test_client()

        # Public health check
        r1 = client.get("/health")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json.get("status"), "ok")

        # Provider health check
        r2 = client.get("/health/providers")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("groq", r2.json)
        self.assertIn("gemini", r2.json)
        self.assertIn("openrouter", r2.json)


if __name__ == "__main__":
    unittest.main()


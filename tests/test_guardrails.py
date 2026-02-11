from __future__ import annotations

import unittest

from emailgenius.guardrails import apply_claim_guard


class GuardrailTests(unittest.TestCase):
    def test_detects_and_sanitizes_risky_claims(self) -> None:
        text = "Oggetto: Soluzione garantita\n\nRisultati garantiti e senza rischi al 100%."
        cleaned, flags = apply_claim_guard(text, ["risultati garantiti"])

        self.assertIn("claim_guaranteed", flags)
        self.assertIn("claim_zero_risk", flags)
        self.assertIn("no_go:risultati garantiti", flags)
        self.assertNotIn("risultati garantiti", cleaned.lower())


if __name__ == "__main__":
    unittest.main()

import unittest

from emailgenius.scoring import evaluate_transition50_eligibility
from emailgenius.types import CompanySignals


class ScoringTests(unittest.TestCase):
    def test_facility_threshold_base_rate(self) -> None:
        signals = CompanySignals(facility_reduction_pct=3.0)
        result = evaluate_transition50_eligibility(signals)
        self.assertTrue(result.eligible)
        self.assertEqual(result.estimated_credit_rate, 35)

    def test_process_threshold_mid_rate(self) -> None:
        signals = CompanySignals(process_reduction_pct=10.0)
        result = evaluate_transition50_eligibility(signals)
        self.assertTrue(result.eligible)
        self.assertEqual(result.estimated_credit_rate, 40)

    def test_high_rate(self) -> None:
        signals = CompanySignals(facility_reduction_pct=12.0)
        result = evaluate_transition50_eligibility(signals)
        self.assertTrue(result.eligible)
        self.assertEqual(result.estimated_credit_rate, 45)

    def test_not_eligible(self) -> None:
        signals = CompanySignals(facility_reduction_pct=2.5, process_reduction_pct=4.9)
        result = evaluate_transition50_eligibility(signals)
        self.assertFalse(result.eligible)
        self.assertIsNone(result.estimated_credit_rate)


if __name__ == "__main__":
    unittest.main()

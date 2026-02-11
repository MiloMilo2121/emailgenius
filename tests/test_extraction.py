import unittest

from emailgenius.extraction import infer_company_signals


class ExtractionTests(unittest.TestCase):
    def test_extracts_energy_signals(self) -> None:
        text = """
        Nel nostro stabilimento abbiamo raggiunto una riduzione consumi energetici del 7,2%.
        Sulla linea di processo principale abbiamo ottenuto una riduzione del 5.4%.
        Pubblichiamo il bilancio di sostenibilita e il report ESG annuale.
        Il sito usa soluzioni Industria 4.0 con MES e IoT.
        """
        signals = infer_company_signals(text)

        self.assertEqual(signals.facility_reduction_pct, 7.2)
        self.assertEqual(signals.process_reduction_pct, 5.4)
        self.assertTrue(signals.has_esg_report)
        self.assertTrue(signals.has_industry40_signals)
        self.assertGreater(len(signals.evidence), 0)

    def test_handles_no_signals(self) -> None:
        text = "Sito vetrina senza dati energetici rilevanti."
        signals = infer_company_signals(text)

        self.assertIsNone(signals.facility_reduction_pct)
        self.assertIsNone(signals.process_reduction_pct)
        self.assertFalse(signals.has_esg_report)
        self.assertFalse(signals.has_industry40_signals)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from emailgenius.profiles import load_parent_profile


class ProfileTests(unittest.TestCase):
    def test_load_profile_with_slug_override(self) -> None:
        payload = textwrap.dedent(
            """
            company_name: Azienda Madre A
            tone: formale-consulenziale
            offer_catalog:
              - Soluzione X
            icp:
              - PMI manifatturiere
            proof_points:
              - Case study settore plastica
            objections:
              - Budget limitato
            cta_policy: call conoscitiva 20-30 min
            no_go_claims:
              - garantito
            compliance_notes:
              - usare solo dati pubblici
            """
        ).strip()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profile.yaml"
            path.write_text(payload, encoding="utf-8")
            profile = load_parent_profile(path, slug_override="azienda-a")

        self.assertEqual(profile.slug, "azienda-a")
        self.assertEqual(profile.company_name, "Azienda Madre A")
        self.assertIn("Soluzione X", profile.offer_catalog)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from emailgenius.sheets import _build_sheet_title
from emailgenius.sheets import _normalize_drive_folder_id
from emailgenius.sheets import _oauth_local_port


class SheetsTests(unittest.TestCase):
    def test_normalize_drive_folder_id_from_plain_id(self) -> None:
        folder_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz123456"
        self.assertEqual(_normalize_drive_folder_id(folder_id), folder_id)

    def test_normalize_drive_folder_id_from_folders_url(self) -> None:
        url = "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456?usp=sharing"
        self.assertEqual(_normalize_drive_folder_id(url), "1AbCdEfGhIjKlMnOpQrStUvWxYz123456")

    def test_normalize_drive_folder_id_from_open_url(self) -> None:
        url = "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz123456"
        self.assertEqual(_normalize_drive_folder_id(url), "1AbCdEfGhIjKlMnOpQrStUvWxYz123456")

    def test_normalize_drive_folder_id_rejects_invalid_url(self) -> None:
        self.assertEqual(_normalize_drive_folder_id("notaurl"), "notaurl")
        self.assertEqual(_normalize_drive_folder_id(""), "")

    def test_oauth_local_port_defaults_and_validation(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self.assertEqual(_oauth_local_port(), 53877)
        with patch.dict(os.environ, {"EMAILGENIUS_GOOGLE_OAUTH_LOCAL_PORT": "56000"}, clear=False):
            self.assertEqual(_oauth_local_port(), 56000)
        with patch.dict(os.environ, {"EMAILGENIUS_GOOGLE_OAUTH_LOCAL_PORT": "bad"}, clear=False):
            self.assertEqual(_oauth_local_port(), 53877)

    def test_build_sheet_title_includes_campaign_or_parent_and_date(self) -> None:
        title = _build_sheet_title(
            sheet_title="EmailGenius AB Enriched",
            parent_slug="contributo-facile",
            campaign_id="f1930196-79b3-4eb1-91b5-332ef5c5a845",
        )
        self.assertIn("contributo-facile", title)
        self.assertRegex(title, r"20[0-9]{2}-[0-9]{2}-[0-9]{2}")

    def test_build_sheet_title_uses_campaign_when_parent_missing(self) -> None:
        title = _build_sheet_title(
            sheet_title="EmailGenius",
            parent_slug=None,
            campaign_id="f1930196-79b3-4eb1-91b5-332ef5c5a845",
        )
        self.assertIn("campaign-f1930196", title)


if __name__ == "__main__":
    unittest.main()

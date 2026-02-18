from __future__ import annotations

import unittest

from emailgenius.sheets import _normalize_drive_folder_id


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


if __name__ == "__main__":
    unittest.main()

import base64
import io
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

from server import build_table_menu_url, create_app


class QRManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "cafe.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "PUBLIC_BASE_URL": "https://cafe.example.test",
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.client = self.app.test_client()
        initialized = self.app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(initialized.exit_code, 0, initialized.output)
        with patch.dict(
            os.environ,
            {
                "OWNER_USERNAME": "cafe-owner",
                "OWNER_PASSWORD": "first-secure-password",
            },
        ):
            created = self.app.test_cli_runner().invoke(args=["setup-owner"])
        self.assertEqual(created.exit_code, 0, created.output)

    def tearDown(self):
        self.temp_dir.cleanup()

    def login(self, client=None):
        client = client or self.client
        return client.post(
            "/owner/login",
            data={
                "username": "cafe-owner",
                "password": "first-secure-password",
            },
            follow_redirects=True,
        )

    def test_owner_can_preview_and_download_valid_png_qr(self):
        self.login()
        response = self.client.post(
            "/owner/qr",
            data={"table_number": "2", "custom_table_number": ""},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://juniper.cafe.example.test/menu?table=2", response.data)
        preview = re.search(
            rb'src="data:image/png;base64,([^"]+)"', response.data
        )
        self.assertIsNotNone(preview)
        preview_bytes = base64.b64decode(preview.group(1))
        with Image.open(io.BytesIO(preview_bytes)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 100)

        download = self.client.get("/owner/qr/2.png")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "image/png")
        self.assertIn("juniper-table-2.png", download.headers["Content-Disposition"])
        self.assertTrue(download.data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(download.data)) as image:
            self.assertEqual(image.format, "PNG")

    def test_owner_can_create_valid_custom_table_qr(self):
        self.login()
        response = self.client.post(
            "/owner/qr",
            data={"table_number": "", "custom_table_number": "21"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://juniper.cafe.example.test/menu?table=21", response.data)
        with closing(sqlite3.connect(self.database)) as db:
            active = db.execute(
                """
                SELECT active FROM cafe_tables
                WHERE cafe_id = (SELECT id FROM cafes WHERE slug = 'juniper')
                  AND display_number = 21
                """
            ).fetchone()
        self.assertEqual(active, (1,))
        self.assertEqual(self.client.get("/menu?table=21").status_code, 200)

    def test_invalid_or_inactive_table_numbers_are_rejected(self):
        self.login()
        for table_number in ("abc", "0", "1000"):
            with self.subTest(table_number=table_number):
                self.client.post(
                    "/owner/qr",
                    data={"custom_table_number": table_number},
                )
                with closing(sqlite3.connect(self.database)) as db:
                    exists = db.execute(
                        "SELECT 1 FROM cafe_tables WHERE table_number = ?",
                        (int(table_number) if table_number.isdigit() else -1,),
                    ).fetchone()
                self.assertIsNone(exists)

        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE cafe_tables SET active = 0 WHERE table_number = 3")
            db.commit()
        rejected = self.client.post(
            "/owner/qr",
            data={"table_number": "3", "custom_table_number": ""},
            follow_redirects=True,
        )
        self.assertIn(b"That table is inactive", rejected.data)
        self.assertEqual(self.client.get("/owner/qr/3.png").status_code, 404)

    def test_qr_manager_requires_authentication_and_csrf(self):
        guest = self.client.get("/owner/qr")
        self.assertEqual(guest.status_code, 302)

        protected_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "PUBLIC_BASE_URL": "https://cafe.example.test",
                "WTF_CSRF_ENABLED": True,
            }
        )
        client = protected_app.test_client()
        page = client.get("/owner/login")
        token = re.search(
            rb'name="csrf_token" value="([^"]+)"', page.data
        ).group(1).decode()
        client.post(
            "/owner/login",
            data={
                "csrf_token": token,
                "username": "cafe-owner",
                "password": "first-secure-password",
            },
        )
        page = client.get("/owner/qr")
        rejected = client.post(
            "/owner/qr",
            data={"table_number": "2", "custom_table_number": ""},
        )
        self.assertEqual(rejected.status_code, 400)
        token = re.search(
            rb'name="csrf_token" value="([^"]+)"', page.data
        ).group(1).decode()
        accepted = client.post(
            "/owner/qr",
            data={
                "csrf_token": token,
                "table_number": "2",
                "custom_table_number": "",
            },
        )
        self.assertEqual(accepted.status_code, 302)

    def test_qr_manager_does_not_trust_external_host_header(self):
        unconfigured_app = create_app(
            {
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "PUBLIC_BASE_URL": "",
                "WTF_CSRF_ENABLED": False,
            }
        )
        with unconfigured_app.test_request_context(
            "/owner/qr?table=2",
            base_url="https://attacker.example",
        ):
            from flask import g

            g.cafe = {"slug": "juniper"}
            with self.assertRaisesRegex(ValueError, "PUBLIC_BASE_URL"):
                build_table_menu_url(2)


if __name__ == "__main__":
    unittest.main()

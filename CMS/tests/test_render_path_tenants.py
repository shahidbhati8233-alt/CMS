import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

from server import create_app


class RenderPathTenantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "cafe.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "BASE_DOMAIN": "localhost",
                "PUBLIC_BASE_URL": "https://cms-cafe.onrender.com",
                "TENANT_ROUTING_MODE": "path",
                "WTF_CSRF_ENABLED": False,
                "RATELIMIT_ENABLED": False,
            }
        )
        self.client = self.app.test_client()
        self.base_url = "https://cms-cafe.onrender.com"

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, slug, password):
        return self.client.post(
            "/signup",
            base_url=self.base_url,
            data={
                "cafe_name": f"{slug.title()} Café",
                "slug": slug,
                "password": password,
            },
        )

    def test_signup_redirects_to_working_path_based_owner_login(self):
        response = self.register("harbor-cafe", "harbor-secure-password")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "https://cms-cafe.onrender.com/c/harbor-cafe/owner/login?welcome=1",
        )

        login_page = self.client.get(
            "/c/harbor-cafe/owner/login?welcome=1",
            base_url=self.base_url,
        )
        self.assertEqual(login_page.status_code, 200)
        self.assertIn(b"Harbor-Cafe Caf", login_page.data)
        self.assertIn(b"profile is ready", login_page.data)

        login = self.client.post(
            "/c/harbor-cafe/owner/login",
            base_url=self.base_url,
            data={"password": "harbor-secure-password"},
        )
        self.assertEqual(login.status_code, 302)
        self.assertTrue(
            login.headers["Location"].endswith("/c/harbor-cafe/owner"),
            login.headers["Location"],
        )

        dashboard = self.client.get(
            "/c/harbor-cafe/owner",
            base_url=self.base_url,
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Harbor-Cafe Caf", dashboard.data)

    def test_path_tenants_keep_menu_data_isolated(self):
        self.assertEqual(
            self.register("north-cafe", "north-secure-password").status_code,
            302,
        )
        self.assertEqual(
            self.register("south-cafe", "south-secure-password").status_code,
            302,
        )

        north = self.app.test_client()
        south = self.app.test_client()
        with sqlite3.connect(self.database) as db:
            north_id = db.execute(
                "SELECT id FROM cafes WHERE slug = 'north-cafe'"
            ).fetchone()[0]
            category_id = db.execute(
                "SELECT id FROM menu_categories WHERE cafe_id = ? AND name = 'Coffee'",
                (north_id,),
            ).fetchone()[0]
            db.execute(
                """
                INSERT INTO menu_items
                    (cafe_id, category_id, name, description, price_paise)
                VALUES (?, ?, 'North-only item', 'Tenant isolation test', 500)
                """,
                (north_id, category_id),
            )
            db.commit()

        north_menu = north.get(
            "/c/north-cafe/menu?table=1",
            base_url=self.base_url,
        )
        south_menu = south.get(
            "/c/south-cafe/menu?table=1",
            base_url=self.base_url,
        )
        self.assertEqual(north_menu.status_code, 200)
        self.assertEqual(south_menu.status_code, 200)
        self.assertIn(b"North-only item", north_menu.data)
        self.assertNotIn(b"North-only item", south_menu.data)

        invalid = self.client.get(
            "/c/not-a-real-cafe/menu?table=1",
            base_url=self.base_url,
        )
        self.assertEqual(invalid.status_code, 404)

    def test_qr_urls_use_path_based_tenant_address(self):
        self.assertEqual(
            self.register("qr-cafe", "qr-secure-password").status_code,
            302,
        )
        login = self.client.post(
            "/c/qr-cafe/owner/login",
            base_url=self.base_url,
            data={"password": "qr-secure-password"},
        )
        self.assertEqual(login.status_code, 302)

        qr_page = self.client.get(
            "/c/qr-cafe/owner/qr?table=1",
            base_url=self.base_url,
        )
        self.assertEqual(qr_page.status_code, 200)
        self.assertIn(
            b"https://cms-cafe.onrender.com/c/qr-cafe/menu?table=1",
            qr_page.data,
        )


if __name__ == "__main__":
    unittest.main()

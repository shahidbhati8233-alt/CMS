import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

from server import create_app
from werkzeug.security import check_password_hash


class OwnerMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "cafe.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.client = self.app.test_client()
        setup = self.app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(setup.exit_code, 0, setup.output)
        self.create_owner("cafe-owner", "first-secure-password")

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_owner(self, username, password):
        with patch.dict(
            os.environ,
            {"OWNER_USERNAME": username, "OWNER_PASSWORD": password},
        ):
            result = self.app.test_cli_runner().invoke(args=["setup-owner"])
        self.assertEqual(result.exit_code, 0, result.output)

    def login(self, username="cafe-owner", password="first-secure-password"):
        return self.client.post(
            "/owner/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def test_owner_pages_require_authentication(self):
        for path in ("/owner", "/owner/menu", "/owner/items/new"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/owner/login", response.location)
        response = self.client.post(
            "/owner/categories", data={"name": "Guest category"}
        )
        self.assertEqual(response.status_code, 302)

    def test_owner_account_uses_password_hash_and_can_be_reset(self):
        with closing(sqlite3.connect(self.database)) as db:
            password_hash = db.execute(
                "SELECT password_hash FROM users WHERE username = ?",
                ("cafe-owner",),
            ).fetchone()[0]
        self.assertNotEqual(password_hash, "first-secure-password")
        self.assertTrue(check_password_hash(password_hash, "first-secure-password"))

        self.create_owner("cafe-owner", "second-secure-password")
        with closing(sqlite3.connect(self.database)) as db:
            reset_hash = db.execute(
                "SELECT password_hash FROM users WHERE username = ?",
                ("cafe-owner",),
            ).fetchone()[0]
        self.assertTrue(check_password_hash(reset_hash, "second-secure-password"))
        self.assertFalse(check_password_hash(reset_hash, "first-secure-password"))

    def test_owner_setup_rejects_short_passwords(self):
        with patch.dict(
            os.environ,
            {"OWNER_USERNAME": "short-pass", "OWNER_PASSWORD": "too-short"},
        ):
            result = self.app.test_cli_runner().invoke(args=["setup-owner"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("between 12 and 128", result.output)
        with closing(sqlite3.connect(self.database)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'short-pass'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_init_db_migrates_milestone_two_menu_items(self):
        legacy_database = str(Path(self.temp_dir.name) / "legacy.sqlite3")
        with closing(sqlite3.connect(legacy_database)) as db:
            db.executescript(
                """
                CREATE TABLE menu_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_order INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE menu_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL REFERENCES menu_categories(id),
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    price_paise INTEGER NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1,
                    image_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO menu_categories (name) VALUES ('Coffee');
                INSERT INTO menu_items (category_id, name, price_paise)
                VALUES (1, 'Legacy latte', 500);
                """
            )
        legacy_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": legacy_database,
            }
        )
        result = legacy_app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(result.exit_code, 0, result.output)
        with closing(sqlite3.connect(legacy_database)) as db:
            visible = db.execute(
                "SELECT visible FROM menu_items WHERE name = 'Legacy latte'"
            ).fetchone()[0]
            users_table = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()
        self.assertEqual(visible, 1)
        self.assertIsNotNone(users_table)

    def test_owner_can_manage_categories_and_menu_items(self):
        login_response = self.login()
        self.assertIn(b"Owner overview", login_response.data)

        manager = self.client.get("/owner/menu")
        self.assertEqual(manager.status_code, 200)
        self.assertIn(b"Menu manager", manager.data)

        added_category = self.client.post(
            "/owner/categories", data={"name": "Seasonal"}, follow_redirects=True
        )
        self.assertEqual(added_category.status_code, 200)
        self.assertIn(b"Seasonal", added_category.data)
        with closing(sqlite3.connect(self.database)) as db:
            category_id = db.execute(
                "SELECT id FROM menu_categories WHERE name = 'Seasonal'"
            ).fetchone()[0]

        renamed = self.client.post(
            f"/owner/categories/{category_id}",
            data={"name": "Seasonal favourites"},
            follow_redirects=True,
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertIn(b"Seasonal favourites", renamed.data)

        created_item = self.client.post(
            "/owner/items/new",
            data={
                "name": "Rose Latte",
                "category_id": str(category_id),
                "description": "Espresso with a floral finish.",
                "price": "12.35",
                "available": "1",
                "image_url": "https://images.example.test/rose-latte.jpg",
            },
            follow_redirects=True,
        )
        self.assertEqual(created_item.status_code, 200)
        self.assertIn(b"Rose Latte", created_item.data)
        with closing(sqlite3.connect(self.database)) as db:
            saved = db.execute(
                "SELECT id, price_paise, image_path FROM menu_items WHERE name = ?",
                ("Rose Latte",),
            ).fetchone()
        item_id, price_paise, image_url = saved
        self.assertEqual(price_paise, 1235)
        self.assertEqual(image_url, "https://images.example.test/rose-latte.jpg")
        edit_page = self.client.get(f"/owner/items/{item_id}/edit")
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(b'value="12.35"', edit_page.data)

        public_menu = self.client.get("/menu?table=1")
        self.assertIn(b"Rose Latte", public_menu.data)
        self.assertIn(b"12.35", public_menu.data)

        unavailable = self.client.post(
            f"/owner/items/{item_id}/edit",
            data={
                "name": "Rose Latte",
                "category_id": str(category_id),
                "description": "Espresso with a floral finish.",
                "price": "13.00",
                "available": "0",
                "image_url": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(unavailable.status_code, 200)
        self.assertIn(b"Currently unavailable", self.client.get("/menu?table=1").data)

        hidden = self.client.post(
            f"/owner/items/{item_id}/visibility",
            data={"visible": "0"},
            follow_redirects=True,
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertNotIn(b"Rose Latte", self.client.get("/menu?table=1").data)

    def test_removing_an_item_keeps_historical_order_snapshots(self):
        self.login()
        with closing(sqlite3.connect(self.database)) as db:
            item_id = db.execute(
                "SELECT id FROM menu_items WHERE name = 'Cappuccino'"
            ).fetchone()[0]
        order = self.client.post(
            "/orders?table=2",
            json={"items": [{"id": item_id, "quantity": 1}]},
        )
        self.assertEqual(order.status_code, 201)
        removed = self.client.post(
            f"/owner/items/{item_id}/delete", follow_redirects=True
        )
        self.assertEqual(removed.status_code, 200)
        with closing(sqlite3.connect(self.database)) as db:
            snapshot = db.execute(
                """
                SELECT menu_item_id, item_name, unit_price_paise
                FROM order_items
                WHERE order_id = (SELECT id FROM orders WHERE order_number = ?)
                """,
                (order.get_json()["order_number"],),
            ).fetchone()
        self.assertEqual(snapshot, (None, "Cappuccino", 16000))

    def test_invalid_menu_values_are_rejected(self):
        self.client.post("/owner/login", data={
            "username": "cafe-owner",
            "password": "first-secure-password",
        })
        invalid = self.client.post(
            "/owner/items/new",
            data={
                "name": "Bad price",
                "category_id": "1",
                "description": "",
                "price": "12.345",
                "available": "1",
                "image_url": "http://images.example.test/item.jpg",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b"no more than two decimal places", invalid.data)
        with closing(sqlite3.connect(self.database)) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM menu_items WHERE name = 'Bad price'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_owner_login_requires_csrf_token(self):
        csrf_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "WTF_CSRF_ENABLED": True,
            }
        )
        client = csrf_app.test_client()
        response = client.post(
            "/owner/login",
            data={
                "username": "cafe-owner",
                "password": "first-secure-password",
            },
        )
        self.assertEqual(response.status_code, 400)
        login_page = client.get("/owner/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data)
        self.assertIsNotNone(token)
        accepted = client.post(
            "/owner/login",
            data={
                "csrf_token": token.group(1).decode(),
                "username": "cafe-owner",
                "password": "first-secure-password",
            },
            follow_redirects=True,
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertIn(b"Owner overview", accepted.data)


if __name__ == "__main__":
    unittest.main()

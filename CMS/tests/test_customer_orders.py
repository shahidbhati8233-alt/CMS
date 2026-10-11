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


class CustomerOrderTests(unittest.TestCase):
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
        result = self.app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(result.exit_code, 0, result.output)

    def tearDown(self):
        self.temp_dir.cleanup()

    def item_id(self, name):
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute("SELECT id FROM menu_items WHERE name = ?", (name,)).fetchone()
        return row[0]

    def test_menu_request_initializes_complete_schema_and_active_table_one(self):
        database = str(Path(self.temp_dir.name) / "first-request.sqlite3")
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": database,
            }
        )
        response = app.test_client().get("/menu?table=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b">1</strong>", response.data)

        with closing(sqlite3.connect(database)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            active_table = db.execute(
                "SELECT active FROM cafe_tables WHERE table_number = 1"
            ).fetchone()
        self.assertTrue(
            {
                "cafe_tables",
                "menu_categories",
                "menu_items",
                "users",
                "orders",
                "order_items",
            }.issubset(tables)
        )
        self.assertEqual(active_table, (1,))

    def test_first_menu_request_adds_missing_table_without_erasing_existing_menu(self):
        database = str(Path(self.temp_dir.name) / "legacy-menu.sqlite3")
        with closing(sqlite3.connect(database)) as db:
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
                INSERT INTO menu_items (category_id, name, description, price_paise)
                VALUES (1, 'Saved latte', 'Existing menu record', 750);
                """
            )
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": database,
            }
        )

        response = app.test_client().get("/menu?table=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Saved latte", response.data)
        with closing(sqlite3.connect(database)) as db:
            saved_item = db.execute(
                "SELECT name, price_paise FROM menu_items WHERE id = 1"
            ).fetchone()
            table_one = db.execute(
                "SELECT active FROM cafe_tables WHERE table_number = 1"
            ).fetchone()
        self.assertEqual(saved_item, ("Saved latte", 750))
        self.assertEqual(table_one, (1,))

    def test_missing_table_registry_is_recreated_without_losing_orders_or_users(self):
        with closing(sqlite3.connect(self.database)) as db:
            cappuccino_id = db.execute(
                "SELECT id FROM menu_items WHERE name = 'Cappuccino'"
            ).fetchone()[0]
        owner_setup = self.app.test_cli_runner()
        with patch.dict(
            os.environ,
            {
                "OWNER_USERNAME": "preserved-owner",
                "OWNER_PASSWORD": "preserved-password-123",
            },
        ):
            result = owner_setup.invoke(args=["setup-owner"])
        self.assertEqual(result.exit_code, 0, result.output)
        order_response = self.client.post(
            "/orders?table=1",
            json={"items": [{"id": cappuccino_id, "quantity": 1}]},
        )
        self.assertEqual(order_response.status_code, 201)
        order_number = order_response.get_json()["order_number"]

        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE cafe_tables")
            db.commit()

        reopened_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
            }
        )
        response = reopened_app.test_client().get("/menu?table=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cappuccino", response.data)
        with closing(sqlite3.connect(self.database)) as db:
            table_one = db.execute(
                "SELECT active FROM cafe_tables WHERE table_number = 1"
            ).fetchone()
            order_count = db.execute(
                "SELECT COUNT(*) FROM orders WHERE order_number = ?",
                (order_number,),
            ).fetchone()[0]
            order_item = db.execute(
                """
                SELECT item_name, unit_price_paise
                FROM order_items
                WHERE order_id = (SELECT id FROM orders WHERE order_number = ?)
                """,
                (order_number,),
            ).fetchone()
            owner_count = db.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'preserved-owner'"
            ).fetchone()[0]
        self.assertEqual(table_one, (1,))
        self.assertEqual(order_count, 1)
        self.assertEqual(order_item, ("Cappuccino", 16000))
        self.assertEqual(owner_count, 1)

    def test_menu_shows_table_and_rejects_invalid_tables(self):
        response = self.client.get("/menu?table=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your table", response.data)
        self.assertIn(b">2</strong>", response.data)

        for table in ("abc", "0", "21", "9999"):
            with self.subTest(table=table):
                self.assertNotEqual(
                    self.client.get(f"/menu?table={table}").status_code, 200
                )

    def test_order_uses_database_price_and_saves_item_snapshots(self):
        cappuccino_id = self.item_id("Cappuccino")
        response = self.client.post(
            "/orders?table=3",
            json={
                "instructions": "Oat milk, please",
                "items": [{"id": cappuccino_id, "quantity": 2, "price": 1}],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        order = response.get_json()
        self.assertIn("Your order has been sent to the café.", order["message"])
        self.assertRegex(order["order_number"], r"^CF-\d{8}-[A-F0-9]{12}$")

        with closing(sqlite3.connect(self.database)) as db:
            saved_order = db.execute(
                "SELECT table_number, status, instructions, total_paise FROM orders WHERE order_number = ?",
                (order["order_number"],),
            ).fetchone()
            saved_item = db.execute(
                "SELECT item_name, unit_price_paise, quantity FROM order_items WHERE order_id = (SELECT id FROM orders WHERE order_number = ?)",
                (order["order_number"],),
            ).fetchone()
        self.assertEqual(saved_order, (3, "New", "Oat milk, please", 32000))
        self.assertEqual(saved_item, ("Cappuccino", 16000, 2))

    def test_invalid_quantities_and_inactive_items_are_rejected(self):
        cappuccino_id = self.item_id("Cappuccino")
        for quantity in (0, 21, True):
            with self.subTest(quantity=quantity):
                response = self.client.post(
                    "/orders?table=1",
                    json={"items": [{"id": cappuccino_id, "quantity": quantity}]},
                )
                self.assertEqual(response.status_code, 400)

        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE menu_items SET available = 0 WHERE id = ?", (cappuccino_id,))
            db.commit()
        response = self.client.post(
            "/orders?table=1",
            json={"items": [{"id": cappuccino_id, "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer available", response.get_json()["error"])

    def test_hidden_menu_item_cannot_be_ordered_by_submitting_its_id(self):
        cappuccino_id = self.item_id("Cappuccino")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "UPDATE menu_items SET visible = 0 WHERE id = ?",
                (cappuccino_id,),
            )
            db.commit()
        menu_response = self.client.get("/menu?table=1")
        self.assertNotIn(b"Cappuccino", menu_response.data)

        response = self.client.post(
            "/orders?table=1",
            json={"items": [{"id": cappuccino_id, "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no longer available", response.get_json()["error"])

    def test_empty_menu_has_helpful_state(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DELETE FROM menu_items")
            db.commit()
        response = self.client.get("/menu?table=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Our menu is getting ready", response.data)

    def test_order_endpoint_enforces_csrf(self):
        protected_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "WTF_CSRF_ENABLED": True,
            }
        )
        client = protected_app.test_client()
        rejected = client.post(
            "/orders?table=1",
            json={"items": [{"id": self.item_id("Cappuccino"), "quantity": 1}]},
        )
        self.assertEqual(rejected.status_code, 400)

        menu_response = client.get("/menu?table=1")
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', menu_response.data)
        self.assertIsNotNone(token)
        accepted = client.post(
            "/orders?table=1",
            json={"items": [{"id": self.item_id("Cappuccino"), "quantity": 1}]},
            headers={"X-CSRFToken": token.group(1).decode()},
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())

    def test_order_endpoint_rate_limits_repeated_submissions(self):
        cappuccino_id = self.item_id("Cappuccino")
        for _ in range(30):
            response = self.client.post(
                "/orders?table=1",
                json={"items": [{"id": cappuccino_id, "quantity": 1}]},
            )
            self.assertEqual(response.status_code, 201)
        limited = self.client.post(
            "/orders?table=1",
            json={"items": [{"id": cappuccino_id, "quantity": 1}]},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Too many orders", limited.get_json()["error"])


if __name__ == "__main__":
    unittest.main()

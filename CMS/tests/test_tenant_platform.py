import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

from server import create_app


class TenantPlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "cafe.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "BASE_DOMAIN": "localhost",
                "PUBLIC_BASE_URL": "",
                "WTF_CSRF_ENABLED": False,
                "RATELIMIT_ENABLED": False,
            }
        )
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/").status_code, 200)

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, slug, password, client=None):
        client = client or self.client
        return client.post(
            "/signup",
            data={
                "cafe_name": f"{slug.title()} Café",
                "slug": slug,
                "password": password,
            },
        )

    def test_home_and_self_service_cafe_registration(self):
        home = self.client.get("/")
        self.assertIn(b"Create your caf", home.data)
        signup_page = self.client.get("/signup")
        self.assertNotIn(b'name="username"', signup_page.data)

        response = self.register("harbor-cafe", "harbor-secure-password")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "http://harbor-cafe.localhost/owner/login?welcome=1",
        )
        tenant = self.client.get(
            "/owner/login?welcome=1", base_url="http://harbor-cafe.localhost"
        )
        self.assertEqual(tenant.status_code, 200)
        self.assertIn(b"Harbor-Cafe Caf", tenant.data)
        self.assertIn(b"profile is ready", tenant.data)
        self.assertNotIn(b'name="username"', tenant.data)

        login = self.client.post(
            "/owner/login",
            data={"password": "harbor-secure-password"},
            base_url="http://harbor-cafe.localhost",
        )
        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/owner"))

        with closing(sqlite3.connect(self.database)) as db:
            cafe = db.execute(
                "SELECT name, slug FROM cafes WHERE slug = 'harbor-cafe'"
            ).fetchone()
            table = db.execute(
                """
                SELECT display_number, active FROM cafe_tables
                WHERE cafe_id = (SELECT id FROM cafes WHERE slug = 'harbor-cafe')
                """
            ).fetchone()
            stored_password = db.execute(
                """
                SELECT password_hash FROM users
                WHERE cafe_id = (SELECT id FROM cafes WHERE slug = 'harbor-cafe')
                """
            ).fetchone()[0]
        self.assertEqual(cafe, ("Harbor-Cafe Café", "harbor-cafe"))
        self.assertEqual(table, (1, 1))
        self.assertNotEqual(stored_password, "harbor-secure-password")
        self.assertTrue(stored_password.startswith("scrypt:"))

    def test_owner_credentials_and_orders_are_scoped_to_the_cafe(self):
        self.assertEqual(
            self.register("north-cafe", "north-secure-password").status_code, 302
        )
        self.assertEqual(
            self.register("south-cafe", "south-secure-password").status_code, 302
        )
        north = self.app.test_client()
        south = self.app.test_client()

        wrong_tenant_password = north.post(
            "/owner/login",
            data={
                "password": "south-secure-password",
            },
            base_url="http://north-cafe.localhost",
        )
        self.assertEqual(wrong_tenant_password.status_code, 401)
        login = north.post(
            "/owner/login",
            data={
                "password": "north-secure-password",
            },
            base_url="http://north-cafe.localhost",
        )
        self.assertEqual(login.status_code, 302)

        with closing(sqlite3.connect(self.database)) as db:
            cafe_id = db.execute(
                "SELECT id FROM cafes WHERE slug = 'north-cafe'"
            ).fetchone()[0]
            category_id = db.execute(
                "SELECT id FROM menu_categories WHERE cafe_id = ? AND name = 'Coffee'",
                (cafe_id,),
            ).fetchone()[0]
            db.execute(
                """
                INSERT INTO menu_items
                    (cafe_id, category_id, name, description, price_paise)
                VALUES (?, ?, 'North house coffee', 'Tenant-only item', 550)
                """,
                (cafe_id, category_id),
            )
            item_id = db.execute(
                "SELECT id FROM menu_items WHERE cafe_id = ?",
                (cafe_id,),
            ).fetchone()[0]
            db.commit()

        north_menu = north.get(
            "/menu?table=1", base_url="http://north-cafe.localhost"
        )
        south_menu = south.get(
            "/menu?table=1", base_url="http://south-cafe.localhost"
        )
        self.assertIn(b"North house coffee", north_menu.data)
        self.assertNotIn(b"North house coffee", south_menu.data)
        self.assertNotIn(b"Cappuccino", north_menu.data)

        order = north.post(
            "/orders?table=1",
            json={"items": [{"id": item_id, "quantity": 2}]},
            base_url="http://north-cafe.localhost",
        )
        self.assertEqual(order.status_code, 201, order.get_json())
        self.assertEqual(order.get_json()["message"], "Your order has been sent to the café. Please wait for staff confirmation.")

        south_login = south.post(
            "/owner/login",
            data={
                "username": "cafe-owner",
                "password": "south-secure-password",
            },
            base_url="http://south-cafe.localhost",
        )
        self.assertEqual(south_login.status_code, 302)
        south_orders = south.get(
            "/owner/orders", base_url="http://south-cafe.localhost"
        )
        self.assertEqual(south_orders.status_code, 200)
        self.assertNotIn(order.get_json()["order_number"].encode(), south_orders.data)
        with closing(sqlite3.connect(self.database)) as db:
            owner_counts = db.execute(
                "SELECT cafe_id, COUNT(*) FROM users GROUP BY cafe_id ORDER BY cafe_id"
            ).fetchall()
            saved_order = db.execute(
                "SELECT cafe_id, total_paise FROM orders WHERE order_number = ?",
                (order.get_json()["order_number"],),
            ).fetchone()
        self.assertEqual(len(owner_counts), 2)
        self.assertEqual(saved_order, (cafe_id, 1100))

    def test_legacy_migration_preserves_users_menu_and_order_snapshots(self):
        database = str(Path(self.temp_dir.name) / "legacy.sqlite3")
        with closing(sqlite3.connect(database)) as db:
            db.executescript(
                """
                CREATE TABLE cafe_tables (
                    table_number INTEGER PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 1
                );
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
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'owner',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT NOT NULL UNIQUE,
                    table_number INTEGER NOT NULL REFERENCES cafe_tables(table_number),
                    status TEXT NOT NULL DEFAULT 'New',
                    instructions TEXT NOT NULL DEFAULT '',
                    total_paise INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    menu_item_id INTEGER REFERENCES menu_items(id) ON DELETE SET NULL,
                    item_name TEXT NOT NULL,
                    unit_price_paise INTEGER NOT NULL,
                    quantity INTEGER NOT NULL
                );
                INSERT INTO cafe_tables (table_number) VALUES (1);
                INSERT INTO menu_categories (name) VALUES ('Coffee');
                INSERT INTO menu_items (category_id, name, price_paise)
                    VALUES (1, 'Legacy latte', 725);
                INSERT INTO users (username, password_hash) VALUES ('legacy-owner', 'old-hash');
                INSERT INTO orders
                    (order_number, table_number, total_paise)
                    VALUES ('CF-LEGACY-ORDER', 1, 725);
                INSERT INTO order_items
                    (order_id, menu_item_id, item_name, unit_price_paise, quantity)
                    VALUES (1, 1, 'Legacy latte snapshot', 725, 1);
                """
            )

        migrated_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": database,
                "BASE_DOMAIN": "localhost",
                "WTF_CSRF_ENABLED": False,
            }
        )
        client = migrated_app.test_client()
        menu = client.get("/menu?table=1")
        self.assertEqual(menu.status_code, 200)
        self.assertIn(b"Legacy latte", menu.data)
        registration = client.post(
            "/signup",
            data={
                "cafe_name": "New Coffee House",
                "slug": "new-coffee",
                "username": "new-owner",
                "password": "new-secure-password",
            },
        )
        self.assertEqual(registration.status_code, 302)

        with closing(sqlite3.connect(database)) as db:
            legacy_order = db.execute(
                """
                SELECT order_number, total_paise, cafe_id
                FROM orders WHERE id = 1
                """
            ).fetchone()
            old_snapshot = db.execute(
                """
                SELECT item_name, unit_price_paise, quantity
                FROM order_items WHERE id = 1
                """
            ).fetchone()
            old_owner = db.execute(
                """
                SELECT username, cafe_id FROM users WHERE username = 'legacy-owner'
                """
            ).fetchone()
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(legacy_order, ("CF-LEGACY-ORDER", 725, 1))
        self.assertEqual(old_snapshot, ("Legacy latte snapshot", 725, 1))
        self.assertEqual(old_owner, ("legacy-owner", 1))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()

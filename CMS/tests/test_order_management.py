import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

from server import ORDER_STATUSES, create_app


class OrderManagementTests(unittest.TestCase):
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

    def create_order(self):
        with closing(sqlite3.connect(self.database)) as db:
            item_id = db.execute(
                "SELECT id FROM menu_items WHERE name = 'Cappuccino'"
            ).fetchone()[0]
        response = self.client.post(
            "/orders?table=4",
            json={
                "instructions": "Please bring water too.",
                "items": [{"id": item_id, "quantity": 2}],
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["order_number"]

    def test_orders_are_private_and_new_orders_are_highlighted(self):
        order_number = self.create_order()
        guest_response = self.client.get("/owner/orders")
        self.assertEqual(guest_response.status_code, 302)
        self.assertIn("/owner/login", guest_response.location)

        self.login()
        response = self.client.get("/owner/orders")
        self.assertEqual(response.status_code, 200)
        self.assertIn(order_number.encode(), response.data)
        self.assertIn(b"Table 4", response.data)
        self.assertIn(b"Please bring water too.", response.data)
        self.assertIn(b"2 \xc3\x97", response.data)
        self.assertIn("₹320.00".encode(), response.data)
        self.assertIn(b"NEW", response.data)
        self.assertIn(b"is-new-order", response.data)
        self.assertIn(b"UTC", response.data)
        self.assertIn(b"30 seconds", response.data)

    def test_authorized_owner_can_update_each_allowed_status(self):
        order_number = self.create_order()
        self.login()
        with closing(sqlite3.connect(self.database)) as db:
            order_id = db.execute(
                "SELECT id FROM orders WHERE order_number = ?", (order_number,)
            ).fetchone()[0]

        dashboard = self.client.get("/owner")
        self.assertRegex(dashboard.data.decode(), r"<strong>1</strong><span>New orders</span>")

        for status in ORDER_STATUSES:
            with self.subTest(status=status):
                response = self.client.post(
                    f"/owner/orders/{order_id}/status",
                    data={"status": status},
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                with closing(sqlite3.connect(self.database)) as db:
                    saved_status = db.execute(
                        "SELECT status FROM orders WHERE id = ?", (order_id,)
                    ).fetchone()[0]
                self.assertEqual(saved_status, status)
                if status == "Accepted":
                    dashboard = self.client.get("/owner")
                    self.assertRegex(
                        dashboard.data.decode(),
                        r"<strong>0</strong><span>New orders</span>",
                    )

        invalid = self.client.post(
            f"/owner/orders/{order_id}/status",
            data={"status": "Refunded"},
            follow_redirects=True,
        )
        self.assertIn(b"Choose a valid order status", invalid.data)
        with closing(sqlite3.connect(self.database)) as db:
            final_status = db.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()[0]
        self.assertEqual(final_status, "Cancelled")

    def test_customer_cannot_update_order_status(self):
        order_number = self.create_order()
        with closing(sqlite3.connect(self.database)) as db:
            order_id = db.execute(
                "SELECT id FROM orders WHERE order_number = ?", (order_number,)
            ).fetchone()[0]
        response = self.client.post(
            f"/owner/orders/{order_id}/status",
            data={"status": "Accepted"},
        )
        self.assertEqual(response.status_code, 302)
        with closing(sqlite3.connect(self.database)) as db:
            saved_status = db.execute(
                "SELECT status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()[0]
        self.assertEqual(saved_status, "New")

    def test_status_form_requires_csrf(self):
        order_number = self.create_order()
        with closing(sqlite3.connect(self.database)) as db:
            order_id = db.execute(
                "SELECT id FROM orders WHERE order_number = ?", (order_number,)
            ).fetchone()[0]
        protected_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-only-secret-key",
                "DATABASE": self.database,
                "WTF_CSRF_ENABLED": True,
            }
        )
        client = protected_app.test_client()
        login_page = client.get("/owner/login")
        login_token = re.search(
            rb'name="csrf_token" value="([^"]+)"', login_page.data
        ).group(1).decode()
        client.post(
            "/owner/login",
            data={
                "csrf_token": login_token,
                "username": "cafe-owner",
                "password": "first-secure-password",
            },
        )
        client.get("/owner/orders")
        rejected = client.post(
            f"/owner/orders/{order_id}/status", data={"status": "Accepted"}
        )
        self.assertEqual(rejected.status_code, 400)

        orders_page = client.get("/owner/orders")
        status_token = re.search(
            rb'name="csrf_token" value="([^"]+)"', orders_page.data
        ).group(1).decode()
        accepted = client.post(
            f"/owner/orders/{order_id}/status",
            data={"csrf_token": status_token, "status": "Accepted"},
        )
        self.assertEqual(accepted.status_code, 302)

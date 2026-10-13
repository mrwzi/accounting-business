"""Integration checks using temporary databases only.

Run with: py -3.14 -m unittest discover -s tests -v
"""
import importlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class AccountingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="accounting-workflow-")
        self.master_db = Path(self.temp_dir) / "master.db"
        self.ledger_dir = Path(self.temp_dir) / "ledgers"
        os.environ["ACCOUNTING_MASTER_DB_PATH"] = str(self.master_db)
        os.environ["ACCOUNTING_BUSINESS_DB_DIR"] = str(self.ledger_dir)
        os.environ["ALLOW_SIGNUPS"] = "0"
        for module in ("app", "db", "config", "accounting_recovered_core"):
            sys.modules.pop(module, None)
        self.module = importlib.import_module("app")
        self.flask_app = self.module._core.app
        self.flask_app.config.update(TESTING=True)
        self.client = self.flask_app.test_client()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        for key in ("ACCOUNTING_MASTER_DB_PATH", "ACCOUNTING_BUSINESS_DB_DIR", "ALLOW_SIGNUPS"):
            os.environ.pop(key, None)

    @staticmethod
    def _csrf(response):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if not match:
            raise AssertionError("No CSRF token found in response")
        return match.group(1).decode()

    def post_form(self, url, data, follow_redirects=False):
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200, page.data[:500])
        form = dict(data)
        form["csrf_token"] = self._csrf(page)
        return self.client.post(url, data=form, follow_redirects=follow_redirects)

    def post_from_page(self, page_url, post_url, data, follow_redirects=False):
        page = self.client.get(page_url)
        self.assertEqual(page.status_code, 200, page.data[:500])
        form = dict(data)
        form["csrf_token"] = self._csrf(page)
        return self.client.post(post_url, data=form, follow_redirects=follow_redirects)

    def active_ledger_path(self):
        conn = self.module._master_db()
        try:
            row = conn.execute("SELECT database_path FROM businesses ORDER BY id LIMIT 1").fetchone()
            return Path(row["database_path"]) if row["database_path"] else self.module._core.db.DB_PATH
        finally:
            conn.close()

    def create_owner(self):
        response = self.post_form("/setup", {
            "username": "ownerone",
            "password": "correct-horse-123",
            "confirm_password": "correct-horse-123",
            "reporting_currency": "USD",
        })
        self.assertEqual(response.status_code, 302)

    def test_simple_income_expense_and_product_workflow(self):
        self.create_owner()
        response = self.post_form("/products", {
            "action": "add", "name": "Consulting", "description": "Hourly service",
        })
        self.assertEqual(response.status_code, 302)

        response = self.post_form("/revenue/new", {
            "entry_date": "2026-10-13", "amount_original": "100.00",
            "profit_kept_amount": "96.00", "currency": "USD", "fx_to_usd": "1",
            "collected_by": "Owner", "product": "product:1", "payment_method": "Card",
            "description": "Consulting", "note": "Card processing fee recorded separately",
            "idempotency_key": "income-simple-1",
        })
        self.assertEqual(response.status_code, 302)

        response = self.post_form("/expense/new", {
            "entry_date": "2026-10-13", "amount_original": "20.00", "currency": "USD",
            "fx_to_usd": "1", "description": "Software subscription", "vendor": "Vendor",
            "paid_by": "Owner", "payment_method": "Card", "note": "Monthly expense",
            "template": "other", "shared": "on", "idempotency_key": "expense-simple-1",
        })
        self.assertEqual(response.status_code, 302)

        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"96.00", dashboard.data)
        self.assertIn(b"20.00", dashboard.data)

    def test_invalid_profit_kept_is_rejected(self):
        self.create_owner()
        response = self.post_form("/revenue/new", {
            "entry_date": "2026-10-13", "amount_original": "10.00",
            "profit_kept_amount": "12.00", "currency": "USD", "fx_to_usd": "1",
            "collected_by": "Owner", "product": "", "payment_method": "Cash",
            "description": "", "note": "test", "idempotency_key": "invalid-profit-1",
        })
        self.assertEqual(response.status_code, 400)

    def test_signup_is_disabled(self):
        self.create_owner()
        response = self.client.get("/signup")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sign-ups are disabled", response.data)

    def test_refund_fee_and_void_keep_an_audit_trail(self):
        self.create_owner()
        response = self.post_form("/revenue/new", {
            "entry_date": "2026-10-13", "amount_original": "120.00",
            "profit_kept_amount": "115.00", "currency": "USD", "fx_to_usd": "1",
            "collected_by": "Owner", "product": "general_income", "payment_method": "Stripe",
            "description": "Stripe project payment", "note": "original sale",
            "idempotency_key": "stripe-sale-1",
        })
        self.assertEqual(response.status_code, 302)
        ledger = sqlite3.connect(self.active_ledger_path())
        try:
            revenue_id = ledger.execute("SELECT id FROM revenue ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            ledger.close()
        response = self.post_from_page("/", f"/revenue/{revenue_id}/refund", {
            "event": "refund", "processor_fee": "3.50", "reason": "customer refund",
        })
        self.assertEqual(response.status_code, 302)

        # A refund is an immutable reversal plus a separate fee expense.
        ledger = sqlite3.connect(self.active_ledger_path())
        try:
            revenue = ledger.execute("SELECT status, reversal_of_id FROM revenue ORDER BY id").fetchall()
            expenses = ledger.execute("SELECT amount_original, description FROM expenses ORDER BY id").fetchall()
        finally:
            ledger.close()
        self.assertEqual(len(revenue), 2)
        self.assertTrue(any(row[1] == revenue_id for row in revenue))
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0][0], 3.5)

        # A second refund cannot create a second reversal or fee.
        response = self.post_from_page("/", f"/revenue/{revenue_id}/refund", {
            "event": "refund", "processor_fee": "3.50", "reason": "duplicate",
        })
        self.assertEqual(response.status_code, 302)
        ledger = sqlite3.connect(self.active_ledger_path())
        try:
            self.assertEqual(ledger.execute("SELECT COUNT(*) FROM revenue").fetchone()[0], 2)
            self.assertEqual(ledger.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 1)
        finally:
            ledger.close()

    def test_second_business_is_empty_and_cannot_be_selected_by_another_user(self):
        self.create_owner()
        # Owner records an item in the original business, then creates a second empty one.
        response = self.post_form("/revenue/new", {
            "entry_date": "2026-10-13", "amount_original": "25.00", "profit_kept_amount": "25.00",
            "currency": "USD", "fx_to_usd": "1", "collected_by": "Owner", "product": "general_income",
            "payment_method": "Cash", "description": "", "note": "first business", "idempotency_key": "owner-income-1",
        })
        self.assertEqual(response.status_code, 302)
        response = self.post_form("/businesses/new", {"name": "Second business", "reporting_currency": "USD"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"No revenue yet", self.client.get("/").data)

        # Give an independent user an independent workspace without enabling public signup.
        conn = self.module._auth_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_owner, created_at) VALUES (?, ?, 1, datetime('now'))",
                ("othertest", self.module.generate_password_hash("another-secure-123")),
            )
            conn.commit()
        finally:
            conn.close()
        other = self.flask_app.test_client()
        login = other.get("/login")
        token = self._csrf(login)
        response = other.post("/login", data={"username": "othertest", "password": "another-secure-123", "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        page = other.get("/businesses/new")
        response = other.post("/businesses/new", data={"name": "Other business", "reporting_currency": "USD", "csrf_token": self._csrf(page)})
        self.assertEqual(response.status_code, 302)

        master = self.module._master_db()
        try:
            protected_id = master.execute("SELECT id FROM businesses WHERE name = ?", ("Second business",)).fetchone()["id"]
        finally:
            master.close()
        response = other.post(f"/businesses/{protected_id}/select", data={"csrf_token": self._csrf(other.get('/'))})
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"No revenue yet", other.get("/").data)

    def test_void_and_exports_do_not_delete_history(self):
        self.create_owner()
        response = self.post_form("/expense/new", {
            "entry_date": "2026-10-13", "amount_original": "42.00", "currency": "USD",
            "fx_to_usd": "1", "description": "Test expense", "vendor": "Vendor",
            "paid_by": "Owner", "payment_method": "Cash", "note": "void workflow",
            "template": "other", "shared": "on", "idempotency_key": "expense-void-1",
        })
        self.assertEqual(response.status_code, 302)
        ledger = sqlite3.connect(self.active_ledger_path())
        try:
            expense_id = ledger.execute("SELECT id FROM expenses ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            ledger.close()

        response = self.post_from_page("/", f"/expense/{expense_id}/void", {"reason": "entered twice"})
        self.assertEqual(response.status_code, 302)
        ledger = sqlite3.connect(self.active_ledger_path())
        try:
            self.assertEqual(ledger.execute("SELECT status FROM expenses WHERE id = ?", (expense_id,)).fetchone()[0], "voided")
        finally:
            ledger.close()

        csv_export = self.client.get("/export/all")
        self.assertEqual(csv_export.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(csv_export.data)) as archive:
            self.assertIn("Test expense", archive.read("expenses.csv").decode("utf-8"))
        backup = self.client.get("/export/backup-db")
        self.assertEqual(backup.status_code, 200)
        self.assertGreater(len(backup.data), 0)
        backup.close()

    def test_recurring_expenses_are_currently_disabled_safely(self):
        self.create_owner()
        response = self.client.get("/recurring")
        self.assertEqual(response.status_code, 302)

    def test_partner_payment_and_adjustment_are_recorded(self):
        self.create_owner()
        response = self.post_form("/settings/partners", {"action": "add", "partner_name": "Partner two"})
        self.assertEqual(response.status_code, 302)

        response = self.post_form("/payment/new", {
            "entry_date": "2026-10-13", "from_person": "Owner", "to_person": "Partner two",
            "amount_original": "30.00", "currency": "USD", "fx_to_usd": "1",
            "note": "settlement", "idempotency_key": "payment-1",
        })
        self.assertEqual(response.status_code, 302)
        response = self.post_form("/adjustment/new", {
            "entry_date": "2026-10-13", "from_person": "Partner two", "to_person": "Owner",
            "amount_original": "10.00", "currency": "USD", "fx_to_usd": "1",
            "note": "adjustment", "idempotency_key": "adjustment-1",
        })
        self.assertEqual(response.status_code, 302)
        ledger = sqlite3.connect(self.active_ledger_path())
        try:
            self.assertEqual(ledger.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
            self.assertEqual(ledger.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0], 1)
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

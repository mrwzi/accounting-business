import os
import json
import sqlite3
import calendar
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta, date, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# The original accounting database remains the master/legacy ledger. Each
# additional business receives an isolated SQLite ledger selected per request.
# Tests and maintenance tools may point this at an isolated database. Existing
# installs keep their root-level ledger; fresh downloads create private runtime
# data under instance/, which is ignored by Git.
_PROJECT_DIR = Path(__file__).resolve().parent
_LEGACY_DB_PATH = _PROJECT_DIR / "accounting.db"
_DEFAULT_DB_PATH = _LEGACY_DB_PATH if _LEGACY_DB_PATH.exists() else _PROJECT_DIR / "instance" / "accounting.db"
DB_PATH = Path(os.environ.get("ACCOUNTING_MASTER_DB_PATH") or _DEFAULT_DB_PATH).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_active_db_path: ContextVar[Path | None] = ContextVar("active_accounting_db", default=None)


def set_active_database(path: str | Path | None):
    """Select a business ledger for the current request/context."""
    target = Path(path).resolve() if path else None
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
    return _active_db_path.set(target)


def reset_active_database(token):
    _active_db_path.reset(token)


def active_database_path() -> Path:
    return _active_db_path.get() or DB_PATH

TABLES = ("revenue", "expenses", "adjustments", "payments")
ALLOWED_TABLES = set(TABLES)

MONEY_SCALE = Decimal("1000000")  # micro-units
MONEY_UNIT = Decimal("0.000001")

VALID_STATUSES = {"posted", "voided", "reversed", "draft"}
DELETE_WINDOW_SECONDS = int(os.environ.get("DELETE_WINDOW_SECONDS", "86400"))
RECURRING_TABLE = "recurring_expenses"


def connect():
    conn = sqlite3.connect(active_database_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def master_connect():
    """Open the master registry/legacy ledger regardless of request context."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _audit_event_type_for_status_change(new_status: str) -> str:
    if new_status == "voided":
        return "void"
    return "status_change"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_utc(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_entry_date_utc(value: str | None):
    if not value:
        return None
    try:
        d = date.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def is_within_delete_window(created_at: str | None, entry_date: str | None = None) -> bool:
    created_dt = _parse_iso_utc(created_at)
    if not created_dt:
        return False
    now = datetime.now(timezone.utc)
    if (now - created_dt) > timedelta(seconds=DELETE_WINDOW_SECONDS):
        return False

    # Match UI expectations for "old rows": backdated transactions are not deletable.
    entry_dt = _parse_entry_date_utc(entry_date)
    if entry_dt and (now - entry_dt) > timedelta(seconds=DELETE_WINDOW_SECONDS):
        return False
    return True


def decimal_to_micros(value) -> int:
    d = Decimal(str(value))
    return int((d * MONEY_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def micros_to_decimal(value) -> Decimal:
    return (Decimal(int(value or 0)) / MONEY_SCALE).quantize(MONEY_UNIT, rounding=ROUND_HALF_UP)


def decimal_to_ppm(value) -> int:
    d = Decimal(str(value))
    return int((d * MONEY_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ppm_to_decimal(value) -> Decimal:
    return (Decimal(int(value or 0)) / MONEY_SCALE).quantize(MONEY_UNIT, rounding=ROUND_HALF_UP)


def _column_names(cur, table: str):
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _ensure_column(cur, table: str, col: str, col_type: str):
    cols = _column_names(cur, table)
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def _ensure_common_migration_columns(cur, table: str):
    _ensure_column(cur, table, "amount_original_micros", "INTEGER")
    _ensure_column(cur, table, "fx_to_usd_ppm", "INTEGER")
    _ensure_column(cur, table, "amount_usd_micros", "INTEGER")
    _ensure_column(cur, table, "status", "TEXT")
    _ensure_column(cur, table, "reversal_of_id", "INTEGER")
    _ensure_column(cur, table, "corrects_id", "INTEGER")
    _ensure_column(cur, table, "idempotency_key", "TEXT")
    _ensure_column(cur, table, "created_by", "TEXT")
    _ensure_column(cur, table, "created_at", "TEXT")
    _ensure_column(cur, table, "updated_at", "TEXT")


def _ensure_template_helper_columns(cur):
    _ensure_column(cur, "revenue", "template", "TEXT")
    _ensure_column(cur, "revenue", "category", "TEXT")
    _ensure_column(cur, "revenue", "product", "TEXT")
    _ensure_column(cur, "revenue", "payment_method", "TEXT")
    _ensure_column(cur, "revenue", "gross_sale_amount", "REAL")
    _ensure_column(cur, "revenue", "profit_kept_amount", "REAL")
    _ensure_column(cur, "expenses", "template", "TEXT")
    _ensure_column(cur, "expenses", "category", "TEXT")
    _ensure_column(cur, "expenses", "vendor", "TEXT")
    _ensure_column(cur, "expenses", "recurring_id", "INTEGER")
    _ensure_column(cur, "expenses", "payment_method", "TEXT")
    _ensure_column(cur, "recurring_expenses", "payment_method", "TEXT")


def init_db():
    conn = connect()
    cur = conn.cursor()

    cur.executescript(
        """
    CREATE TABLE IF NOT EXISTS revenue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount_original REAL NOT NULL,
        currency TEXT NOT NULL,
        fx_to_usd REAL NOT NULL,
        amount_usd REAL NOT NULL,
        amount_original_micros INTEGER NOT NULL,
        fx_to_usd_ppm INTEGER NOT NULL,
        amount_usd_micros INTEGER NOT NULL,
        collected_by TEXT NOT NULL,
        note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'posted',
        reversal_of_id INTEGER,
        idempotency_key TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount_original REAL NOT NULL,
        currency TEXT NOT NULL,
        fx_to_usd REAL NOT NULL,
        amount_usd REAL NOT NULL,
        amount_original_micros INTEGER NOT NULL,
        fx_to_usd_ppm INTEGER NOT NULL,
        amount_usd_micros INTEGER NOT NULL,
        paid_by TEXT NOT NULL,
        shared INTEGER NOT NULL DEFAULT 1,
        for_person TEXT,
        note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'posted',
        reversal_of_id INTEGER,
        idempotency_key TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        from_person TEXT NOT NULL,
        to_person TEXT NOT NULL,
        amount_original REAL NOT NULL,
        currency TEXT NOT NULL,
        fx_to_usd REAL NOT NULL,
        amount_usd REAL NOT NULL,
        amount_original_micros INTEGER NOT NULL,
        fx_to_usd_ppm INTEGER NOT NULL,
        amount_usd_micros INTEGER NOT NULL,
        note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'posted',
        reversal_of_id INTEGER,
        idempotency_key TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        from_person TEXT NOT NULL,
        to_person TEXT NOT NULL,
        amount_original REAL NOT NULL,
        currency TEXT NOT NULL,
        fx_to_usd REAL NOT NULL,
        amount_usd REAL NOT NULL,
        amount_original_micros INTEGER NOT NULL,
        fx_to_usd_ppm INTEGER NOT NULL,
        amount_usd_micros INTEGER NOT NULL,
        note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'posted',
        reversal_of_id INTEGER,
        idempotency_key TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS recurring_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        template TEXT,
        category TEXT,
        vendor TEXT,
        amount_original REAL NOT NULL,
        currency TEXT NOT NULL,
        fx_to_usd REAL,
        paid_by TEXT NOT NULL,
        shared INTEGER NOT NULL DEFAULT 1,
        for_person TEXT,
        day_of_month INTEGER NOT NULL DEFAULT 1,
        note TEXT,
        start_month TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        payment_method TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        starting_balance_usd REAL NOT NULL DEFAULT 0,
        starting_balance_date TEXT NOT NULL,
        note TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS account_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_account_id INTEGER NOT NULL,
        to_account_id INTEGER,
        amount_usd REAL NOT NULL CHECK(amount_usd > 0),
        entry_date TEXT NOT NULL,
        note TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(from_account_id) REFERENCES accounts(id),
        FOREIGN KEY(to_account_id) REFERENCES accounts(id)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_name TEXT NOT NULL,
        record_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT,
        event_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );

    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is immutable');
    END;

    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is immutable');
    END;
    """
    )

    # Products are business-local because each selected database is a business ledger.
    cur.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(name COLLATE NOCASE)
    )""")
    # Migration columns for older DBs.
    for table in TABLES:
        _ensure_common_migration_columns(cur, table)
    _ensure_template_helper_columns(cur)
    _ensure_column(cur, "revenue", "product_name", "TEXT")

    # Ensure legacy multicurrency columns on older databases before backfill.
    for table in TABLES:
        _ensure_column(cur, table, "amount_original", "REAL")
        _ensure_column(cur, table, "currency", "TEXT")
        _ensure_column(cur, table, "fx_to_usd", "REAL")

    now = _now_utc_iso()

    for table in TABLES:
        cur.execute(
            f"""
            UPDATE {table}
            SET amount_original = COALESCE(amount_original, amount_usd),
                currency = COALESCE(currency, 'USD'),
                fx_to_usd = COALESCE(fx_to_usd, 1.0),
                amount_original_micros = COALESCE(amount_original_micros, CAST(ROUND(COALESCE(amount_original, amount_usd, 0) * 1000000.0) AS INTEGER)),
                fx_to_usd_ppm = COALESCE(fx_to_usd_ppm, CAST(ROUND(COALESCE(fx_to_usd, 1.0) * 1000000.0) AS INTEGER)),
                amount_usd_micros = COALESCE(amount_usd_micros, CAST(ROUND(COALESCE(amount_usd, 0) * 1000000.0) AS INTEGER)),
                note = COALESCE(note, ''),
                status = COALESCE(NULLIF(status, ''), 'posted'),
                created_at = COALESCE(created_at, ?),
                updated_at = COALESCE(updated_at, created_at, ?)
            """,
            (now, now),
        )

    # Backfill gross_sale_amount for pre-existing revenue rows: historically the
    # recorded amount doubled as both the sale price and the profit kept, so the
    # best-available gross figure equals amount_original.
    cur.execute(
        "UPDATE revenue SET gross_sale_amount = amount_original WHERE gross_sale_amount IS NULL"
    )
    cur.execute(
        "UPDATE revenue SET profit_kept_amount = amount_original WHERE profit_kept_amount IS NULL"
    )

    # Backfill payment_method for legacy revenue rows from their description
    # prefix, so payment-method filtering/totals work across old and new data.
    cur.execute(
        """UPDATE revenue SET payment_method = 'Cash'
           WHERE payment_method IS NULL AND description LIKE 'Cash Profit%'"""
    )
    cur.execute(
        """UPDATE revenue SET payment_method = 'e-Transfer'
           WHERE payment_method IS NULL AND description LIKE 'e-Transfer Profit%'"""
    )
    cur.execute(
        """UPDATE revenue SET payment_method = 'Other'
           WHERE payment_method IS NULL"""
    )

    # Legacy expense rows predate payment_method entirely -- there's no
    # description pattern to infer it from, so tag them 'Other' rather than
    # leaving them out of every account's balance calculation silently.
    cur.execute(
        """UPDATE expenses SET payment_method = 'Other'
           WHERE payment_method IS NULL"""
    )

    # One-time correction: auto-generated reversal rows (created when a posted
    # entry is edited) were previously inserted with status='posted', which
    # double-counted them toward totals on top of the original already being
    # excluded via status='reversed'. Every past edit silently understated
    # revenue/expenses by the original amount. Reversal rows should never
    # count toward totals themselves (the audit trail already records what
    # changed) -- fix any existing ones by marking them 'voided' instead.
    cur.execute(
        """UPDATE revenue SET status = 'voided'
           WHERE reversal_of_id IS NOT NULL AND status = 'posted'"""
    )
    cur.execute(
        """UPDATE expenses SET status = 'voided'
           WHERE reversal_of_id IS NOT NULL AND status = 'posted'"""
    )
    cur.execute(
        """UPDATE adjustments SET status = 'voided'
           WHERE reversal_of_id IS NOT NULL AND status = 'posted'"""
    )
    cur.execute(
        """UPDATE payments SET status = 'voided'
           WHERE reversal_of_id IS NOT NULL AND status = 'posted'"""
    )

    # Idempotency uniqueness per table.
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_revenue_idempotency ON revenue(idempotency_key) WHERE idempotency_key IS NOT NULL")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_expenses_idempotency ON expenses(idempotency_key) WHERE idempotency_key IS NOT NULL")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_adjustments_idempotency ON adjustments(idempotency_key) WHERE idempotency_key IS NOT NULL")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_idempotency ON payments(idempotency_key) WHERE idempotency_key IS NOT NULL")

    for table in TABLES:
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_status ON {table}(status)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_entry_date ON {table}(entry_date)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_reversal_of_id ON {table}(reversal_of_id)")

    cur.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_table_record ON audit_log(table_name, record_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_event_at ON audit_log(event_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_expenses_recurring_id ON expenses(recurring_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_recurring_active ON recurring_expenses(active)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_account_movements_from_date ON account_movements(from_account_id, entry_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_account_movements_to_date ON account_movements(to_account_id, entry_date)")

    # Split the known legacy catch-all wallet once.  It used to combine Card,
    # Crypto and e-Transfer, which makes distinct balances impossible.  This
    # exact name/mapping identifies the old default; custom accounts are never
    # modified automatically.
    legacy_wallet = cur.execute(
        "SELECT * FROM accounts WHERE name = ? AND payment_method = ?",
        ("Crypto Wallet", "Card,Crypto,Other,e-Transfer"),
    ).fetchone()
    if legacy_wallet:
        legacy_before = dict(legacy_wallet)
        cur.execute(
            "UPDATE accounts SET name = ?, payment_method = ?, updated_at = ? WHERE id = ?",
            ("Card / Other wallet", "Card,Other", now, legacy_before["id"]),
        )
        _insert_audit_log_tx(conn, ACCOUNTS_TABLE, legacy_before["id"], "edit", "system-migration", {
            "reason": "split_legacy_payment_methods",
            "changes": {
                "name": {"from": legacy_before["name"], "to": "Card / Other wallet"},
                "payment_method": {"from": legacy_before["payment_method"], "to": "Card,Other"},
            },
        })

    # Processor funds and every supported payment rail must have their own
    # mapping. Add only missing mappings; existing user-configured accounts
    # are otherwise never changed by this migration.
    existing_methods = set()
    for account in cur.execute("SELECT payment_method FROM accounts WHERE active = 1").fetchall():
        existing_methods.update(
            method.strip() for method in str(account[0] or "").split(",") if method.strip()
        )
    for name, method in (
        ("e-Transfer", "e-Transfer"),
        ("Crypto", "Crypto"),
        ("Stripe balance / clearing", "Stripe"),
        ("PayPal balance", "PayPal"),
    ):
        if method not in existing_methods:
            cur.execute(
                """INSERT INTO accounts
                   (name, payment_method, starting_balance_usd, starting_balance_date, note, active,
                    created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,1,?,?,?)""",
                (name, method, 0.0, str(date.today()),
                 "System mapping for processor receipts and fees.", "system-migration", now, now),
            )
            aid = cur.lastrowid
            _insert_audit_log_tx(conn, ACCOUNTS_TABLE, aid, "create", "system-migration", {
                "reason": "processor_account_mapping",
                "snapshot": _row_snapshot_tx(conn, ACCOUNTS_TABLE, aid),
            })

    conn.commit()
    conn.close()


def _normalize_row_dict(row_dict: dict) -> dict:
    d = dict(row_dict)

    if "amount_original_micros" in d and d.get("amount_original_micros") is not None:
        d["amount_original"] = float(micros_to_decimal(d["amount_original_micros"]))
    if "amount_usd_micros" in d and d.get("amount_usd_micros") is not None:
        d["amount_usd"] = float(micros_to_decimal(d["amount_usd_micros"]))
    if "fx_to_usd_ppm" in d and d.get("fx_to_usd_ppm") is not None:
        d["fx_to_usd"] = float(ppm_to_decimal(d["fx_to_usd_ppm"]))

    d["status"] = d.get("status") or "posted"
    return d


def fetch_all(table: str):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")
    conn = connect()
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY entry_date DESC, id DESC").fetchall()
    conn.close()
    return [_normalize_row_dict(dict(r)) for r in rows]


def fetch_one(table: str, row_id: int):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")
    conn = connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    return _normalize_row_dict(dict(row)) if row else None


def find_by_idempotency(table: str, key: str):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")
    if not key:
        return None
    conn = connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE idempotency_key = ?", (key,)).fetchone()
    conn.close()
    return _normalize_row_dict(dict(row)) if row else None


def insert_audit_log(table_name: str, record_id: int, event_type: str, actor: str | None, payload: dict | None = None):
    conn = connect()
    conn.execute(
        """INSERT INTO audit_log (table_name, record_id, event_type, actor, event_at, payload_json)
           VALUES (?,?,?,?,?,?)""",
        (
            table_name,
            int(record_id),
            event_type,
            actor,
            _now_utc_iso(),
            json.dumps(payload or {}, ensure_ascii=True, sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()


def _insert_audit_log_tx(conn: sqlite3.Connection, table_name: str, record_id: int, event_type: str, actor: str | None, payload: dict | None = None):
    conn.execute(
        """INSERT INTO audit_log (table_name, record_id, event_type, actor, event_at, payload_json)
           VALUES (?,?,?,?,?,?)""",
        (
            table_name,
            int(record_id),
            event_type,
            actor,
            _now_utc_iso(),
            json.dumps(payload or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def _insert_common_amounts(amount_original, fx_to_usd, amount_usd):
    amount_original_dec = Decimal(str(amount_original))
    fx_dec = Decimal(str(fx_to_usd))
    amount_usd_dec = Decimal(str(amount_usd))
    return {
        "amount_original": float(amount_original_dec),
        "fx_to_usd": float(fx_dec),
        "amount_usd": float(amount_usd_dec),
        "amount_original_micros": decimal_to_micros(amount_original_dec),
        "fx_to_usd_ppm": decimal_to_ppm(fx_dec),
        "amount_usd_micros": decimal_to_micros(amount_usd_dec),
    }


def _find_row_id_by_idempotency(conn: sqlite3.Connection, table: str, key: str | None):
    if not key:
        return None
    row = conn.execute(f"SELECT id FROM {table} WHERE idempotency_key = ?", (key,)).fetchone()
    return int(row[0]) if row else None


def _row_snapshot_tx(conn: sqlite3.Connection, table: str, row_id: int):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else {}


def insert_revenue(entry_date, description, amount_original, currency, fx_to_usd, amount_usd, collected_by, note,
                   created_by=None, idempotency_key=None, template=None, category=None, payment_method=None,
                   gross_sale_amount=None, profit_kept_amount=None, product=None, product_name=None, status="posted", corrects_id=None):
    now = _now_utc_iso()
    m = _insert_common_amounts(amount_original, fx_to_usd, amount_usd)
    gross = float(Decimal(str(gross_sale_amount))) if gross_sale_amount is not None else m["amount_original"]
    profit_kept = float(Decimal(str(profit_kept_amount))) if profit_kept_amount is not None else m["amount_original"]

    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO revenue
               (entry_date, description, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                collected_by, note, status, reversal_of_id, corrects_id, idempotency_key, created_by, created_at, updated_at,
                 template, category, payment_method, gross_sale_amount, profit_kept_amount, product)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_date,
                description,
                m["amount_original"],
                currency,
                m["fx_to_usd"],
                m["amount_usd"],
                m["amount_original_micros"],
                m["fx_to_usd_ppm"],
                m["amount_usd_micros"],
                collected_by,
                note,
                status,
                None,
                corrects_id,
                idempotency_key,
                created_by,
                now,
                now,
                template,
                category,
                payment_method,
                gross,
                profit_kept,
                product,
            ),
        )
        row_id = cur.lastrowid
        _insert_audit_log_tx(conn, "revenue", row_id, "create", created_by, {
            "idempotency_key": idempotency_key,
            "snapshot": _row_snapshot_tx(conn, "revenue", row_id),
        })
        conn.commit()
        return row_id
    except sqlite3.IntegrityError:
        conn.rollback()
        existing_id = _find_row_id_by_idempotency(conn, "revenue", idempotency_key)
        if existing_id:
            return existing_id
        raise
    finally:
        conn.close()


def insert_expense(entry_date, description, amount_original, currency, fx_to_usd, amount_usd,
                   paid_by, shared, for_person, note, created_by=None, idempotency_key=None,
                   template=None, category=None, vendor=None, recurring_id=None, status="posted",
                   corrects_id=None, payment_method=None):
    now = _now_utc_iso()
    m = _insert_common_amounts(amount_original, fx_to_usd, amount_usd)

    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO expenses
               (entry_date, description, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                paid_by, shared, for_person, note, status, reversal_of_id, corrects_id, idempotency_key, created_by, created_at, updated_at,
                template, category, vendor, recurring_id, payment_method)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_date,
                description,
                m["amount_original"],
                currency,
                m["fx_to_usd"],
                m["amount_usd"],
                m["amount_original_micros"],
                m["fx_to_usd_ppm"],
                m["amount_usd_micros"],
                paid_by,
                shared,
                for_person,
                note,
                status,
                None,
                corrects_id,
                idempotency_key,
                created_by,
                now,
                now,
                template,
                category,
                vendor,
                recurring_id,
                payment_method,
            ),
        )
        row_id = cur.lastrowid
        _insert_audit_log_tx(conn, "expenses", row_id, "create", created_by, {
            "idempotency_key": idempotency_key,
            "snapshot": _row_snapshot_tx(conn, "expenses", row_id),
        })
        conn.commit()
        return row_id
    except sqlite3.IntegrityError:
        conn.rollback()
        existing_id = _find_row_id_by_idempotency(conn, "expenses", idempotency_key)
        if existing_id:
            return existing_id
        raise
    finally:
        conn.close()


def insert_adjustment(entry_date, from_person, to_person, amount_original, currency, fx_to_usd, amount_usd, note,
                      created_by=None, idempotency_key=None, status="posted", corrects_id=None):
    now = _now_utc_iso()
    m = _insert_common_amounts(amount_original, fx_to_usd, amount_usd)

    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO adjustments
               (entry_date, from_person, to_person, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                note, status, reversal_of_id, corrects_id, idempotency_key, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_date,
                from_person,
                to_person,
                m["amount_original"],
                currency,
                m["fx_to_usd"],
                m["amount_usd"],
                m["amount_original_micros"],
                m["fx_to_usd_ppm"],
                m["amount_usd_micros"],
                note,
                status,
                None,
                corrects_id,
                idempotency_key,
                created_by,
                now,
                now,
            ),
        )
        row_id = cur.lastrowid
        _insert_audit_log_tx(conn, "adjustments", row_id, "create", created_by, {
            "idempotency_key": idempotency_key,
            "snapshot": _row_snapshot_tx(conn, "adjustments", row_id),
        })
        conn.commit()
        return row_id
    except sqlite3.IntegrityError:
        conn.rollback()
        existing_id = _find_row_id_by_idempotency(conn, "adjustments", idempotency_key)
        if existing_id:
            return existing_id
        raise
    finally:
        conn.close()


def insert_payment(entry_date, from_person, to_person, amount_original, currency, fx_to_usd, amount_usd, note,
                   created_by=None, idempotency_key=None, status="posted", corrects_id=None):
    now = _now_utc_iso()
    m = _insert_common_amounts(amount_original, fx_to_usd, amount_usd)

    conn = connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO payments
               (entry_date, from_person, to_person, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                note, status, reversal_of_id, corrects_id, idempotency_key, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_date,
                from_person,
                to_person,
                m["amount_original"],
                currency,
                m["fx_to_usd"],
                m["amount_usd"],
                m["amount_original_micros"],
                m["fx_to_usd_ppm"],
                m["amount_usd_micros"],
                note,
                status,
                None,
                corrects_id,
                idempotency_key,
                created_by,
                now,
                now,
            ),
        )
        row_id = cur.lastrowid
        _insert_audit_log_tx(conn, "payments", row_id, "create", created_by, {
            "idempotency_key": idempotency_key,
            "snapshot": _row_snapshot_tx(conn, "payments", row_id),
        })
        conn.commit()
        return row_id
    except sqlite3.IntegrityError:
        conn.rollback()
        existing_id = _find_row_id_by_idempotency(conn, "payments", idempotency_key)
        if existing_id:
            return existing_id
        raise
    finally:
        conn.close()


def set_status(table: str, row_id: int, new_status: str, actor: str | None, reason: str | None = None):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")
    if new_status not in VALID_STATUSES:
        raise ValueError("Invalid status")

    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        conn.close()
        return False

    old_status = row[0] or "posted"
    if old_status == new_status:
        conn.close()
        return True

    cur.execute(
        f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, _now_utc_iso(), row_id),
    )
    _insert_audit_log_tx(conn, table, row_id, _audit_event_type_for_status_change(new_status), actor, {
        "from": old_status,
        "to": new_status,
        "reason": reason or "",
        "snapshot_after": _row_snapshot_tx(conn, table, row_id),
    })
    conn.commit()
    conn.close()
    return True


def _reverse_insert_sql(table: str):
    if table == "revenue":
        return (
            """INSERT INTO revenue
               (entry_date, description, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                collected_by, note, status, reversal_of_id, idempotency_key, created_by, created_at, updated_at,
                template, category, payment_method, gross_sale_amount, profit_kept_amount, product)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ["entry_date", "description", "amount_original", "currency", "fx_to_usd", "amount_usd",
             "amount_original_micros", "fx_to_usd_ppm", "amount_usd_micros",
             "collected_by", "note", "status", "reversal_of_id", "idempotency_key", "created_by", "created_at", "updated_at",
             "template", "category", "payment_method", "gross_sale_amount", "profit_kept_amount", "product", "product_name"],
        )
    if table == "expenses":
        return (
            """INSERT INTO expenses
               (entry_date, description, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                paid_by, shared, for_person, note, status, reversal_of_id, idempotency_key, created_by, created_at, updated_at,
                template, category, vendor, recurring_id, payment_method)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ["entry_date", "description", "amount_original", "currency", "fx_to_usd", "amount_usd",
             "amount_original_micros", "fx_to_usd_ppm", "amount_usd_micros",
             "paid_by", "shared", "for_person", "note", "status", "reversal_of_id", "idempotency_key", "created_by", "created_at", "updated_at",
             "template", "category", "vendor", "recurring_id", "payment_method"],
        )
    if table == "adjustments":
        return (
            """INSERT INTO adjustments
               (entry_date, from_person, to_person, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                note, status, reversal_of_id, idempotency_key, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ["entry_date", "from_person", "to_person", "amount_original", "currency", "fx_to_usd", "amount_usd",
             "amount_original_micros", "fx_to_usd_ppm", "amount_usd_micros",
             "note", "status", "reversal_of_id", "idempotency_key", "created_by", "created_at", "updated_at"],
        )
    if table == "payments":
        return (
            """INSERT INTO payments
               (entry_date, from_person, to_person, amount_original, currency, fx_to_usd, amount_usd,
                amount_original_micros, fx_to_usd_ppm, amount_usd_micros,
                note, status, reversal_of_id, idempotency_key, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ["entry_date", "from_person", "to_person", "amount_original", "currency", "fx_to_usd", "amount_usd",
             "amount_original_micros", "fx_to_usd_ppm", "amount_usd_micros",
             "note", "status", "reversal_of_id", "idempotency_key", "created_by", "created_at", "updated_at"],
        )
    raise ValueError("Invalid table")


def reverse_transaction(table: str, row_id: int, actor: str | None, reason: str | None = None):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")

    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        conn.close()
        return None

    row = dict(row)
    status = row.get("status") or "posted"
    if status != "posted":
        conn.close()
        return None

    now = _now_utc_iso()
    reason_text = (reason or "").strip()
    note_prefix = f"REVERSAL of #{row_id}" + (f" ({reason_text})" if reason_text else "")
    existing_note = (row.get("note") or "").strip()
    reversal_note = note_prefix if not existing_note else f"{note_prefix} | original note: {existing_note}"

    amount_original_micros = -int(row.get("amount_original_micros") or 0)
    amount_usd_micros = -int(row.get("amount_usd_micros") or 0)
    fx_to_usd_ppm = int(row.get("fx_to_usd_ppm") or decimal_to_ppm(row.get("fx_to_usd") or 1))
    gross_sale_amount = row.get("gross_sale_amount")
    reversed_gross_sale_amount = -float(gross_sale_amount) if gross_sale_amount is not None else None
    profit_kept_amount = row.get("profit_kept_amount")
    reversed_profit_kept_amount = -float(profit_kept_amount) if profit_kept_amount is not None else None

    payload = {
        **row,
        "amount_original_micros": amount_original_micros,
        "amount_usd_micros": amount_usd_micros,
        "fx_to_usd_ppm": fx_to_usd_ppm,
        "amount_original": float(micros_to_decimal(amount_original_micros)),
        "amount_usd": float(micros_to_decimal(amount_usd_micros)),
        "fx_to_usd": float(ppm_to_decimal(fx_to_usd_ppm)),
        "gross_sale_amount": reversed_gross_sale_amount,
        "profit_kept_amount": reversed_profit_kept_amount,
        "note": reversal_note,
        "status": "voided",
        "reversal_of_id": row_id,
        "idempotency_key": None,
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }

    sql, cols = _reverse_insert_sql(table)
    values = [payload.get(c) for c in cols]

    cur.execute(sql, values)
    reversal_id = cur.lastrowid

    cur.execute(
        f"UPDATE {table} SET status = 'reversed', updated_at = ? WHERE id = ?",
        (now, row_id),
    )

    _insert_audit_log_tx(conn, table, row_id, "reversed_original", actor, {
        "reversal_id": reversal_id,
        "reason": reason_text,
        "snapshot_after": _row_snapshot_tx(conn, table, row_id),
    })
    _insert_audit_log_tx(conn, table, reversal_id, "create_reversal", actor, {
        "original_id": row_id,
        "reason": reason_text,
        "snapshot": _row_snapshot_tx(conn, table, reversal_id),
    })
    conn.commit()
    conn.close()
    return reversal_id


def delete_row(table: str, row_id: int):
    raise RuntimeError("Hard delete is disabled. Use status workflow (void/reverse) instead.")


def hard_delete_recent(table: str, row_id: int, actor: str | None):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")

    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        conn.close()
        return False, "not_found"

    row_d = dict(row)
    status = row_d.get("status") or "posted"
    if status != "posted":
        conn.close()
        return False, "not_posted"

    if not is_within_delete_window(row_d.get("created_at"), row_d.get("entry_date")):
        conn.close()
        return False, "window_expired"

    _insert_audit_log_tx(conn, table, row_id, "hard_delete", actor, {
        "reason": "delete_within_24h_window",
        "snapshot": row_d,
    })
    cur.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return True, "deleted"


# ---------------------------------------------------------------------------
# Edit / correction workflow: never hard-delete a posted entry. Editing marks
# the original as 'reversed' (writing an offsetting negative entry, reusing
# reverse_transaction) and inserts a brand-new posted row with the corrected
# values, linked back via corrects_id. Both steps plus the diff are captured
# in a single audit_log event so there's always a full trail of who changed
# what and when.
# ---------------------------------------------------------------------------

_TABLE_INSERT_FN_NAMES = {
    "revenue": "insert_revenue",
    "expenses": "insert_expense",
    "adjustments": "insert_adjustment",
    "payments": "insert_payment",
}

_TABLE_EDITABLE_FIELDS = {
    "revenue": ["entry_date", "description", "amount_original", "currency", "fx_to_usd", "amount_usd",
                "collected_by", "note", "template", "category", "payment_method", "gross_sale_amount", "product"],
    "expenses": ["entry_date", "description", "amount_original", "currency", "fx_to_usd", "amount_usd",
                 "paid_by", "shared", "for_person", "note", "template", "category", "vendor", "recurring_id",
                 "payment_method"],
    "adjustments": ["entry_date", "from_person", "to_person", "amount_original", "currency", "fx_to_usd",
                     "amount_usd", "note"],
    "payments": ["entry_date", "from_person", "to_person", "amount_original", "currency", "fx_to_usd",
                 "amount_usd", "note"],
}


def correct_entry(table: str, row_id: int, actor: str | None, new_fields: dict, reason: str | None = None):
    """Replace a posted row with a corrected version: reverse the original,
    insert a new posted row with the corrected fields, link them, and record
    one audit event with the field-level diff. Returns (reversal_id, corrected_id)
    or None if the row can't be corrected (not found / not posted)."""
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")

    conn = connect()
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        conn.close()
        return None
    original = dict(row)
    if (original.get("status") or "posted") != "posted":
        conn.close()
        return None
    conn.close()

    reversal_id = reverse_transaction(table, row_id, actor, reason or "edit correction")
    if reversal_id is None:
        return None

    editable = _TABLE_EDITABLE_FIELDS[table]
    merged = {f: original.get(f) for f in editable}
    changes = {}
    for f, new_val in new_fields.items():
        if f not in editable:
            continue
        old_val = original.get(f)
        if str(old_val) != str(new_val):
            changes[f] = {"from": old_val, "to": new_val}
        merged[f] = new_val

    globals_map = globals()
    insert_fn = globals_map[_TABLE_INSERT_FN_NAMES[table]]

    if table == "revenue":
        corrected_id = insert_fn(
            merged["entry_date"], merged["description"], merged["amount_original"], merged["currency"],
            merged["fx_to_usd"], merged["amount_usd"], merged["collected_by"], merged["note"],
            created_by=actor, template=merged.get("template"), category=merged.get("category"),
            payment_method=merged.get("payment_method"), gross_sale_amount=merged.get("gross_sale_amount"),
            product=merged.get("product"),
            corrects_id=row_id,
        )
    elif table == "expenses":
        corrected_id = insert_fn(
            merged["entry_date"], merged["description"], merged["amount_original"], merged["currency"],
            merged["fx_to_usd"], merged["amount_usd"], merged["paid_by"], merged["shared"],
            merged["for_person"], merged["note"], created_by=actor, template=merged.get("template"),
            category=merged.get("category"), vendor=merged.get("vendor"),
            recurring_id=merged.get("recurring_id"), payment_method=merged.get("payment_method"),
            corrects_id=row_id,
        )
    elif table == "adjustments":
        corrected_id = insert_fn(
            merged["entry_date"], merged["from_person"], merged["to_person"], merged["amount_original"],
            merged["currency"], merged["fx_to_usd"], merged["amount_usd"], merged["note"],
            created_by=actor, corrects_id=row_id,
        )
    else:  # payments
        corrected_id = insert_fn(
            merged["entry_date"], merged["from_person"], merged["to_person"], merged["amount_original"],
            merged["currency"], merged["fx_to_usd"], merged["amount_usd"], merged["note"],
            created_by=actor, corrects_id=row_id,
        )

    conn = connect()
    _insert_audit_log_tx(conn, table, row_id, "edit", actor, {
        "original_id": row_id,
        "reversal_id": reversal_id,
        "corrected_id": corrected_id,
        "reason": (reason or "").strip(),
        "changes": changes,
    })
    conn.commit()
    conn.close()
    return reversal_id, corrected_id


def void_entry(table: str, row_id: int, actor: str | None, reason: str | None = None):
    """Cancel a posted entry entirely (no replacement). Keeps full history;
    excluded from totals going forward."""
    return set_status(table, row_id, "voided", actor, reason)


def fetch_audit_log(table_name: str | None = None, record_id: int | None = None, limit: int = 500):
    conn = connect()
    query = "SELECT * FROM audit_log"
    clauses = []
    params = []
    if table_name:
        clauses.append("table_name = ?")
        params.append(table_name)
    if record_id is not None:
        clauses.append("record_id = ?")
        params.append(record_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY event_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(query, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Recurring expenses: a template that, each month, should have exactly one
# expense row against it. On dashboard load we detect months between
# start_month and the current month with no linked row and create a DRAFT
# expense (excluded from totals) so the user can confirm the amount/FX before
# it posts, rather than silently guessing and posting.
# ---------------------------------------------------------------------------

def insert_recurring_expense(label, template, category, vendor, amount_original, currency, fx_to_usd,
                              paid_by, shared, for_person, day_of_month, note, start_month,
                              created_by=None, payment_method=None):
    now = _now_utc_iso()
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO recurring_expenses
           (label, template, category, vendor, amount_original, currency, fx_to_usd, paid_by, shared,
            for_person, day_of_month, note, start_month, active, payment_method, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
        (label, template, category, vendor, float(amount_original), currency,
         float(fx_to_usd) if fx_to_usd is not None else None, paid_by, int(shared), for_person,
         int(day_of_month), note, start_month, payment_method, created_by, now, now),
    )
    rid = cur.lastrowid
    _insert_audit_log_tx(conn, RECURRING_TABLE, rid, "create", created_by, {
        "snapshot": _row_snapshot_tx(conn, RECURRING_TABLE, rid),
    })
    conn.commit()
    conn.close()
    return rid


def fetch_recurring_expenses(active_only: bool = False):
    conn = connect()
    q = f"SELECT * FROM {RECURRING_TABLE}"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY active DESC, label ASC"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_recurring_active(recurring_id: int, active: bool, actor: str | None):
    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT active FROM {RECURRING_TABLE} WHERE id = ?", (recurring_id,)).fetchone()
    if not row:
        conn.close()
        return False
    cur.execute(
        f"UPDATE {RECURRING_TABLE} SET active = ?, updated_at = ? WHERE id = ?",
        (1 if active else 0, _now_utc_iso(), recurring_id),
    )
    _insert_audit_log_tx(conn, RECURRING_TABLE, recurring_id, "status_change", actor, {
        "active": bool(active),
    })
    conn.commit()
    conn.close()
    return True


ACCOUNTS_TABLE = "accounts"


def insert_account(name: str, payment_method: str, starting_balance_usd, starting_balance_date: str,
                    note: str | None = None, created_by: str | None = None):
    now = _now_utc_iso()
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO accounts
           (name, payment_method, starting_balance_usd, starting_balance_date, note, active,
            created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,1,?,?,?)""",
        (name, payment_method, float(starting_balance_usd), starting_balance_date, note,
         created_by, now, now),
    )
    aid = cur.lastrowid
    _insert_audit_log_tx(conn, ACCOUNTS_TABLE, aid, "create", created_by, {
        "snapshot": _row_snapshot_tx(conn, ACCOUNTS_TABLE, aid),
    })
    conn.commit()
    conn.close()
    return aid


def fetch_accounts(active_only: bool = False):
    conn = connect()
    sql = f"SELECT * FROM {ACCOUNTS_TABLE}"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_account_active(account_id: int, active: bool, actor: str | None):
    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT active FROM {ACCOUNTS_TABLE} WHERE id = ?", (account_id,)).fetchone()
    if not row:
        conn.close()
        return False
    cur.execute(
        f"UPDATE {ACCOUNTS_TABLE} SET active = ?, updated_at = ? WHERE id = ?",
        (1 if active else 0, _now_utc_iso(), account_id),
    )
    _insert_audit_log_tx(conn, ACCOUNTS_TABLE, account_id, "status_change", actor, {
        "active": bool(active),
    })
    conn.commit()
    conn.close()
    return True


def update_account(account_id: int, name: str, payment_method: str, starting_balance_usd,
                    starting_balance_date: str, note: str | None, actor: str | None):
    """Update an existing account's details (name, which payment methods feed
    it, its starting balance, and the date that balance is as-of). Logs a
    field-level diff to the audit log -- accounts aren't part of the
    reversal/void transaction machinery since they're metadata, not ledger
    entries, but every change is still recorded."""
    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT * FROM {ACCOUNTS_TABLE} WHERE id = ?", (account_id,)).fetchone()
    if not row:
        conn.close()
        return False
    before = dict(row)

    after = {
        "name": name,
        "payment_method": payment_method,
        "starting_balance_usd": float(starting_balance_usd),
        "starting_balance_date": starting_balance_date,
        "note": note,
    }
    cur.execute(
        f"""UPDATE {ACCOUNTS_TABLE} SET name = ?, payment_method = ?, starting_balance_usd = ?,
                starting_balance_date = ?, note = ?, updated_at = ? WHERE id = ?""",
        (after["name"], after["payment_method"], after["starting_balance_usd"],
         after["starting_balance_date"], after["note"], _now_utc_iso(), account_id),
    )
    changes = {}
    for k, new_v in after.items():
        old_v = before.get(k)
        if str(old_v) != str(new_v):
            changes[k] = {"from": old_v, "to": new_v}
    _insert_audit_log_tx(conn, ACCOUNTS_TABLE, account_id, "edit", actor, {"changes": changes})
    conn.commit()
    conn.close()
    return True


def fetch_account(account_id: int) -> dict | None:
    conn = connect()
    row = conn.execute(f"SELECT * FROM {ACCOUNTS_TABLE} WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_account(account_id: int, actor: str | None):
    """Accounts are configuration sitting on top of the real ledger, not
    ledger entries themselves -- deleting one doesn't touch or hide any
    revenue/expense history, it just removes the tracking config. Safe to
    hard-delete, unlike revenue/expenses/adjustments/payments. Still logs a
    full snapshot to the audit trail first (same pattern as discard_draft)."""
    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT * FROM {ACCOUNTS_TABLE} WHERE id = ?", (account_id,)).fetchone()
    if not row:
        conn.close()
        return False
    _insert_audit_log_tx(conn, ACCOUNTS_TABLE, account_id, "delete", actor, {"snapshot": dict(row)})
    cur.execute(f"DELETE FROM {ACCOUNTS_TABLE} WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    return True


def insert_owner_withdrawal(account_id, amount_usd, entry_date, note, created_by):
    """Record money taken out of a tracked account without creating an expense."""
    amount = Decimal(str(amount_usd))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Withdrawal amount must be greater than zero.")
    conn = connect()
    cur = conn.cursor()
    account = cur.execute("SELECT id, active FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not account or not account["active"]:
        conn.close()
        raise ValueError("Choose an active account.")
    now = _now_utc_iso()
    cur.execute("INSERT INTO account_movements (from_account_id, to_account_id, amount_usd, entry_date, note, created_by, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?)", (account_id, float(amount), entry_date, note, created_by, now))
    movement_id = cur.lastrowid
    _insert_audit_log_tx(conn, "account_movements", movement_id, "owner_withdrawal", created_by, {"snapshot": _row_snapshot_tx(conn, "account_movements", movement_id)})
    conn.commit()
    conn.close()
    return movement_id


def fetch_account_withdrawals():
    conn = connect()
    rows = conn.execute("""SELECT m.*, a.name AS account_name
                         FROM account_movements m JOIN accounts a ON a.id = m.from_account_id
                         WHERE m.to_account_id IS NULL ORDER BY m.entry_date DESC, m.id DESC""").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _month_range(start_month: str, through_month: str):
    """Inclusive list of 'YYYY-MM' strings from start_month to through_month."""
    sy, sm = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(through_month[:4]), int(through_month[5:7])
    months = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def missing_recurring_months(recurring_row: dict, through_month: str | None = None):
    through_month = through_month or date.today().strftime("%Y-%m")
    start_month = recurring_row["start_month"]
    if start_month > through_month:
        return []
    all_months = _month_range(start_month, through_month)

    conn = connect()
    rows = conn.execute(
        "SELECT DISTINCT substr(entry_date, 1, 7) AS ym FROM expenses "
        "WHERE recurring_id = ? AND status IN ('posted', 'draft')",
        (recurring_row["id"],),
    ).fetchall()
    conn.close()
    have = {r["ym"] for r in rows}
    return [m for m in all_months if m not in have]


def create_draft_expense_for_recurring(recurring_row: dict, month_key: str, actor: str | None = None):
    day = int(recurring_row.get("day_of_month") or 1)
    year, month = int(month_key[:4]), int(month_key[5:7])
    last_day = calendar.monthrange(year, month)[1]
    entry_date = f"{month_key}-{min(max(day, 1), last_day):02d}"

    currency = recurring_row["currency"]
    amount_original = Decimal(str(recurring_row["amount_original"]))
    fx = recurring_row.get("fx_to_usd")
    fx_dec = Decimal(str(fx)) if fx else (Decimal("1") if currency == "USD" else Decimal("1"))
    amount_usd = (amount_original * fx_dec).quantize(MONEY_UNIT, rounding=ROUND_HALF_UP)

    description = recurring_row.get("label") or "Recurring expense"
    return insert_expense(
        entry_date, description, amount_original, currency, fx_dec, amount_usd,
        recurring_row["paid_by"], int(recurring_row.get("shared") or 1), recurring_row.get("for_person"),
        recurring_row.get("note") or f"Auto-drafted recurring entry for {month_key} — review amount/FX and confirm.",
        created_by=actor, template=recurring_row.get("template"), category=recurring_row.get("category"),
        vendor=recurring_row.get("vendor"), recurring_id=recurring_row["id"], status="draft",
        payment_method=recurring_row.get("payment_method"),
    )


def sync_recurring_drafts(actor: str | None = None):
    """Create draft expense rows for any missing months across all active
    recurring expenses. Returns list of {recurring, month, expense_id}."""
    created = []
    for rec in fetch_recurring_expenses(active_only=True):
        for month_key in missing_recurring_months(rec):
            expense_id = create_draft_expense_for_recurring(rec, month_key, actor)
            created.append({"recurring": rec, "month": month_key, "expense_id": expense_id})
    return created


def confirm_draft(table: str, row_id: int, actor: str | None, overrides: dict | None = None):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")
    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        conn.close()
        return False, "not_found"
    row_d = dict(row)
    if (row_d.get("status") or "posted") != "draft":
        conn.close()
        return False, "not_draft"

    overrides = overrides or {}
    amount_original = overrides.get("amount_original", row_d["amount_original"])
    currency = overrides.get("currency", row_d["currency"])
    fx_to_usd = overrides.get("fx_to_usd", row_d["fx_to_usd"])
    m = _insert_common_amounts(amount_original, fx_to_usd, Decimal(str(amount_original)) * Decimal(str(fx_to_usd)))

    cur.execute(
        f"""UPDATE {table} SET
                status = 'posted',
                amount_original = ?, currency = ?, fx_to_usd = ?, amount_usd = ?,
                amount_original_micros = ?, fx_to_usd_ppm = ?, amount_usd_micros = ?,
                updated_at = ?
            WHERE id = ?""",
        (m["amount_original"], currency, m["fx_to_usd"], m["amount_usd"],
         m["amount_original_micros"], m["fx_to_usd_ppm"], m["amount_usd_micros"],
         _now_utc_iso(), row_id),
    )
    _insert_audit_log_tx(conn, table, row_id, "confirm_draft", actor, {
        "snapshot_after": _row_snapshot_tx(conn, table, row_id),
    })
    conn.commit()
    conn.close()
    return True, "confirmed"


def discard_draft(table: str, row_id: int, actor: str | None):
    if table not in ALLOWED_TABLES:
        raise ValueError("Invalid table name")
    conn = connect()
    cur = conn.cursor()
    row = cur.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        conn.close()
        return False, "not_found"
    row_d = dict(row)
    if (row_d.get("status") or "posted") != "draft":
        conn.close()
        return False, "not_draft"

    _insert_audit_log_tx(conn, table, row_id, "discard_draft", actor, {"snapshot": row_d})
    cur.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return True, "discarded"

# Product management is intentionally small and local to the active business ledger.
def fetch_products(include_archived: bool = False):
    conn = connect()
    sql = "SELECT * FROM products" + ("" if include_archived else " WHERE active = 1") + " ORDER BY lower(name), id"
    rows = [dict(row) for row in conn.execute(sql).fetchall()]
    conn.close()
    return rows


def fetch_product(product_id: int):
    conn = connect()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (int(product_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_product(name: str, description: str = ""):
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Product name is required")
    now = _now_utc_iso()
    conn = connect()
    try:
        cur = conn.execute("INSERT INTO products (name, description, active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)", (name, (description or "").strip(), now, now))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_product(product_id: int, name: str, description: str = ""):
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Product name is required")
    conn = connect()
    cur = conn.execute("UPDATE products SET name = ?, description = ?, updated_at = ? WHERE id = ?", (name, (description or "").strip(), _now_utc_iso(), int(product_id)))
    conn.commit()
    conn.close()
    return cur.rowcount == 1


def set_product_active(product_id: int, active: bool):
    conn = connect()
    cur = conn.execute("UPDATE products SET active = ?, updated_at = ? WHERE id = ?", (1 if active else 0, _now_utc_iso(), int(product_id)))
    conn.commit()
    conn.close()
    return cur.rowcount == 1


def delete_product_if_unused(product_id: int):
    """Remove only an unused product tracker, never historical accounting data."""
    conn = connect()
    try:
        product = conn.execute("SELECT id, name FROM products WHERE id = ?", (int(product_id),)).fetchone()
        if not product:
            return False, "not_found"
        product_key = f"product:{product['id']}"
        in_use = conn.execute(
            "SELECT 1 FROM revenue WHERE product = ? OR product_name = ? LIMIT 1",
            (product_key, product["name"]),
        ).fetchone()
        if in_use:
            return False, "in_use"
        conn.execute("DELETE FROM products WHERE id = ?", (int(product_id),))
        conn.commit()
        return True, "deleted"
    finally:
        conn.close()

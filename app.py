"""Recovery launcher for the Accounting application.

The original source was accidentally flattened on 2026-09-09. The last valid
CPython 3.14 bytecode is preserved as accounting_core_recovered.pyc. This
launcher loads that application without touching accounting.db.
"""

import builtins
import hmac
import html
import inspect
import json
import os
import re
import time
import sqlite3
import tempfile
from datetime import date
from importlib.machinery import SourcelessFileLoader
from pathlib import Path
from uuid import uuid4

import requests
from flask import abort, after_this_request, flash, g, has_request_context, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_DIR = Path(__file__).resolve().parent
CORE_PATH = PROJECT_DIR / "accounting_core_recovered.pyc"
LANGUAGES_DIR = PROJECT_DIR / "languages"
LANGUAGE_FILE_LIMIT = 256 * 1024


def _language_packs():
    """Load data-only language packs; invalid third-party files are ignored."""
    packs = {}
    if not LANGUAGES_DIR.is_dir():
        return packs
    for path in LANGUAGES_DIR.glob("*.json"):
        try:
            if path.stat().st_size > LANGUAGE_FILE_LIMIT:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            code = str(payload.get("code", "")).lower()
            name = str(payload.get("name", "")).strip()
            direction = str(payload.get("direction", "ltr")).lower()
            translations = payload.get("translations", {})
            strings = payload.get("strings", {})
            tokens = payload.get("tokens", {})
            if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", code):
                continue
            if not name or direction not in {"ltr", "rtl"} or not isinstance(translations, dict) or not isinstance(strings, dict) or not isinstance(tokens, dict):
                continue
            if not all(isinstance(key, str) and isinstance(value, str) for key, value in translations.items()) or not all(isinstance(key, str) and isinstance(value, str) for key, value in strings.items()) or not all(isinstance(key, str) and isinstance(value, str) for key, value in tokens.items()):
                continue
            packs[code] = {"code": code, "name": name, "direction": direction, "translations": translations, "strings": strings, "tokens": tokens}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return dict(sorted(packs.items(), key=lambda item: item[1]["name"].casefold()))


def _current_language():
    packs = _language_packs()
    code = session.get("language", "en") if has_request_context() else "en"
    return packs.get(code) or packs.get("en") or {"code": "en", "name": "English", "direction": "ltr", "translations": {}}


def translate(key: str, fallback: str):
    """Template translation with a deliberate English fallback for missing keys."""
    return _current_language()["translations"].get(key, fallback)

if not CORE_PATH.exists():
    raise RuntimeError(f"Recovered Accounting core is missing: {CORE_PATH}")

_core = SourcelessFileLoader("accounting_recovered_core", str(CORE_PATH)).load_module()
_core.app.jinja_env.globals["t"] = translate

# Local development works over HTTP; set COOKIE_SECURE=1 behind HTTPS in production.
_core.app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
_core.PUBLIC_ENDPOINTS.update({"setup", "signup"})


def _safe_core_print(*args, **kwargs):
    """Keep optional recovered-core diagnostics from breaking a web request.

    Runner can close a managed process's output pipe while it is still serving
    a request.  The recovered security route prints a diagnostic immediately
    before returning JSON; a broken pipe there must not turn a successful scan
    into an HTTP 500.
    """
    try:
        builtins.print(*args, **kwargs)
    except (BrokenPipeError, OSError):
        pass


_core.__dict__["print"] = _safe_core_print

# Maintained integration layer.  The recovered core is deliberately left
# untouched: it continues to own the existing journal and UI routes while this
# source file owns integrations and new processor-specific behaviour.
PAYMENT_METHODS = ("Cash", "e-Transfer", "Crypto", "Card", "Stripe", "PayPal", "Other")
PROCESSOR_METHODS = frozenset({"Stripe", "PayPal"})
CATALOG_CACHE_SECONDS = max(30, int(os.environ.get("ACCDIVIDE_CATALOG_CACHE_SECONDS", "300")))
_catalog_cache = {"expires": 0.0, "products": None}
SYSTEM_EXPENSE_TEMPLATES = {
    "other": {"label": "Other Expense", "category": "Other Expense"},
    "stripe_processing_fee": {"label": "Stripe processing fee", "category": "Processor fee"},
    "paypal_processing_fee": {"label": "PayPal processing fee", "category": "Processor fee"},
    "stripe_refund_fee": {"label": "Stripe refund / chargeback fee", "category": "Processor fee"},
    "paypal_refund_fee": {"label": "PayPal refund / chargeback fee", "category": "Processor fee"},
}


def _configure_core_options():

    """Expose new methods/templates to every recovered-core form and filter."""
    _core.PAYMENT_METHODS = set(PAYMENT_METHODS)
    _core.SUPPORTED_FX_CURRENCIES = {"USD", "CAD", "TOMAN"}
    _core.REVENUE_TEMPLATES.update({
        "stripe_sale": {"label": "Stripe Sale", "category": "Stripe Sale"},
        "paypal_sale": {"label": "PayPal Sale", "category": "PayPal Sale"},
    })
    _core.EXPENSE_TEMPLATES.update(SYSTEM_EXPENSE_TEMPLATES)


def _catalog_products():
    """Read active AccDivide products only; failure never empties the form."""
    now = time.monotonic()
    if _catalog_cache["products"] is not None and now < _catalog_cache["expires"]:
        return _catalog_cache["products"]
    url = (os.environ.get("ACCDIVIDE_CATALOG_URL") or "").strip()
    token = (os.environ.get("ACCDIVIDE_CATALOG_TOKEN") or "").strip()
    if url and token:
        try:
            response = requests.get(url, headers={"X-Accounting-Token": token}, timeout=5)
            response.raise_for_status()
            payload = response.json()
            products = _products_from_payload(payload)
            if products:
                _catalog_cache.update(expires=now + CATALOG_CACHE_SECONDS, products=products)
                return products
        except (requests.RequestException, ValueError):
            _core.app.logger.warning("AccDivide authenticated catalog refresh failed; trying public catalog fallback")

    # The primary integration above remains the authenticated API. This
    # read-only fallback keeps Accounting current while an already-running
    # AccDivide process is waiting for its next restart to load that endpoint.
    fallback_url = (os.environ.get("ACCDIVIDE_PUBLIC_CATALOG_URL") or "").strip()
    if fallback_url:
        try:
            response = requests.get(fallback_url, timeout=5)
            response.raise_for_status()
            products = _products_from_public_catalog_html(response.text)
            offers_url = (os.environ.get("ACCDIVIDE_PUBLIC_OFFERS_URL") or "").strip()
            if offers_url:
                offers_response = requests.get(offers_url, timeout=5)
                offers_response.raise_for_status()
                products.update(_products_from_public_offers_html(offers_response.text))
            if products:
                _catalog_cache.update(expires=now + CATALOG_CACHE_SECONDS, products=products)
                return products
        except requests.RequestException:
            _core.app.logger.warning("AccDivide public catalog fallback refresh failed; retaining last known catalog")
    return _catalog_cache["products"] or _core.PRODUCTS


def _products_from_payload(payload):
    products = {}
    for item in payload.get("products", []) if isinstance(payload, dict) else []:
        slug = str(item.get("slug") or "").strip()
        name = str(item.get("name") or "").strip()
        if slug and name:
            products[slug] = {"label": name, "category": str(item.get("category") or name)}
    return products


def _products_from_public_catalog_html(page: str):
    """Extract only public active listing names/slugs from AccDivide /products."""
    products = {}
    for card in re.findall(r'<article class="catalog-card">(.*?)</article>', page, flags=re.DOTALL):
        slug_match = re.search(r'action="/buy-now/([a-z0-9-]+)"', card)
        name_match = re.search(r'<h2>\s*(.*?)\s*</h2>', card, flags=re.DOTALL)
        if slug_match and name_match:
            slug = slug_match.group(1).strip()
            name = html.unescape(re.sub(r'<[^>]+>', '', name_match.group(1))).strip()
            if slug and name:
                products[slug] = {"label": name, "category": name}
    return products


def _products_from_public_offers_html(page: str):
    """Add currently purchasable AccDivide Special Offers using stable offer SKUs."""
    products = {}
    for card in re.findall(r'<article class="offer-public-card">(.*?)</article>', page, flags=re.DOTALL):
        offer_match = re.search(r'href="/offers/([a-f0-9-]{36})"', card)
        name_match = re.search(r'<h2>\s*<a[^>]*>\s*(.*?)\s*</a>\s*</h2>', card, flags=re.DOTALL)
        if offer_match and name_match:
            offer_id = offer_match.group(1).strip()
            name = html.unescape(re.sub(r'<[^>]+>', '', name_match.group(1))).strip()
            if offer_id and name:
                products[f"offer:{offer_id}"] = {
                    "label": f"{name} (Special Offer)",
                    "category": "Special Offer",
                }
    return products


def _business_products():
    # The recovered core requires a non-empty product key.  Keep a neutral
    # built-in choice so a business can record general income without having
    # to create a fake product in its private catalogue.
    return {
        "general_income": {"label": "General income", "category": "General income"},
        **{f"product:{row['id']}": {"label": row["name"], "category": row["name"]}
           for row in _core.db.fetch_products()},
    }


def _seed_legacy_products():
    token = _core.db.set_active_database(None)
    try:
        _core.db.init_db()
        if not _core.db.fetch_products(include_archived=True):
            for item in _core.PRODUCTS.values():
                _core.db.add_product(item.get("label") or item.get("category") or "Untitled product")
    finally:
        _core.db.reset_active_database(token)
def _refresh_live_catalog():
    # Products are tenant data; never use another business's catalogue.
    if session.get("logged_in") and getattr(g, "business", None):
        _core.PRODUCTS = _business_products()
        _core.EXPENSE_TEMPLATES = {**SYSTEM_EXPENSE_TEMPLATES}
        return    # The route still gets a dict, preserving the current template contract.
    products = _catalog_products()
    _core.PRODUCTS = products
    # Expense product costs use exactly the same active catalog as sales;
    # non-product accounting categories are retained separately.
    _core.EXPENSE_TEMPLATES = {
        **{slug: {"label": item["label"], "category": item["category"]}
           for slug, item in products.items()},
        **SYSTEM_EXPENSE_TEMPLATES,
    }


def _processor_fee_template(method: str, event: str) -> str:
    return f"{method.lower()}_{'refund_fee' if event in {'refund', 'chargeback'} else 'processing_fee'}"


def _actor():
    return session.get("user") or "accounting-api"

# Readable onboarding layer for fresh, GitHub-ready installations.
def _auth_db():
    conn = _core.db.master_connect()
    conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL, is_owner INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    conn.commit()
    return conn

BUSINESS_DB_DIR = Path(os.environ.get("ACCOUNTING_BUSINESS_DB_DIR") or PROJECT_DIR / "instance" / "businesses").resolve()


def _master_db():
    return _core.db.master_connect()


def _init_business_registry():
    """Create additive tenant metadata and attach all current owners to legacy."""
    conn = _master_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                database_path TEXT NOT NULL DEFAULT '',
                reporting_currency TEXT NOT NULL DEFAULT 'USD' CHECK(reporting_currency IN ('USD', 'CAD')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_members (
                business_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('owner', 'member')),
                created_at TEXT NOT NULL,
                PRIMARY KEY (business_id, user_id),
                FOREIGN KEY(business_id) REFERENCES businesses(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(businesses)").fetchall()}
        if "native_currency" not in columns:
            conn.execute("ALTER TABLE businesses ADD COLUMN native_currency TEXT")
        legacy = conn.execute("SELECT id FROM businesses WHERE database_path = '' LIMIT 1").fetchone()
        if not legacy:
            cur = conn.execute("INSERT INTO businesses (name, database_path, reporting_currency, active, created_at) VALUES ('Existing business', '', 'USD', 1, datetime('now'))")
            legacy_id = int(cur.lastrowid)
        else:
            legacy_id = int(legacy['id'])
        users_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if users_exists:
            for user in conn.execute("SELECT id FROM users WHERE is_owner = 1").fetchall():
                conn.execute("INSERT OR IGNORE INTO business_members (business_id, user_id, role, created_at) VALUES (?, ?, 'owner', datetime('now'))", (legacy_id, user['id']))
        conn.commit()
    finally:
        conn.close()


def _business_for_user(username, requested_id=None):
    conn = _master_db()
    try:
        sql = """SELECT b.* , bm.role FROM businesses b JOIN business_members bm ON bm.business_id = b.id JOIN users u ON u.id = bm.user_id WHERE lower(u.username) = lower(?) AND b.active = 1"""
        rows = conn.execute(sql, (username,)).fetchall()
        if not rows:
            return None
        if requested_id is not None:
            for row in rows:
                if int(row['id']) == int(requested_id):
                    return dict(row)
            # An explicit workspace id must never silently select a different
            # workspace.  Callers turn this into a 404 without disclosing it.
            return None
        return dict(rows[0])
    finally:
        conn.close()


def _businesses_for_user(username):
    """Return only active workspaces the authenticated user belongs to."""
    conn = _master_db()
    try:
        rows = conn.execute(
            """SELECT b.*, bm.role FROM businesses b
               JOIN business_members bm ON bm.business_id = b.id
               JOIN users u ON u.id = bm.user_id
               WHERE lower(u.username) = lower(?) AND b.active = 1
               ORDER BY b.id""",
            (username,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _effective_currency(business=None):
    business = business or getattr(g, "business", {})
    return (business.get("native_currency") or business.get("reporting_currency") or "USD").upper()


def _set_business_native_currency(currency):
    currency = (currency or "").upper()
    if currency not in {"USD", "TOMAN"}:
        raise ValueError("Unsupported native currency")
    conn = _master_db()
    try:
        conn.execute(
            "UPDATE businesses SET native_currency = ? WHERE id = ?",
            (currency, int(g.business["id"])),
        )
        conn.commit()
    finally:
        conn.close()
    g.business["native_currency"] = currency


def _apply_persian_currency_default():
    """Initialize an unset Persian owner's workspace as native Toman."""
    business = getattr(g, "business", None)
    if (
        business
        and business.get("role") == "owner"
        and session.get("language") == "fa"
        and not business.get("native_currency")
    ):
        _set_business_native_currency("TOMAN")


def _ensure_owner_membership(username):
    _init_business_registry()
    conn = _master_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
        legacy = conn.execute("SELECT id FROM businesses WHERE database_path = '' LIMIT 1").fetchone()
        if user and legacy:
            conn.execute("INSERT OR IGNORE INTO business_members (business_id, user_id, role, created_at) VALUES (?, ?, 'owner', datetime('now'))", (legacy['id'], user['id']))
            conn.commit()
    finally:
        conn.close()


def _select_business_ledger():
    if not session.get('logged_in'):
        return None
    business = _business_for_user(session.get('user', ''), session.get('business_id'))
    if not business:
        # A newly authenticated user may not have a workspace yet.  Preserve
        # the authenticated session only long enough to create that first
        # private workspace; every ledger, export, and accounting route still
        # requires a selected business below.
        if request.endpoint in {"new_business", "logout", "change_password"}:
            return None
        session.clear()
        return None
    session['business_id'] = int(business['id'])
    ledger_path = business['database_path'] or None
    g.business = business
    ledger_file = Path(ledger_path) if ledger_path else None
    if ledger_file and not ledger_file.is_absolute():
        ledger_file = PROJECT_DIR / ledger_file
    g.business_db_token = _core.db.set_active_database(ledger_file)
    return None


def _clear_business_ledger(_error=None):
    token = g.pop('business_db_token', None)
    if token is not None:
        _core.db.reset_active_database(token)
def _has_users():
    conn = _auth_db()
    try: return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally: conn.close()

def _username(value):
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", value) else None

def _password_error(password, confirmation):
    if len(password or "") < 12: return "Use a password with at least 12 characters."
    return None if password == confirmation else "Passwords do not match."

def _sign_in(username):
    session.clear()
    session.update(logged_in=True, user=username, security_ok=True, session_version=_core._current_session_version(username))
    business = _business_for_user(username)
    if business:
        session['business_id'] = int(business['id'])

def setup():
    if _has_users(): return _core.redirect(_core.url_for("login"))
    error = None
    if request.method == "POST":
        username, password = _username(request.form.get("username")), request.form.get("password", "")
        reporting_currency = (request.form.get("reporting_currency") or "USD").upper()
        error = _password_error(password, request.form.get("confirm_password", ""))
        if not username: error = "Choose a username using 3–32 letters, numbers, dots, underscores, or hyphens."
        if reporting_currency not in {"USD", "TOMAN"}: error = "Choose USD or Toman as the default currency."
        if not error:
            conn = _auth_db()
            try:
                conn.execute("INSERT INTO users (username, password_hash, is_owner, created_at) VALUES (?, ?, 1, datetime('now'))", (username, generate_password_hash(password)))
                conn.commit()
            finally: conn.close()
            _ensure_owner_membership(username)
            conn = _master_db()
            try:
                conn.execute("UPDATE businesses SET native_currency = ? WHERE database_path = ''", (reporting_currency,))
                conn.commit()
            finally:
                conn.close()
            _sign_in(username)
            return _core.redirect(_core.url_for("dashboard"))
    return _core.render_template("auth/setup.html", error=error)

def login():
    if not _has_users(): return _core.redirect(_core.url_for("setup"))
    error = None
    if request.method == "POST":
        username = _username(request.form.get("username"))
        conn = _auth_db()
        try: user = conn.execute("SELECT username, password_hash FROM users WHERE username = ?", (username or "",)).fetchone()
        finally: conn.close()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            _sign_in(user["username"])
            return _core.redirect(_core.url_for("dashboard"))
        error = "Incorrect username or password."
    return _core.render_template("auth/login.html", error=error)

def signup():
    if not _has_users(): return _core.redirect(_core.url_for("setup"))
    if os.environ.get("ALLOW_SIGNUPS", "0") != "1":
        return _core.render_template("auth/signup.html", error="Sign-ups are disabled by the workspace owner.", disabled=True)
    error = None
    if request.method == "POST":
        username, password = _username(request.form.get("username")), request.form.get("password", "")
        error = _password_error(password, request.form.get("confirm_password", ""))
        if not username: error = "Choose a username using 3–32 letters, numbers, dots, underscores, or hyphens."
        if not error:
            conn = _auth_db()
            try:
                conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, datetime('now'))", (username, generate_password_hash(password)))
                conn.commit()
            except Exception: error = "That username is already in use."
            finally: conn.close()
            if not error:
                _sign_in(username)
                return _core.redirect(_core.url_for("dashboard"))
    return _core.render_template("auth/signup.html", error=error, disabled=False)

def _is_owner():
    return bool(getattr(g, "business", None) and g.business.get("role") == "owner")


def create_business(name: str, reporting_currency: str = "USD"):
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Business name is required")
    reporting_currency = reporting_currency.upper()
    if reporting_currency not in {"USD", "TOMAN"}:
        raise ValueError("Choose USD or Toman as the workspace currency")
    filename = f"{uuid4().hex}.db"
    ledger_file = BUSINESS_DB_DIR / filename
    # Keep production metadata portable while allowing tests to use a fully
    # isolated temporary directory.
    try:
        relative_path = str(ledger_file.relative_to(PROJECT_DIR))
    except ValueError:
        relative_path = str(ledger_file)
    conn = _master_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE lower(username) = lower(?)", (session.get("user", ""),)).fetchone()
        if not user:
            raise PermissionError("Unknown user")
        cur = conn.execute("INSERT INTO businesses (name, database_path, reporting_currency, native_currency, active, created_at) VALUES (?, ?, 'USD', ?, 1, datetime('now'))", (name, relative_path, reporting_currency))
        business_id = int(cur.lastrowid)
        conn.execute("INSERT INTO business_members (business_id, user_id, role, created_at) VALUES (?, ?, 'owner', datetime('now'))", (business_id, user['id']))
        conn.commit()
    finally:
        conn.close()
    token = _core.db.set_active_database(ledger_file)
    try:
        _core.db.init_db()
    finally:
        _core.db.reset_active_database(token)
    return business_id


def new_business():
    """Minimal owner-created workspace flow; signup remains disabled by default."""
    if not session.get("logged_in"):
        return _core.redirect(_core.url_for("login"))
    error = None
    if request.method == "POST":
        try:
            business_id = create_business(request.form.get("name", ""), request.form.get("reporting_currency", "USD"))
        except (ValueError, PermissionError) as exc:
            error = str(exc)
        else:
            session["business_id"] = business_id
            flash("Business workspace created. It starts with no products or transactions.", "success")
            return _core.redirect(_core.url_for("dashboard"))
    return _core.render_template("new_business.html", error=error)


def select_business(business_id: int):
    business = _business_for_user(session.get("user", ""), business_id)
    if not business:
        abort(404)
    session['business_id'] = int(business['id'])
    return _core.redirect(_core.url_for("dashboard"))


def choose_language():
    if not session.get("logged_in"):
        return _core.redirect(_core.url_for("login"))
    code = (request.form.get("language") or "en").lower()
    if code not in _language_packs():
        abort(400)
    session["language"] = code
    if code == "fa" and _is_owner() and not g.business.get("native_currency"):
        _set_business_native_currency("TOMAN")
        flash("Workspace currency changed to Toman. No USD conversion was applied.", "success")
    return _core.redirect(_core.url_for("dashboard"))


@_core.app.context_processor
def _workspace_template_context():
    username = session.get("user") if session.get("logged_in") else None
    business = getattr(g, "business", {})
    return {
        "available_businesses": _businesses_for_user(username) if username else [],
        "reporting_currency": _effective_currency(business),
        "language_packs": list(_language_packs().values()),
        "current_language": _current_language(),
    }

def products_page():
    if not _is_owner():
        abort(403)
    if request.method == "POST":
        try:
            action = request.form.get("action")
            product_id = int(request.form.get("product_id") or 0)
            if action == "add":
                _core.db.add_product(request.form.get("name", ""), request.form.get("description", ""))
            elif action == "edit":
                if not _core.db.update_product(product_id, request.form.get("name", ""), request.form.get("description", "")):
                    abort(404)
            elif action in {"archive", "restore"}:
                if not _core.db.set_product_active(product_id, action == "restore"):
                    abort(404)
            elif action == "delete":
                deleted, reason = _core.db.delete_product_if_unused(product_id)
                if reason == "not_found":
                    abort(404)
                if not deleted:
                    flash("This product is used by historical Income entries, so it was kept. Archive it instead.", "error")
                    return _core.redirect(_core.url_for("products_page"))
            else:
                abort(400)
            flash("Product saved.", "success")
        except ValueError:
            flash("A product name is required.", "error")
        return _core.redirect(_core.url_for("products_page"))
    edit_product = None
    if request.args.get("edit"):
        try:
            edit_product = _core.db.fetch_product(int(request.args["edit"]))
        except ValueError:
            abort(404)
        if not edit_product:
            abort(404)
    return _core.render_template("products.html", products=_core.db.fetch_products(include_archived=True), edit_product=edit_product)


def tools():
    if not _is_owner():
        abort(403)
    if request.method == "POST":
        reporting_currency = (request.form.get("reporting_currency") or "").upper()
        if reporting_currency not in {"USD", "TOMAN"}:
            abort(400)
        _set_business_native_currency(reporting_currency)
        flash(f"Default currency set to {reporting_currency}. Existing entries were not changed.", "success")
        return _core.redirect(_core.url_for("tools"))
    return _core.render_template("tools.html", partners=_core.config.PARTNERS)
def partner_settings():
    if not _is_owner():
        abort(403)
    return _original_partner_settings()
def change_password():
    if not session.get("logged_in"): return _core.redirect(_core.url_for("login"))
    username, error = session.get("user", ""), None
    if request.method == "POST":
        conn = _auth_db()
        try:
            user = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
            if not user or not check_password_hash(user["password_hash"], request.form.get("current_password", "")): error = "Your current password is incorrect."
            else:
                new = request.form.get("new_password", "")
                error = _password_error(new, request.form.get("confirm_password", ""))
                if not error:
                    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (generate_password_hash(new), username))
                    conn.commit()
                    flash("Password updated.", "success")
                    return _core.redirect(_core.url_for("dashboard"))
        finally: conn.close()
    return _core.render_template("change_password.html", username=username, error=error)

# The migration is additive: existing data stays in the legacy ledger.
_init_business_registry()
_core.app.before_request(_select_business_ledger)
_core.app.before_request(_apply_persian_currency_default)
_core.app.teardown_request(_clear_business_ledger)
_seed_legacy_products()
_configure_core_options()

_core_compute_amount_usd_and_fx = _core.compute_amount_usd_and_fx
_core_format_money = _core.format_money


def _compute_native_amount(amount_original, currency, raw_fx):
    """Treat Toman as a workspace-native unit; it is never converted to USD."""
    if str(currency).upper() == "TOMAN":
        return _core_compute_amount_usd_and_fx(amount_original, "USD", "1")
    return _core_compute_amount_usd_and_fx(amount_original, currency, raw_fx)


def _format_native_money(value):
    if has_request_context() and _effective_currency() == "TOMAN":
        amount = float(value or 0)
        sign = "-" if amount < 0 else ""
        return f"{sign}{abs(amount):,.0f} Toman"
    return _core_format_money(value)


_core.compute_amount_usd_and_fx = _compute_native_amount
_core.format_money = _format_native_money
_core.app.before_request(_refresh_live_catalog)

# The compiled route passes its legacy amount field to db.insert_revenue().
# Intercept only the new optional retained-amount form value, keeping the route
# source-compatible while making the persisted journal semantics correct.
_core_insert_revenue = _core.db.insert_revenue


def _insert_revenue_with_profit_kept(*args, **kwargs):
    if has_request_context():
        product_key = (request.form.get("product") or "").strip()
        if product_key.startswith("product:"):
            try:
                product = _core.db.fetch_product(int(product_key.split(":", 1)[1]))
                if product:
                    kwargs["product_name"] = product["name"]
            except ValueError:
                pass
    if has_request_context() and "profit_kept_amount" not in kwargs:
        raw = (request.form.get("profit_kept_amount") or "").strip()
        if raw:
            kwargs["profit_kept_amount"] = raw
    return _core_insert_revenue(*args, **kwargs)


_core.db.insert_revenue = _insert_revenue_with_profit_kept


@_core.app.before_request
def _validate_revenue_profit_kept():
    if request.method != "POST" or request.endpoint not in {"new_revenue", "edit_revenue"}:
        return None
    raw = (request.form.get("profit_kept_amount") or "").strip()
    if not raw:
        return None
    try:
        gross = float(request.form.get("amount_original") or 0)
        kept = float(raw)
    except ValueError:
        return "Profit kept must be a valid amount.", 400
    if kept < 0 or gross < 0 or kept > gross:
        return "Profit kept must be between zero and the gross sale amount.", 400
    return None

def _template_from_payment_method(payment_method: str) -> str:
    """Derive revenue templates while preserving existing expense templates."""
    caller = inspect.currentframe().f_back
    try:
        if caller and "expense" in caller.f_code.co_name:
            return caller.f_locals.get("template", "other")
    finally:
        del caller
    return {
        "Cash": "cash_sale",
        "e-Transfer": "etransfer_sale",
        "Crypto": "crypto_sale",
        "Stripe": "stripe_sale",
        "PayPal": "paypal_sale",
    }.get(payment_method, "other")

_core.revenue_template_for_payment_method = _template_from_payment_method


# The recovered core hard-codes the old five-item list when rendering these
# forms (even though it validates against PAYMENT_METHODS). Replace only their
# GET rendering; POST processing remains the proven core implementation.
_original_new_revenue = _core.app.view_functions["new_revenue"]
_original_new_expense = _core.app.view_functions["new_expense"]
_original_edit_revenue = _core.app.view_functions["edit_revenue"]
_original_edit_expense = _core.app.view_functions["edit_expense"]


@_core.require_full_security
def new_revenue():
    if request.method == "POST":
        return _original_new_revenue()
    _core.db.init_db()
    return _core.render_template(
        "revenue_form.html", partners=_core.config.PARTNERS,
        revenue_templates=_core.REVENUE_TEMPLATES, products=_core.PRODUCTS,
        payment_methods=list(PAYMENT_METHODS),
    )


@_core.require_full_security
def new_expense():
    if request.method == "POST":
        return _original_new_expense()
    _core.db.init_db()
    return _core.render_template(
        "expense_form.html", partners=_core.config.PARTNERS,
        has_partner_split=len(_core.config.PARTNERS) > 1,
        expense_templates=_core.EXPENSE_TEMPLATES, payment_methods=list(PAYMENT_METHODS),
    )


@_core.require_full_security
def edit_revenue(row_id: int):
    if request.method == "POST":
        return _original_edit_revenue(row_id)
    _core.db.init_db()
    row = _core.db.fetch_one("revenue", row_id)
    if not row or row.get("status") != "posted":
        flash("That revenue entry can't be edited (not found or not currently posted).", "error")
        return _core.redirect(_core.url_for("dashboard"))
    return _core.render_template(
        "revenue_form.html", partners=_core.config.PARTNERS,
        revenue_templates=_core.REVENUE_TEMPLATES, products=_core.PRODUCTS,
        payment_methods=list(PAYMENT_METHODS), edit_row=row,
    )


@_core.require_full_security
def edit_expense(row_id: int):
    if request.method == "POST":
        return _original_edit_expense(row_id)
    _core.db.init_db()
    row = _core.db.fetch_one("expenses", row_id)
    if not row or row.get("status") != "posted":
        flash("That expense entry can't be edited (not found or not currently posted).", "error")
        return _core.redirect(_core.url_for("dashboard"))
    return _core.render_template(
        "expense_form.html", partners=_core.config.PARTNERS,
        has_partner_split=len(_core.config.PARTNERS) > 1,
        expense_templates=_core.EXPENSE_TEMPLATES, payment_methods=list(PAYMENT_METHODS), edit_row=row,
    )


_core.app.view_functions.update({
    "new_revenue": new_revenue,
    "new_expense": new_expense,
    "edit_revenue": edit_revenue,
    "edit_expense": edit_expense,
})


@_core.app.post("/revenue/<int:row_id>/refund")
@_core.require_full_security
def refund_or_chargeback_revenue(row_id: int):
    """Reverse a posted processor sale and optionally book the processor fee.

    This creates ledger entries; it never deletes or mutates the original
    amount, which keeps a complete audit trail for refunds and chargebacks.
    """
    row = _core.db.fetch_one("revenue", row_id)
    if not row:
        abort(404)
    method = str(row.get("payment_method") or "")
    if method not in PROCESSOR_METHODS:
        flash("Only Stripe or PayPal revenue can be refunded here.", "error")
        return _core.redirect(_core.url_for("dashboard"))

    event = (request.form.get("event") or "refund").strip().lower()
    if event not in {"refund", "chargeback"}:
        abort(400)
    reason = (request.form.get("reason") or event).strip()
    reversal_id = _core.db.reverse_transaction("revenue", row_id, _actor(), reason)
    if not reversal_id:
        flash("This sale is no longer posted and cannot be reversed again.", "error")
        return _core.redirect(_core.url_for("dashboard"))

    raw_fee = (request.form.get("processor_fee") or "").strip()
    if raw_fee:
        try:
            fee = float(raw_fee)
        except ValueError:
            fee = -1
        if fee < 0:
            flash("Revenue reversed, but the processor fee was not recorded (invalid amount).", "warning")
            return _core.redirect(_core.url_for("dashboard"))
        if fee > 0:
            currency = row.get("currency") or "USD"
            fx = float(row.get("fx_to_usd") or 1)
            _core.db.insert_expense(
                row.get("entry_date") or str(date.today()),
                f"{method} {'chargeback' if event == 'chargeback' else 'refund'} fee",
                fee, currency, fx, fee * fx, row.get("collected_by"), 1, None,
                f"{event.title()} fee for revenue #{row_id}; reversal #{reversal_id}",
                created_by=_actor(),
                idempotency_key=f"{method.lower()}-{event}-fee-{row_id}",
                template=_processor_fee_template(method, event), category="Processor fee",
                payment_method=method,
            )
    flash(f"{method} {event} recorded as a reversal; audit history is preserved.", "success")
    return _core.redirect(_core.url_for("dashboard"))


def export_business_backup():
    """Return only the active business ledger; never expose the master registry."""
    if not _is_owner():
        abort(403)
    source = _core.db.active_database_path()
    handle = tempfile.NamedTemporaryFile(prefix="accounting-business-", suffix=".db", delete=False)
    handle.close()
    destination = sqlite3.connect(handle.name)
    source_conn = sqlite3.connect(source)
    try:
        source_conn.backup(destination)
        # The legacy ledger is also the user/business registry. Strip only the
        # registry tables from its exported copy, not accounting records.
        for table in ("business_members", "businesses", "users"):
            destination.execute(f"DROP TABLE IF EXISTS {table}")
        destination.commit()
    finally:
        source_conn.close()
        destination.close()
    @after_this_request
    def _remove_backup(response):
        try:
            Path(handle.name).unlink(missing_ok=True)
        except OSError:
            pass
        return response
    return send_file(handle.name, as_attachment=True, download_name="business-accounting-backup.db", mimetype="application/x-sqlite3")

@_core.app.post("/api/integrations/confirmed-payment")
def ingest_confirmed_payment():
    """Optional server-to-server importer, intentionally restricted to confirmed events.

    AccDivide may call this *from verified webhook processing only*.  Browser
    return/success pages cannot authenticate to this endpoint and are rejected.
    """
    token = (os.environ.get("ACCOUNTING_INGEST_TOKEN") or "").strip()
    supplied = request.headers.get("X-Accounting-Token", "")
    if not token or not hmac.compare_digest(supplied, token):
        abort(401)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or str(body.get("status") or "").lower() != "confirmed":
        return jsonify({"ok": False, "error": "confirmed_webhook_required"}), 400
    method = str(body.get("payment_method") or "")
    if method not in {"Stripe", "PayPal", "Crypto"}:
        return jsonify({"ok": False, "error": "unsupported_payment_method"}), 400
    try:
        gross = float(body["gross_sale_amount"])
        profit = float(body["profit_kept"])
        fx = float(body.get("fx_to_usd") or 1)
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_amounts"}), 400
    if gross < 0 or profit < 0 or profit > gross:
        return jsonify({"ok": False, "error": "invalid_sale_amounts"}), 400
    event_id = str(body.get("event_id") or "").strip()
    product = str(body.get("product") or "").strip()
    if not event_id or not product:
        return jsonify({"ok": False, "error": "event_id_and_product_required"}), 400
    collected_by = str(body.get("collected_by") or (getattr(_core.config, "PARTNERS", [""])[0])).strip()
    if collected_by not in _core.config.PARTNERS:
        return jsonify({"ok": False, "error": "invalid_collector"}), 400
    currency = str(body.get("currency") or "USD").upper()
    entry_id = _core.db.insert_revenue(
        str(body.get("entry_date") or date.today()), f"{method} confirmed sale",
        gross, currency, fx, gross * fx, collected_by,
        f"Confirmed {method} webhook event {event_id}", created_by="accdivide-webhook",
        idempotency_key=f"accdivide-confirmed-{method.lower()}-{event_id}",
        template=_template_from_payment_method(method), category=f"{method} Sale",
        payment_method=method, gross_sale_amount=gross, profit_kept_amount=profit, product=product,
    )
    fee = gross - profit
    if fee > 0 and method in PROCESSOR_METHODS:
        _core.db.insert_expense(
            str(body.get("entry_date") or date.today()), f"{method} processing fee", fee,
            currency, fx, fee * fx, collected_by, 1, None,
            f"Processor fee for confirmed {method} event {event_id}", created_by="accdivide-webhook",
            idempotency_key=f"accdivide-fee-{method.lower()}-{event_id}",
            template=_processor_fee_template(method, "sale"), category="Processor fee",
            payment_method=method,
        )
    return jsonify({"ok": True, "revenue_id": entry_id}), 201

# Replace legacy environment-only authentication and add first-run pages.
_original_partner_settings = _core.app.view_functions["partner_settings"]
_core.app.jinja_env.globals["is_owner"] = _is_owner
_core.app.view_functions.update({"login": login, "change_password": change_password, "partner_settings": partner_settings, "products_page": products_page, "tools": tools, "export_backup_db": export_business_backup})
_core.app.add_url_rule("/setup", endpoint="setup", view_func=setup, methods=["GET", "POST"])
_core.app.add_url_rule("/signup", endpoint="signup", view_func=signup, methods=["GET", "POST"])
_core.app.add_url_rule("/tools", endpoint="tools", view_func=tools, methods=["GET", "POST"])
_core.app.add_url_rule("/products", endpoint="products_page", view_func=products_page, methods=["GET", "POST"]) 
_core.app.add_url_rule("/businesses/<int:business_id>/select", endpoint="select_business", view_func=select_business, methods=["POST"])
_core.app.add_url_rule("/businesses/new", endpoint="new_business", view_func=new_business, methods=["GET", "POST"])
_core.app.add_url_rule("/language", endpoint="choose_language", view_func=choose_language, methods=["POST"])
for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "5003"))
    app.run(host="0.0.0.0", port=port, debug=False)

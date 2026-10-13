# config.py
import os
import secrets
from pathlib import Path


def _secret_key():
    """Return a durable local secret, unless one is supplied by the host."""
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        return configured
    secret_file = Path(__file__).resolve().parent / "instance" / ".secret_key"
    secret_file.parent.mkdir(exist_ok=True)
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    secret_file.write_text(key, encoding="utf-8")
    return key


ADMIN_USERS = {}
for idx, default_user in (("1", None), ("2", None)):
    user = (os.environ.get(f"ADMIN_USER{idx}", default_user or "") or "").strip()
    password = os.environ.get(f"ADMIN_PASS{idx}", "")
    if user and password:
        ADMIN_USERS[user] = password

# The recovered core requires one configured account while loading. This random
# bootstrap credential is never shown or used by the replacement login flow.
if not ADMIN_USERS:
    ADMIN_USERS["bootstrap"] = secrets.token_urlsafe(32)

SECRET_KEY = _secret_key()

def _partners_from_env():
    raw = os.environ.get("ACTIVE_PARTNERS", "Owner")
    partners = []
    seen = set()
    for part in raw.split(","):
        name = " ".join(part.strip().split())
        key = name.lower()
        if name and key not in seen:
            partners.append(name)
            seen.add(key)
    return partners or ["Owner"]

PARTNERS = _partners_from_env()
DEFAULT_SPLIT = 0.5

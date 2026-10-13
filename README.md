# Accounting

A clean, self-hosted accounting workspace starter. It ships with no customer records, accounts, transaction history, logs, or secrets.

## Start locally (Windows)

The easiest option is to double-click **`Start-Accounting.bat`**. It checks the
computer first. If Python 3.14 is missing, it asks permission before installing
Python through Windows Package Manager. It never installs anything without the
user choosing **Yes**. It then creates a private local environment, installs the
required packages, starts the app, and opens `http://127.0.0.1:5003`.

If automatic installation is unavailable or fails, the launcher opens the
official Python download page and explains what to do next.

This is a local web application, not a static HTML page: the local server is
needed for login security, permissions, exports, and the SQLite database.

## Manual start

Requires Python 3.14 because the recovered accounting core is a CPython 3.14 bytecode module.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5003`. The first visit takes you to **Set up your workspace**, where you create the owner account. Passwords are stored as hashes in a local SQLite database. Fresh downloads create it at `instance/accounting.db`; it, logs, secrets, and virtual environments are excluded from Git.

## Accounts

- `/setup` creates the first owner account and only appears while no accounts exist.
- `/login` signs in existing users.
- `/signup` is present but disabled by default. Set `ALLOW_SIGNUPS=1` in a local `.env` only when trusted people should be allowed to join; all users share the same accounting workspace.
- `/account/change-password` lets a signed-in user change their password.

Copy `.env.example` to `.env` only if you need optional settings. Never commit `.env`, `instance/`, database files, or logs.

## Optional local language packages

Language-pack loading remains in the app, but the `languages/` folder is intentionally ignored by Git so personal translations are not uploaded. A fresh download works in English when that folder is absent. To use a private language pack locally, create `languages/` beside `app.py` and place valid language JSON files there.

## Deployment

Use HTTPS in production and set `COOKIE_SECURE=1`. Set `SECRET_KEY` to a long random value in your host's secret manager. Without it, the app creates a local `instance/.secret_key` automatically for convenient development.

## Important source note

The original project’s editable core source had been lost before this cleanup. `accounting_core_recovered.pyc` is retained because it contains the existing accounting workflows; the new onboarding and repository setup layer is editable in `app.py`. It is deliberately kept separate so the application remains usable while the core is later reconstructed from source.

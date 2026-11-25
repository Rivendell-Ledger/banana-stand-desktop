# The Banana Stand

A local-first household finance app. Budgets, cash-flow forecasting, net worth, portfolio and
option tracking — in one desktop app talking to a SQLite file on your own machine.

No cloud account. No subscription. No telemetry. Your bank credentials and your data stay on your
computer.

> *There's always money in the banana stand.*

**Status:** pre-release. Windows installer is built and tested; macOS and Linux run from source and
are not yet packaged.

---

## What it does

- **Accounts and net worth** — checking, savings, credit, loans, assets. Liability math and net worth
  snapshots handled properly.
- **Transactions** — full-text search, inline edits, tags, splits, bulk actions, soft delete with
  restore, and CSV import with duplicate detection.
- **Budgets** — monthly envelopes per category, rollover, auto-budget from the last three months, and
  a "safe to spend" figure that accounts for bills still to come.
- **Bills and forecast** — recurring charges detected from history, plus a 30/60/90-day and 6-month
  cash-flow projection with a low-point warning.
- **Reports** — monthly income, expenses, savings rate, category deltas against the previous month,
  top merchants, a 12-month trend, and CSV export.
- **Goals and alerts** — savings targets with required monthly pace, plus alerts for over-budget
  categories, buffer dips, bills due soon, unusual charges and goals behind schedule.
- **Portfolio** — holdings with allocation, unrealised gain and money-weighted return (XIRR), plus
  sold option positions with premium, breakeven, cash secured and days to expiry.
- **Bank sync (optional)** — connect banks through Plaid using your own developer keys. Each user
  brings their own; the app never ships or brokers credentials. CSV import and manual entry work
  without Plaid.

## Install

### Windows

Download `BananaStandSetup.exe` from the [Releases](../../releases) page and run it. It is a
per-user install — no administrator password. Windows SmartScreen may warn because the installer is
not yet code-signed: choose **More info → Run anyway**.

The app starts a small local server and opens in your browser at `http://127.0.0.1:8093`. A banana
icon stays in the system tray; closing the window hides it there.

See [SETUP-WINDOWS.txt](SETUP-WINDOWS.txt) for the step-by-step, including Plaid, the recovery kit,
phone access on your own Wi-Fi, updates and uninstall.

### From source (Windows, macOS, Linux)

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_banana_stand.py
```

Or use the Makefile:

```bash
make install    # venv, dependencies, runtime directories
make run        # http://127.0.0.1:8093
```

On first run you are sent to `/setup` to create your household and owner account. That page exists
only while the users table is empty.

## Bank connection (optional)

1. Create a developer account at [dashboard.plaid.com](https://dashboard.plaid.com) and choose
   **Personal use**.
2. Open **Developers → Keys** and copy the Client ID and the production secret.
3. Paste both into the app's **Connect your banks** screen. They are verified with Plaid before being
   stored, then encrypted at rest with a key generated on your machine.

Plaid's free Trial allows a limited number of bank connections, and those slots are permanent —
reconnect an existing bank rather than adding it twice.

## Recovery kit

On setup you are prompted to create a **recovery kit**: one small text file encrypted with a
passphrase you choose. Store it somewhere other than the computer running the app, and keep the
passphrase separate from the file. Without the kit, losing the machine means re-linking every bank.

`KitDoctor.exe` ships next to the app for diagnosing kit problems — `check`, `inspect`, `reexport`,
`restore`. It never prints the key or your secrets.

## Where your data lives

On Windows: `%LOCALAPPDATA%\BananaStand`

```
data\      the ledger database (SQLite)
backups\   nightly snapshots
secret\    the encryption key
logs\      desktop.log, app.log
```

Move the whole folder to move the installation; the recovery kit is deliberately kept separate,
because a file containing both the database and its key protects nothing.

Updates replace program files only. The ledger, backups, encryption key and Plaid credentials are
untouched, and bank connections stay linked.

## Security posture

- Argon2id password hashing (64 MB, 3 passes, 4 lanes).
- Opaque session tokens; only the SHA-256 hash is stored. HttpOnly, SameSite=Lax, 30-day rolling
  expiry, Secure cookies in production.
- CSRF double-submit on every POST; strict CSP with no inline scripts or styles.
- Login throttling: five failures per IP and email per 15 minutes.
- Binds `127.0.0.1` by default. LAN access is opt-in, off until you turn it on.
- Bank tokens encrypted with a per-installation key; database, backups and key file are mode 600.
- No telemetry, no third-party scripts, no CDN.
- **Update check is off by default.** If you turn it on in Settings, the app makes one
  unauthenticated request to GitHub's public release API every six hours to read the newest version
  number. It sends no account, no identifier and nothing about your ledger. Offline failures are
  silent.

See [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The suite covers first-run setup, CSRF and session handling, money parsing, net worth math,
transaction writes and search, Plaid linking with encrypted tokens, the rule engine and CSV import,
envelopes and rollover, recurring detection and forecasting, portfolio math, reports, goals, alerts,
verified backups and the desktop launcher.

## Layout

```
app/            FastAPI application, templates, static assets
desktop/        tray icon, launcher, scheduler, Windows build helper
migrations/     numbered SQL, applied in order
tests/          pytest suite
assets/         installer and app icons
kit_doctor.py   recovery-kit diagnostic CLI
BananaStand.spec / BananaStand.iss    PyInstaller and Inno Setup definitions
```

## License

MIT — see [LICENSE](LICENSE).

"The Banana Stand" is a working name and may change before the public release.

# Contributing

Thanks for taking a look. This is a small project, so a few ground rules keep it manageable.

## Before you start

- For anything larger than a bug fix, open an issue first and describe what you want to change.
  It saves you from building something that does not fit the design.
- Security problems do not go in issues — see [SECURITY.md](SECURITY.md).

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python run_banana_stand.py
```

The app runs at `http://127.0.0.1:8093`. Runtime directories (`data/`, `backups/`, `logs/`,
`secret/`) are created by `make install` or on first desktop run.

## What a good change looks like

- **Tests.** Every behaviour change needs a test, and the whole suite must pass. The suite uses
  FastAPI's `TestClient` against a temporary database.
- **No new dependencies without a reason.** This app is meant to install and run offline; adding a
  package that phones home or pulls a large tree needs justification in the issue.
- **No inline scripts or styles.** The CSP is strict; UI behaviour goes through delegated listeners
  in `app/static/app.js`, using `data-autosubmit` and `data-confirm`.
- **Money is integer cents.** Never introduce floats into amounts.
- **No personal data in the repo.** No real names, hostnames, addresses, account numbers or bank
  data in code, tests or fixtures.

## Commits and pull requests

- One topic per pull request.
- Explain *why* in the commit body, not just what changed.
- Mention anything that changes the database schema, and include a migration.
- Run `make clean && .venv/bin/python -m pytest -q` before opening the PR.

## License

By contributing you agree that your work is licensed under the MIT License (see [LICENSE](LICENSE)).

# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report it privately through GitHub's [private vulnerability reporting](../../security/advisories/new)
on this repository. Include:

- what the issue is and where it lives (file, route, or screen)
- steps to reproduce it
- what an attacker could do with it
- the version or commit you tested

You can expect an acknowledgement within a few days. Once a fix is available it will be released,
and the reporter credited unless they prefer otherwise.

## Scope

In scope:

- the application in this repository, run either from source or from a release build
- authentication, session handling, CSRF and CSP behaviour
- encryption of Plaid access tokens and the recovery-kit format
- anything that would leak local data to another party

Out of scope:

- issues in Plaid's own systems or bank websites
- problems that require an attacker to already have code execution on the machine
- physical access to an unlocked computer
- reports from automated scanners with no demonstrated impact

## Design notes for reviewers

- The app binds `127.0.0.1` by default. LAN access is an explicit opt-in and serves plain HTTP on a
  private network; there is no TLS termination in the app itself.
- There is no cloud component. Nothing is sent anywhere except Plaid's API and, if enabled, public
  price lookups for tickers you enter.
- The database, backups and encryption key live under the user's local application data directory.
  The recovery kit is encrypted with a user-chosen passphrase and is never written next to the key.

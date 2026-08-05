"""Isolated "is this account's Eitaa session still alive?" check.

This package is DELIBERATELY additive and self-contained, in the same spirit as
`direct/`: it defines NO new session logic of its own. It only composes pieces
that already exist and are already proven in this project:

  • capture.pool.lease            - the same warm browser every job leases
  • eitaa.driver.EitaaDriver.open / is_logged_in
                                  - the canonical login check (driver.py)
  • JobManager._wait_logged_in    - the project's own tolerance for a slow SPA
                                    boot, so a slow host is not called "dead"
  • EitaaDriver.self_peer_id      - proves the session is actually authorized
  • EitaaDriver.bridge_stats      - one real API round trip through the page
  • bot.direct_ctx / bot.contacts_store
                                  - the no-browser freshness signals the panel
                                    already shows
  • bot.cards / bot.runner.StageTracker
                                  - the existing reporting shell

Only two small wiring points exist outside this folder (a button in
`kb_account_panel`, a `pnl:check` branch, and a `run_session_check` job in
`bot/runner.py` that imports this package lazily inside a try/except). Deleting
this directory therefore degrades that one button and nothing else.
"""

__all__ = ["checker"]

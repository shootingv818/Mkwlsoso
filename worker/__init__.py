"""worker/ — spread browser work across extra servers (opt-in, isolated).

Answer to "does a worker help?": on ONE box, no -- Chromium is the bottleneck
and splitting the same RAM/CPU only causes contention. With an EXTRA server it
helps a lot: a worker there runs its OWN warm session pool, so capacity scales
linearly (N servers x K browsers) and one server crashing does not take
everything down. This package is that mechanism, modelled on Makiioo's worker/
but adapted for Eitaa + Playwright.

Two sides, one codebase (like Makiioo):
  * MASTER  — the running bot. Registers workers, keeps an SSH tunnel to each
              worker's loopback-only API, and ROUTES an account's jobs to the
              worker that owns it. A "master worker" runs jobs in-process, so
              with zero remote workers the bot behaves exactly as today.
  * WORKER  — a headless agent on another server that owns a browser pool and
              executes login/send/etc. behind a small token-protected API.

KEY DIFFERENCE FROM MAKIIOO: the Eitaa session lives in the browser PROFILE
(IndexedDB on disk), so an account is pinned to the worker holding its profile
(session affinity). Moving an account between workers means moving its profile.

Everything here is OFF by default (MASTER_AS_WORKER only = all local, exactly
like now). The pure-Python core (registry, tags, selection, affinity) is fully
tested; the SSH/Docker/tunnel/API transport is scaffolded and needs a real
second server to exercise (asyncssh/httpx + Docker), so it is imported lazily
and never runs unless a remote worker is actually added.
"""
from __future__ import annotations

__all__ = ["store", "selection"]

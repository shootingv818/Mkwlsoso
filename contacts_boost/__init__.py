"""Contact Boost — top a newly added account up with some contacts.

Isolated and opt-in, like photo_export/ and eitaa/warmpath.py: nothing here runs
unless the Settings toggle (or MKWL_BOOST=1) is on, and turning it off restores
the previous behaviour exactly.

What it does: probes a fixed number of phone numbers under a saved prefix
through `contacts.importContacts` (the same call the "Build Contacts" job
already uses), keeps whoever turns out to exist on Eitaa, and reports the REAL
contact count before and after.

Two things it does that the existing contacts job does not:

  1. It never probes the same number twice for the same account. The existing
     job's `expand_range()` always starts at index 0, so a second run with the
     same prefix submits the identical numbers and cannot add anybody.
     A per-account cursor is persisted here instead (see numbers.py).

  2. It measures the increase instead of trusting the server's count.
     `contacts.importContacts` returns a number in `imported` when the number
     belongs to a real user -- INCLUDING when that user is already your contact.
     So `imported_count` answers "does this number exist", not "is this contact
     new", and it over-reports on any re-run. The count here comes from
     `contacts.getContacts` before and after.
"""

from __future__ import annotations

__all__ = ["cards", "engine", "numbers"]

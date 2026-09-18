# ORCH v0.4 — replacement sibling artifact census

**Date:** 2026-09-18.

## Gap addressed

Replacement/restore recovery uses sibling staging, rollback, failed-forward-copy, and discard directories outside the ORCH home. A hard crash can leave one of those hidden artifacts without an authoritative replacement journal, so ordinary in-home retention cannot account for its disk usage.

`recovery inspect --replacement-home HOME` now performs a read-only census of only the hidden ORCH naming prefixes bound to that destination. It reports bytes, journal-referenced artifacts, unmanaged siblings, and unsafe symlink/tree nodes without deleting anything.

When no replacement journal exists but matching sibling artifacts remain, recovery returns `ATTENTION` / `ORPHAN_REPLACEMENT_ARTIFACTS` instead of incorrectly reporting `CLEAN`. No automatic deletion command is suggested because journal-free provenance is not strong enough to authorize removal.

With an active journal, the census distinguishes its prepared/rollback/discard/failed paths from additional unmanaged siblings while preserving the proven recovery classification.

Synchronous failure of the **first** replacement journal write is safer than a power loss: because the old home has not moved yet, ORCH now proves the prepared candidate and exact old-home identity, deletes that prepared candidate, and leaves no journal-free artifact.

## Deterministic evidence

Tests prove cleanup on initial journal-write failure, detection of an orphan rollback-style sibling without a journal, byte accounting without deletion, and exposure of an extra unmanaged sibling alongside a valid active replacement journal.

Full deterministic acceptance after this block: **116/116 PASS**, Python compile PASS, `git diff --check` PASS, state `READY`, reconcile `CLEAN`.

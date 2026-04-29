"""
Canonical user_id resolver — single source of truth for "who is this".

Nova receives the same human under several different user_id strings
depending on which entry point fires:

  - iOS WebRTC voice  → device UUID  'DEAA114F-315F-420F-9C7C-C0A6D5F6C8DA'
  - iOS text chat     → PIC user UUID 'dfd9379f-a9cd-4241-99e7-140f5e89e3cd'
  - Internal callers  → 'eleazar' or 'default'
  - Dashboard         → 'dashboard'

Before this module, each form created its own session keyspace, so the
SAME conversation appeared under two unrelated session_ids and Nova
couldn't recall what was just said. canonical_user_id() collapses them
to ONE id (the PIC canonical UUID), used by every store write/read.

Test users (test_diag_*, test_oc, etc.) are passed through unchanged
so test fixtures stay isolated.
"""

import os

# The canonical UUID is the same one used by the PIC service for the
# primary human. Every other system on the homelab (PCG, approval-
# service, CIG) already keys on this — so making Nova match closes the
# fragmentation. Override via env if a second household member is added.
CANONICAL_USER_ID = os.environ.get(
    "NOVA_CANONICAL_USER_ID",
    "dfd9379f-a9cd-4241-99e7-140f5e89e3cd",
)

# Every alias that has historically meant "the primary user" maps to
# the canonical id. Add new aliases here when you discover them in the
# field — never branch the storage key.
_ALIASES: frozenset[str] = frozenset({
    "eleazar",
    "default",
    "dashboard",
    "DEAA114F-315F-420F-9C7C-C0A6D5F6C8DA",  # iOS device UUID
    CANONICAL_USER_ID,
})


def canonical_user_id(raw: str | None) -> str:
    """Map any known alias to the canonical user_id; pass through unknowns.

    Pass-through behaviour matters: test fixtures use ids like
    'test_diag_3' and we don't want them folded into the real user.
    """
    if not raw:
        return CANONICAL_USER_ID
    if raw in _ALIASES:
        return CANONICAL_USER_ID
    return raw


def is_canonical_user(user_id: str | None) -> bool:
    return user_id == CANONICAL_USER_ID

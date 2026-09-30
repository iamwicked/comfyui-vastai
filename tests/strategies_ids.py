"""Hypothesis strategies for Job identity (task 11; design §A1, Properties 24 and 25).

- post_ids(): Post ids from the whole allowed alphabet ([a-z0-9-], 1-48, never `other`), with ids such as
  `a--b`, `0`, `-` and 48 characters drawn often; the same ids calendars() gives its Posts, so Properties 9
  and 10 cover that alphabet through calendars() itself.

A module of its own so the other strategies files stay untouched.
"""
from __future__ import annotations

from hypothesis import strategies as st

from strategies_calendar import POST_IDS


def post_ids() -> st.SearchStrategy[str]:
    """A valid Post id (design §M2)."""
    return POST_IDS

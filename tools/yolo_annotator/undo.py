"""A tiny per-document undo/redo stack.

The labeler keeps one stack per image; states are label lists (plain Python
objects) and are deep-copied on the way in and out so callers can mutate
their working list freely.
"""

from __future__ import annotations

import copy
from typing import Any


class UndoStack:
    """State based undo/redo.

    The stack always holds the *current* state at index ``_index``.  Call
    :meth:`push` after every completed modification; :meth:`undo` and
    :meth:`redo` return copies of the neighbouring states.
    """

    def __init__(self, initial: Any = None, limit: int = 100) -> None:
        self._limit = max(2, int(limit))
        self._states: list[Any] = [copy.deepcopy(initial)]
        self._index = 0

    # -- introspection ----------------------------------------------------
    @property
    def depth(self) -> int:
        return len(self._states)

    @property
    def index(self) -> int:
        return self._index

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._states) - 1

    def current(self) -> Any:
        return copy.deepcopy(self._states[self._index])

    # -- mutation ---------------------------------------------------------
    def push(self, state: Any) -> None:
        """Record a new state, dropping any redo branch."""
        # Truncate the redo tail.
        del self._states[self._index + 1:]
        self._states.append(copy.deepcopy(state))
        self._index += 1
        if len(self._states) > self._limit:
            drop = len(self._states) - self._limit
            del self._states[:drop]
            self._index -= drop

    def undo(self) -> Any | None:
        """Step back; returns the previous state or ``None`` at the bottom."""
        if not self.can_undo:
            return None
        self._index -= 1
        return copy.deepcopy(self._states[self._index])

    def redo(self) -> Any | None:
        """Step forward; returns the next state or ``None`` at the top."""
        if not self.can_redo:
            return None
        self._index += 1
        return copy.deepcopy(self._states[self._index])

    def reset(self, state: Any = None) -> None:
        self._states = [copy.deepcopy(state)]
        self._index = 0

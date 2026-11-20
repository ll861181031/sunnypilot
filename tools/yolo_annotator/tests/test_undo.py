"""Unit tests for the per-image undo/redo stack."""

from __future__ import annotations

from undo import UndoStack


def test_undo_redo_sequence():
    stack = UndoStack(["a"])
    assert not stack.can_undo and not stack.can_redo
    stack.push(["a", "b"])
    stack.push(["a", "b", "c"])
    assert stack.can_undo and not stack.can_redo

    assert stack.undo() == ["a", "b"]
    assert stack.undo() == ["a"]
    assert stack.undo() is None
    assert stack.can_redo

    assert stack.redo() == ["a", "b"]
    assert stack.redo() == ["a", "b", "c"]
    assert stack.redo() is None


def test_push_truncates_redo_branch():
    stack = UndoStack([0])
    stack.push([1])
    stack.push([2])
    stack.undo()
    stack.push([99])
    assert stack.can_redo is False
    assert stack.redo() is None
    assert stack.current() == [99]
    stack.undo()
    assert stack.current() == [1]


def test_states_are_deep_copied_in_both_directions():
    payload = [{"boxes": [1]}]
    stack = UndoStack(payload)
    payload[0]["boxes"].append(2)  # caller mutates after the fact
    assert stack.current() == [{"boxes": [1]}]

    from_stack = stack.current()
    from_stack[0]["boxes"].append(3)
    assert stack.current() == [{"boxes": [1]}]


def test_limit_drops_oldest_states():
    stack = UndoStack(0, limit=3)
    for value in range(1, 6):
        stack.push(value)
    assert stack.depth == 3
    assert stack.current() == 5
    assert stack.undo() == 4
    assert stack.undo() == 3
    assert stack.undo() is None


def test_reset_and_reuse():
    stack = UndoStack(["old"])
    stack.push(["old", "new"])
    stack.reset(["fresh"])
    assert stack.depth == 1
    assert not stack.can_undo and not stack.can_redo
    assert stack.current() == ["fresh"]


def test_limit_is_at_least_two():
    stack = UndoStack(0, limit=1)
    stack.push(1)
    stack.push(2)
    assert stack.depth == 2

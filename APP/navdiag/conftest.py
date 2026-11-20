"""Keep pytest out of the archived evidence snapshots.

`APP/navdiag/captures/<run>/before/` and `.../source-final/` hold verbatim copies of
the tree as it stood during each investigation. Their `test_*.py` files are evidence,
not part of the live suite: they import modules that only existed inside that
snapshot, so collecting them fails.

Without this file `pytest APP/navdiag` reported 38 collection errors and no test
results at all (2026-11-17), which hides whether the live tests pass. Ignoring the
snapshot directories keeps the archives intact and byte-identical while letting the
live suite run; the snapshots are still readable with an explicit path, e.g.

    pytest APP/navdiag/captures/<run>/source-final/<file>

Run them only when you are checking that specific snapshot - they assert against the
code that was current on that date, not against HEAD.
"""

collect_ignore_glob = ["captures/*"]

"""SQLite fact store: writes parser output and serves analysis queries.

TODO: open/initialise the database from ``schema.sql``; replace facts per file atomically
so re-analysis is incremental; provide typed read helpers for files, functions, edges,
data access, entry points, side effects and claims; expose confidence counts.
"""

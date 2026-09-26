"""Nova persistence boundary.

One owner for how Nova's own SQLite stores are shaped and upgraded, and for
how its small JSON state files are read and replaced:

  schema.py  every SQLite table, index and additive column, grouped into
             components and physical stores (the storage inventory)
  sqlite.py  connection policy, idempotent schema application, and the
             transactional per-store upgrade run once at setup
  files.py   JSON reads that tell missing from corrupt, and atomic writes

Each store's module (database.py, knowledge.py, decision_record.py, ...)
keeps its public functions and its own queries; it asks this package for
its schema instead of carrying its own copy. Paths, table names, columns
and backends are unchanged. Nothing here merges, rebuilds or copies data.
"""

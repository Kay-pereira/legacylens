-- LegacyLens IR schema (SQLite).
--
-- TODO: define tables for files, classes, functions, edges, data_access, entry_points,
-- side_effects, claims and llm_cache, with indexes on file paths, qualified names,
-- tables and unresolved edges. Facts reference files by repository-relative path.

-- PRAGMA foreign_keys = ON;
-- PRAGMA journal_mode = WAL;

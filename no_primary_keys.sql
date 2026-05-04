-- Lists base tables that have no PRIMARY KEY.
--
-- For binlog-mode migrations using row-based replication (RBR), each row
-- event on a table without a PRIMARY KEY forces a full table scan on the
-- replica to locate the matching row. Replication "works" but lags
-- indefinitely on any non-trivial table, often making catch-up impractical.
--
-- This check returns no rows on a fully-keyed schema. Each returned row
-- represents a table that an operator should review before binlog migration.
-- Append-only logging tables and tiny lookups are sometimes legitimately
-- without PKs and may be acceptable; large transactional tables are not.
--
-- Output (tab-separated, no header due to --skip-column-names):
--   col 1: table_schema
--   col 2: table_name
--   col 3: table_rows  (information_schema estimate; may be stale for InnoDB)
--   col 4: data_length_mb
--
-- The query joins to information_schema.table_constraints to detect the
-- absence of a PRIMARY KEY constraint specifically. A unique-not-null index
-- can serve the same role for RBR but is not flagged here; if you need that
-- nuance, refine the LEFT JOIN to also accept UNIQUE indexes whose columns
-- are all NOT NULL.

SELECT
  t.table_schema,
  t.table_name,
  IFNULL(t.table_rows, 0)                                AS table_rows,
  ROUND(IFNULL(t.data_length, 0) / 1024 / 1024, 2)       AS data_length_mb
FROM information_schema.tables t
LEFT JOIN information_schema.table_constraints tc
  ON  tc.table_schema    = t.table_schema
  AND tc.table_name      = t.table_name
  AND tc.constraint_type = 'PRIMARY KEY'
WHERE t.table_type = 'BASE TABLE'
  AND t.table_schema NOT IN ('mysql', 'information_schema',
                             'performance_schema', 'sys')
  AND tc.constraint_name IS NULL
ORDER BY data_length_mb DESC, t.table_schema, t.table_name;

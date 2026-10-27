/*
source_collations.sql

Every collation and character set actually in use on the source, at each
level the dump emits: schema, table, column, and the COLLATION_CONNECTION /
DATABASE_COLLATION stored on routines, triggers and events.

Nothing is judged here. checks.py compares this list against the target's own
information_schema.COLLATIONS and reports whatever the target does not have.
That is exact where a hardcoded pattern is not: MariaDB 11.4.5+ carries most
of MySQL's _0900_ collations as aliases but not ja/ru/zh, and not the locales
MySQL added in 8.0.30 (bs, bg, gl, mn, nb, nn, sr_latn, yo). Asking the server
gets all of that right and keeps getting it right as the alias table grows.

Object-level collations matter on their own: a procedure created under
utf8mb4_ja_0900_as_cs fails to load even when no column uses that collation.

Columns (keep in sync with orchestrator/checks.py):
  1 obj_schema   -- MUST stay first: _filter_by_schema() scopes on it
  2 object_type  SCHEMA / TABLE / COLUMN / ROUTINE / TRIGGER / EVENT
  3 object_name  ('' for schema-level)
  4 kind         COLLATION or CHARSET
  5 value        the collation or character set name
*/

SELECT SCHEMA_NAME AS obj_schema, 'SCHEMA' AS object_type, '' AS object_name,
       'COLLATION' AS kind, DEFAULT_COLLATION_NAME AS value
FROM INFORMATION_SCHEMA.SCHEMATA
WHERE SCHEMA_NAME NOT IN ('mysql','information_schema','performance_schema','sys')

UNION
SELECT SCHEMA_NAME, 'SCHEMA', '', 'CHARSET', DEFAULT_CHARACTER_SET_NAME
FROM INFORMATION_SCHEMA.SCHEMATA
WHERE SCHEMA_NAME NOT IN ('mysql','information_schema','performance_schema','sys')

UNION
SELECT TABLE_SCHEMA, 'TABLE', TABLE_NAME, 'COLLATION', TABLE_COLLATION
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND TABLE_COLLATION IS NOT NULL

UNION
SELECT TABLE_SCHEMA, 'COLUMN', CONCAT(TABLE_NAME,'.',COLUMN_NAME), 'COLLATION', COLLATION_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND COLLATION_NAME IS NOT NULL

UNION
SELECT TABLE_SCHEMA, 'COLUMN', CONCAT(TABLE_NAME,'.',COLUMN_NAME), 'CHARSET', CHARACTER_SET_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND CHARACTER_SET_NAME IS NOT NULL

-- Stored objects carry their creation-time collation in the dump.
UNION
SELECT ROUTINE_SCHEMA, 'ROUTINE', ROUTINE_NAME, 'COLLATION', COLLATION_CONNECTION
FROM INFORMATION_SCHEMA.ROUTINES
WHERE ROUTINE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND COLLATION_CONNECTION IS NOT NULL

UNION
SELECT ROUTINE_SCHEMA, 'ROUTINE', ROUTINE_NAME, 'COLLATION', DATABASE_COLLATION
FROM INFORMATION_SCHEMA.ROUTINES
WHERE ROUTINE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND DATABASE_COLLATION IS NOT NULL

UNION
SELECT TRIGGER_SCHEMA, 'TRIGGER', TRIGGER_NAME, 'COLLATION', COLLATION_CONNECTION
FROM INFORMATION_SCHEMA.TRIGGERS
WHERE TRIGGER_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND COLLATION_CONNECTION IS NOT NULL

UNION
SELECT TRIGGER_SCHEMA, 'TRIGGER', TRIGGER_NAME, 'COLLATION', DATABASE_COLLATION
FROM INFORMATION_SCHEMA.TRIGGERS
WHERE TRIGGER_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND DATABASE_COLLATION IS NOT NULL

UNION
SELECT EVENT_SCHEMA, 'EVENT', EVENT_NAME, 'COLLATION', COLLATION_CONNECTION
FROM INFORMATION_SCHEMA.EVENTS
WHERE EVENT_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND COLLATION_CONNECTION IS NOT NULL

ORDER BY 1, 2, 3, 4, 5;

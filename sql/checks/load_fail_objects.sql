/*
load_fail_objects.sql

Source constructs that do not LOAD on a MariaDB target: the dump restores
until it reaches the object, then the client reports an error and stops.
These are distinct from the compatibility warnings elsewhere in sql/checks/,
which flag behaviour differences in objects that do load.

Columns (keep in sync with orchestrator/load_failures.py):
  1 obj_schema    schema name  -- MUST stay first: _filter_by_schema() scopes on it
  2 feature       stable id, used to look up the operator-facing text
  3 mysql_error   error number the load reports when it hits this
  4 object_type   SCHEMA / TABLE / COLUMN / VIEW / ROUTINE / TRIGGER / EVENT
  5 object_name   object name ('' for schema-level)
  6 detail        the offending value, or the construct that matched

No version gating: every row here is reported regardless of target version.
Backports move the line, so the operator decides with the report in hand.
load_failures.py annotates the findings whose support has since landed.

Matching against object bodies is textual, so a construct inside a string
literal or a comment is reported too. Patterns avoid backslashes so they mean
the same thing on 5.7 and 8.x. Every catalog column referenced here exists
from 5.7 onward, so no existence probe is needed.
*/

-- ===========================================================================
-- Part 1: stored code (views, routines, triggers, events)
-- ===========================================================================
-- Object bodies are joined against the construct table below, so adding a
-- construct is one row and it is scanned across all four object types.
--
-- This finds what the SOURCE uses. Whether a given construct actually fails
-- is decided later, by orchestrator/probes.py asking the target directly --
-- support varies by build in ways no list can express (ST_IsValid resolves on
-- 12.3 and 13.1 but not 11.8; ->> parses on 13.1 but not 11.8 or 12.3).
--
-- The list below is therefore a superset of candidates, not a verdict.
-- Entries removed after probing 11.8.9/12.3.3/13.1.1: JSON_SCHEMA_VALID,
-- FORMAT_BYTES and FORMAT_PICO_TIME all resolve on every MariaDB version
-- tested and were false positives.

SELECT o.obj_schema, p.feature, p.mysql_error, o.object_type, o.object_name, p.label
FROM (
    SELECT TABLE_SCHEMA AS obj_schema, 'VIEW' AS object_type,
           TABLE_NAME AS object_name, VIEW_DEFINITION AS body
    FROM INFORMATION_SCHEMA.VIEWS
    WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
      AND VIEW_DEFINITION IS NOT NULL
  UNION ALL
    SELECT ROUTINE_SCHEMA, CONCAT('ROUTINE/', ROUTINE_TYPE),
           ROUTINE_NAME, ROUTINE_DEFINITION
    FROM INFORMATION_SCHEMA.ROUTINES
    WHERE ROUTINE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
      AND ROUTINE_DEFINITION IS NOT NULL
  UNION ALL
    SELECT TRIGGER_SCHEMA, 'TRIGGER',
           CONCAT(EVENT_OBJECT_TABLE, '.', TRIGGER_NAME), ACTION_STATEMENT
    FROM INFORMATION_SCHEMA.TRIGGERS
    WHERE TRIGGER_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
      AND ACTION_STATEMENT IS NOT NULL
  UNION ALL
    SELECT EVENT_SCHEMA, 'EVENT', EVENT_NAME, EVENT_DEFINITION
    FROM INFORMATION_SCHEMA.EVENTS
    WHERE EVENT_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
      AND EVENT_DEFINITION IS NOT NULL
) o
JOIN (
  -- feature | error | label (shown to the operator) | pattern | kind
  -- kind 'word'  : identifier, matched on non-identifier boundaries
  -- kind 'regex' : pattern used as-is
              SELECT 'lateral_in_object'   AS feature, '1064' AS mysql_error, 'LATERAL'      AS label, 'LATERAL'      AS pattern, 'word'  AS kind
    UNION ALL SELECT 'json_arrow_operator', '1064', '->> operator',   '->>',     'regex'
    UNION ALL SELECT 'json_arrow_operator', '1064', '-> operator',    '->[^>]',  'regex'
    UNION ALL SELECT 'mysql_only_operator', '1064', 'MEMBER OF',      'MEMBER[ ]+OF', 'regex'
    UNION ALL SELECT 'mysql_only_function', '1305', 'UUID_TO_BIN',    'UUID_TO_BIN',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'BIN_TO_UUID',    'BIN_TO_UUID',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'IS_UUID',        'IS_UUID',        'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'REGEXP_LIKE',    'REGEXP_LIKE',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'GROUPING',       'GROUPING',       'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ANY_VALUE',      'ANY_VALUE',      'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'JSON_STORAGE_SIZE',  'JSON_STORAGE_SIZE',  'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'JSON_STORAGE_FREE',  'JSON_STORAGE_FREE',  'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'JSON_SCHEMA_VALIDATION_REPORT', 'JSON_SCHEMA_VALIDATION_REPORT', 'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'PS_THREAD_ID',   'PS_THREAD_ID',   'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'PS_CURRENT_THREAD_ID', 'PS_CURRENT_THREAD_ID', 'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'SOURCE_POS_WAIT', 'SOURCE_POS_WAIT', 'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'VALIDATE_PASSWORD_STRENGTH', 'VALIDATE_PASSWORD_STRENGTH', 'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'GTID_SUBSET',    'GTID_SUBSET',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'GTID_SUBTRACT',  'GTID_SUBTRACT',  'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'WAIT_FOR_EXECUTED_GTID_SET', 'WAIT_FOR_EXECUTED_GTID_SET', 'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_Collect',     'ST_Collect',     'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_IsValid',     'ST_IsValid',     'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_Simplify',    'ST_Simplify',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_Validate',    'ST_Validate',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_GeoHash',     'ST_GeoHash',     'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_Latitude',    'ST_Latitude',    'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_Longitude',   'ST_Longitude',   'word'
    UNION ALL SELECT 'mysql_only_function', '1305', 'ST_PointAtDistance', 'ST_PointAtDistance', 'word'
) p
  ON (
       (p.kind = 'word'
        AND o.body REGEXP CONCAT('(^|[^A-Za-z0-9_])', p.pattern, '([^A-Za-z0-9_]|$)'))
    OR (p.kind = 'regex' AND o.body REGEXP p.pattern)
  )

-- ===========================================================================
-- Part 2: schema, table and column attributes
-- ===========================================================================

/*
Collations and character sets are NOT checked here. They are compared against
the target's own information_schema.COLLATIONS in checks.py, using the
inventory from source_collations.sql -- a hardcoded pattern cannot express
which _0900_ collations a given MariaDB build carries as aliases. Testing
showed MariaDB 11.4.5+ carries most of them but not ja/ru/zh, and none of the
locales MySQL added in 8.0.30.
*/

/*
Table-level ENCRYPTION='Y' (1911). Schema-level DEFAULT ENCRYPTION is not
covered: it lives in INFORMATION_SCHEMA.SCHEMATA_EXTENSIONS (8.0.21+), and a
schema default only reaches the dump through the tables that inherit it,
which this catches.
*/
UNION ALL
SELECT TABLE_SCHEMA, 'table_encryption', '1911',
       'TABLE', TABLE_NAME, CREATE_OPTIONS
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND CREATE_OPTIONS LIKE '%ENCRYPTION=''Y''%'

ORDER BY 1, 2, 4, 5;

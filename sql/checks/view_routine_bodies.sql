/*
Scan view and routine definitions for MySQL 8.0-specific syntax
that MariaDB does not support: JSON_TABLE, LATERAL, GROUPING,
REGEXP_LIKE, UUID_TO_BIN, BIN_TO_UUID, MEMBER OF, etc.
Returns only objects whose definitions contain potential
incompatibilities for manual review.
*/
SELECT 'VIEW' AS object_type,
       TABLE_SCHEMA AS obj_schema,
       TABLE_NAME AS obj_name,
       VIEW_DEFINITION AS body
FROM INFORMATION_SCHEMA.VIEWS
WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND (
       VIEW_DEFINITION LIKE '%JSON_TABLE%'
    OR VIEW_DEFINITION LIKE '%LATERAL%'
    OR VIEW_DEFINITION LIKE '%GROUPING(%'
    OR VIEW_DEFINITION LIKE '%REGEXP_LIKE%'
    OR VIEW_DEFINITION LIKE '%UUID_TO_BIN%'
    OR VIEW_DEFINITION LIKE '%BIN_TO_UUID%'
    OR VIEW_DEFINITION LIKE '%MEMBER OF%'
  )

UNION ALL

SELECT 'ROUTINE' AS object_type,
       ROUTINE_SCHEMA AS obj_schema,
       ROUTINE_NAME AS obj_name,
       ROUTINE_DEFINITION AS body
FROM INFORMATION_SCHEMA.ROUTINES
WHERE ROUTINE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND ROUTINE_DEFINITION IS NOT NULL
  AND (
       ROUTINE_DEFINITION LIKE '%JSON_TABLE%'
    OR ROUTINE_DEFINITION LIKE '%LATERAL%'
    OR ROUTINE_DEFINITION LIKE '%GROUPING(%'
    OR ROUTINE_DEFINITION LIKE '%REGEXP_LIKE%'
    OR ROUTINE_DEFINITION LIKE '%UUID_TO_BIN%'
    OR ROUTINE_DEFINITION LIKE '%BIN_TO_UUID%'
    OR ROUTINE_DEFINITION LIKE '%MEMBER OF%'
  )

ORDER BY object_type, obj_schema, obj_name;

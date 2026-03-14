/*
MySQL 8.0 generated (virtual/stored) columns may use MySQL-only
functions such as UUID_TO_BIN(), BIN_TO_UUID(), REGEXP_LIKE(),
JSON_TABLE(), etc. These will cause errors on MariaDB import.
*/
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, EXTRA, GENERATION_EXPRESSION
FROM INFORMATION_SCHEMA.COLUMNS
WHERE GENERATION_EXPRESSION IS NOT NULL
  AND GENERATION_EXPRESSION != ''
  AND TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
ORDER BY TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME;

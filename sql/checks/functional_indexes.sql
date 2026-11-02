-- functional_indexes.sql
-- Functional (expression) indexes are MySQL 8.0+. The identifying column
-- INFORMATION_SCHEMA.STATISTICS.EXPRESSION does not exist before 8.0
SET @has_expr := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'STATISTICS'
    AND COLUMN_NAME  = 'EXPRESSION'
);

-- EXPRESSION is user-written SQL and the client runs with --raw, so tabs and
-- newlines would split the row. Flattened here, not in the reader.
SET @sql := IF(@has_expr > 0,
  'SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX,
          LEFT(REPLACE(REPLACE(REPLACE(EXPRESSION, CHAR(9), '' ''),
                               CHAR(10), '' ''), CHAR(13), '' ''), 200)
     FROM INFORMATION_SCHEMA.STATISTICS
    WHERE EXPRESSION IS NOT NULL
      AND TABLE_SCHEMA NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX',
  'SELECT NULL, NULL, NULL, NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

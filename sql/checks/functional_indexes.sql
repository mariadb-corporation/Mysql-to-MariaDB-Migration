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

SET @sql := IF(@has_expr > 0,
  'SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME
     FROM INFORMATION_SCHEMA.STATISTICS
    WHERE EXPRESSION IS NOT NULL
      AND TABLE_SCHEMA NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')',
  'SELECT NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

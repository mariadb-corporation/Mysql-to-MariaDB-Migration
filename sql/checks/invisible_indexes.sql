/*
invisible_indexes.sql

Indexes MySQL holds invisible (8.0+). MariaDB has no equivalent: the index
loads visible and the optimizer will use it, which can change plans that were
deliberately tuned with it hidden -- often the reason it was made invisible in
the first place, as a drop rehearsal.

IS_VISIBLE arrived in 8.0, so it is probed before use; on an older source the
result is an empty set of the same shape.

Columns:  1 obj_schema  2 table_name  3 index_name
*/

SET @has_vis := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'STATISTICS'
    AND COLUMN_NAME  = 'IS_VISIBLE'
);

SET @sql := IF(@has_vis > 0,
  'SELECT DISTINCT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME
     FROM INFORMATION_SCHEMA.STATISTICS
    WHERE IS_VISIBLE = ''NO''
      AND TABLE_SCHEMA NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME',
  'SELECT NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

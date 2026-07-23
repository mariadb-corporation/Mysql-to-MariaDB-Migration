-- gis_srid_usage.sql
-- Column-level SRID (INFORMATION_SCHEMA.COLUMNS.SRS_ID) is MySQL 8.0+. The
-- column does not exist before 8.0, so a bare reference throws ERROR 1054 on
-- 5.7 and aborts the precheck. Probe for the column first; run the real query
-- only if present, otherwise return an empty four-column result so the TSV
-- maps 1:1 and assess continues.
SET @has_srs := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'COLUMNS'
    AND COLUMN_NAME  = 'SRS_ID'
);

SET @sql := IF(@has_srs > 0,
  'SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, SRS_ID
     FROM INFORMATION_SCHEMA.COLUMNS
    WHERE DATA_TYPE IN (''geometry'', ''point'', ''linestring'', ''polygon'')
      AND SRS_ID IS NOT NULL
      AND TABLE_SCHEMA NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')',
  'SELECT NULL, NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

/*
schema_default_encryption.sql

Databases created with DEFAULT ENCRYPTION='Y' (8.0.16+). MariaDB drops the
clause silently: the database loads, and every table created in it afterwards
is unencrypted. Nothing errors and nothing in the target records that the
source intended otherwise, which makes this a compliance regression nobody
would find by reading a warning list -- hence LOAD_FAIL rather than sign-off.

SCHEMATA_EXTENSIONS arrived in 8.0.21, so the view is probed first.

Columns:  1 obj_schema  2 options
*/

SET @has_ext := (
  SELECT COUNT(*) FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = 'information_schema' AND TABLE_NAME = 'SCHEMATA_EXTENSIONS'
);

SET @sql := IF(@has_ext > 0,
  'SELECT SCHEMA_NAME, OPTIONS
     FROM information_schema.SCHEMATA_EXTENSIONS
    WHERE OPTIONS LIKE ''%ENCRYPTION=''''Y''''%''
      AND SCHEMA_NAME NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    ORDER BY 1',
  'SELECT NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

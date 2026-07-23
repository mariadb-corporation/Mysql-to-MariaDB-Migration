-- mysql_roles.sql
-- Roles (mysql.role_edges) are MySQL 8.0+. The table does not exist before 8.0,
-- so a bare reference throws ERROR 1146 on 5.7 and aborts the precheck. Probe
-- for the table first; run the real query only if present, otherwise return an
-- empty four-column result so the TSV maps 1:1 and assess continues.
SET @has_roles := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = 'mysql'
    AND TABLE_NAME   = 'role_edges'
);

SET @sql := IF(@has_roles > 0,
  'SELECT FROM_USER, FROM_HOST, TO_USER, TO_HOST
     FROM mysql.role_edges
    ORDER BY TO_USER, FROM_USER',
  'SELECT NULL, NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

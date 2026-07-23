-- partial_revokes.sql
-- Partial revokes (mysql.user.User_attributes) are MySQL 8.0+. The column does
-- not exist before 8.0, so a bare reference throws ERROR 1054 on 5.7 and aborts
-- the precheck. Probe for the column first; run the real query only if present,
-- otherwise return an empty result so the TSV maps 1:1 and assess continues.
SET @has_ua := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = 'mysql'
    AND TABLE_NAME   = 'user'
    AND COLUMN_NAME  = 'User_attributes'
);

SET @sql := IF(@has_ua > 0,
  'SELECT User, Host FROM mysql.user WHERE User_attributes LIKE ''%"partial_revokes":true%''',
  'SELECT NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

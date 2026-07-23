-- resource_groups.sql
-- Resource groups (INFORMATION_SCHEMA.RESOURCE_GROUPS) are MySQL 8.0+. The
-- table does not exist before 8.0, so a bare reference throws ERROR 1109 on
-- 5.7 and aborts the precheck. Probe for the table first; run the real query
-- only if present, otherwise return an empty result so the TSV maps 1:1 and
-- assess continues.
--
-- Note: 8.0 always ships two built-in groups (USR_default, SYS_default). They
-- are excluded so the check flags only user-defined resource groups, which is
-- the actual migration concern (MariaDB has no resource-group equivalent).
SET @has_rg := (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = 'information_schema'
    AND TABLE_NAME   = 'RESOURCE_GROUPS'
);

SET @sql := IF(@has_rg > 0,
  'SELECT RESOURCE_GROUP_NAME, RESOURCE_GROUP_TYPE, RESOURCE_GROUP_ENABLED
     FROM INFORMATION_SCHEMA.RESOURCE_GROUPS
    WHERE RESOURCE_GROUP_NAME NOT IN (''USR_default'',''SYS_default'')',
  'SELECT NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

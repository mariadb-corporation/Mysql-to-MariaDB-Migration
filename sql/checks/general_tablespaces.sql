/*
general_tablespaces.sql

Tables in InnoDB general tablespaces. MariaDB has no CREATE TABLESPACE, but
this is a silent change rather than a load failure: the Migrator dumps with
--no-tablespaces so no CREATE TABLESPACE is emitted, and the table DDL keeps
its /*!50100 TABLESPACE ts1 *''/ comment, which MariaDB accepts while storing
the table file-per-table. Any storage placement built on the tablespace is
gone without an error. (MIGTK-46, MIGTK-54.)

INNODB_TABLESPACES is the 8.0 name; 5.7 calls it INNODB_SYS_TABLESPACES, so
the view is probed first. Reading it needs PROCESS: without that privilege it
returns empty rather than failing, so checks.py warns separately when the
view is unreadable -- otherwise this check would report clean on a source
that has tablespaces.

INNODB_TABLES.NAME is 'db/table', and partitioned tables appear once per
partition as 'db/table#p#p0', so the schema is split off and the partition
suffix trimmed.

Columns:  1 obj_schema  2 table_name  3 tablespace_name
*/

SET @has_ts := (
  SELECT COUNT(*) FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = 'information_schema' AND TABLE_NAME = 'INNODB_TABLESPACES'
);

SET @sql := IF(@has_ts > 0,
  'SELECT DISTINCT
          SUBSTRING_INDEX(t.NAME, ''/'', 1) AS obj_schema,
          SUBSTRING_INDEX(SUBSTRING_INDEX(t.NAME, ''/'', -1), ''#'', 1) AS table_name,
          s.NAME AS tablespace_name
     FROM information_schema.INNODB_TABLESPACES s
     LEFT JOIN information_schema.INNODB_TABLES t ON t.SPACE = s.SPACE
    WHERE s.SPACE_TYPE = ''General''
      AND SUBSTRING_INDEX(t.NAME, ''/'', 1) NOT IN (''mysql'',''information_schema'',''performance_schema'',''sys'')
    ORDER BY 1, 2, 3',
  'SELECT NULL, NULL, NULL FROM DUAL WHERE 1=0'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

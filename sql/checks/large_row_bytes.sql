-- large_row_bytes.sql
--
-- Reports tables whose largest row exceeds @row_limit bytes.
--
-- Driven by two session variables set via --init-command by the caller:
--   @selected_dbs  comma-separated database list (no spaces)
--   @row_limit     byte threshold; rows at or below this are not reported
--
-- Only tables holding at least one MEDIUM/LONG text or blob column (or JSON)
-- are scanned. All other column types are bounded well below the 16M floor,
-- so a table without one cannot produce an oversized INSERT.
--
-- Generated columns are excluded: mariadb-dump emits no values for them.
-- Views are excluded: scanning one would run the underlying query.
--
-- Reported bytes are the raw row total. Actual wire size is larger --
-- escaping expands text, and --hex-blob doubles binary columns. The caller
-- compensates by setting @row_limit below the target's real packet limit.
--
-- Emits no rows when nothing exceeds the threshold, which the caller treats
-- as a pass.

SET SESSION group_concat_max_len = 1024 * 1024;

SELECT GROUP_CONCAT(q SEPARATOR ' UNION ALL ')
  INTO @sql
FROM (
  SELECT CONCAT(
           'SELECT ', QUOTE(c.TABLE_SCHEMA),
           ', ',      QUOTE(c.TABLE_NAME),
           ', MAX(',
           GROUP_CONCAT(
             CONCAT('COALESCE(OCTET_LENGTH(`', c.COLUMN_NAME, '`),0)')
             ORDER BY c.ORDINAL_POSITION
             SEPARATOR ' + '
           ),
           ') AS b FROM `', c.TABLE_SCHEMA, '`.`', c.TABLE_NAME, '`'
         ) AS q
  FROM information_schema.COLUMNS c
  JOIN information_schema.TABLES t
    ON  t.TABLE_SCHEMA = c.TABLE_SCHEMA
    AND t.TABLE_NAME   = c.TABLE_NAME
  WHERE t.TABLE_TYPE = 'BASE TABLE'
    AND FIND_IN_SET(c.TABLE_SCHEMA, @selected_dbs)
    AND c.EXTRA NOT LIKE '%GENERATED%'
    AND EXISTS (
      SELECT 1
      FROM information_schema.COLUMNS c2
      WHERE c2.TABLE_SCHEMA = c.TABLE_SCHEMA
        AND c2.TABLE_NAME   = c.TABLE_NAME
        AND c2.DATA_TYPE IN ('mediumtext','mediumblob','longtext','longblob','json')
    )
  GROUP BY c.TABLE_SCHEMA, c.TABLE_NAME
) x;

-- No candidate tables in the selected databases: emit nothing.
SET @sql = IFNULL(@sql, 'SELECT NULL, NULL, NULL AS b FROM DUAL WHERE 1=0');
SET @sql = CONCAT(
  'SELECT * FROM (', @sql, ') r WHERE b > ', CAST(@row_limit AS CHAR),
  ' ORDER BY b DESC'
);

PREPARE s FROM @sql;
EXECUTE s;
DEALLOCATE PREPARE s;

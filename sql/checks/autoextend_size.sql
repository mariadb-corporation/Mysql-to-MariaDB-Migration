/*
autoextend_size.sql

AUTOEXTEND_SIZE on a table (8.0.23+). MariaDB does not recognise it; the
attribute is dropped and tablespace extension reverts to the MariaDB default.
Loads without error.

Columns:  1 obj_schema  2 table_name  3 create_options
*/

SELECT TABLE_SCHEMA, TABLE_NAME, CREATE_OPTIONS
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')
  AND CREATE_OPTIONS LIKE '%AUTOEXTEND_SIZE%'
ORDER BY 1, 2;

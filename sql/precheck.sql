-- sql/precheck.sql
-- Aggregator (ONE entrypoint). Real SQL lives in sql/checks/*.sql

SOURCE sql/checks/mysql_version.sql;
SOURCE sql/checks/innodb_settings.sql;
SOURCE sql/checks/auth_plugins.sql;
SOURCE sql/checks/json_columns.sql;
SOURCE sql/checks/compression_encryption.sql;
SOURCE sql/checks/engines_summary.sql;
SOURCE sql/checks/schema_sizes.sql;

SOURCE sql/checks/schema_charsets.sql;
SOURCE sql/checks/mysql8_collations.sql;
SOURCE sql/checks/mysql8_column_collations.sql;
SOURCE sql/checks/sql_mode.sql;
SOURCE sql/checks/definers_inventory.sql;
SOURCE sql/checks/partitioned_tables.sql;
SOURCE sql/checks/active_plugins.sql;

SOURCE sql/checks/mysql_roles.sql;
SOURCE sql/checks/generated_columns.sql;
SOURCE sql/checks/server_defaults.sql;
SOURCE sql/checks/view_routine_bodies.sql;

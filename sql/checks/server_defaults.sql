/*
Key server-level defaults that differ between MySQL 8.0 and MariaDB 11.x.
Mismatches in these settings can cause subtle post-migration issues
with case sensitivity, timestamp behavior, charset handling, and
event scheduling.
*/
SELECT
  @@character_set_server       AS character_set_server,
  @@collation_server           AS collation_server,
  @@lower_case_table_names     AS lower_case_table_names,
  @@explicit_defaults_for_timestamp AS explicit_defaults_for_timestamp,
  @@event_scheduler            AS event_scheduler,
  @@innodb_default_row_format  AS innodb_default_row_format;

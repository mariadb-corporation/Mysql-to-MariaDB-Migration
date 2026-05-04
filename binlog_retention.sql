-- Reports source binlog retention.
--
-- For binlog-mode migrations, retention must exceed the time required for
-- snapshot + replication catch-up. If the source purges binlogs the replica
-- still needs, replication breaks and the migration is lost.
--
-- Output (tab-separated, no header due to --skip-column-names):
--   col 1: binlog_expire_logs_seconds  (modern variable, 8.0+; takes precedence)
--   col 2: expire_logs_days            (legacy variable, used when col 1 = 0)
--   col 3: effective_retention_seconds (0 = unlimited / disabled)

SELECT
  @@binlog_expire_logs_seconds AS binlog_expire_logs_seconds,
  @@expire_logs_days AS expire_logs_days,
  CASE
    WHEN @@binlog_expire_logs_seconds > 0 THEN @@binlog_expire_logs_seconds
    WHEN @@expire_logs_days > 0 THEN @@expire_logs_days * 86400
    ELSE 0
  END AS effective_retention_seconds;

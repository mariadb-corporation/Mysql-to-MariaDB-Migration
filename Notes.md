# Detailed Notes of all modes of migration

One-step mode --> The one-step mode is for straightforward logical migration where schema and data are moved together in a single pipeline.

1. Check everything first
    * Checks connectivity, admin creds, source DB presence, target readiness
    * Script: scripts/00_preflight_one_step.sh
    * Also runs compatibility precheck: scripts/00_precheck.sh
2. Optional application-user migration
    * Migrates app users from source to target (if enabled)
    * Script: scripts/09_migrate_app_users.sh
3. Migrate schema + data in one pass
    * Takes logical dump from source and restores directly to target
    * Script: scripts/10_one_step_migration.sh
4. Validate result
    * Runs post-migration validation checks
    * Script: scripts/07_validate.sh

Two-step mode --> The two-step mode is for larger datasets where separating schema and data improves control and performance.

1. Check everything first
    * Checks connectivity, admin creds, tool readiness (sqldata)
    * Script: scripts/00_preflight_two_step.sh
    * Also runs compatibility precheck: scripts/00_precheck.sh
2. Export/import schema only
    * Migrates only schema objects first (no table data)
    * Script: scripts/11_two_step_schema.sh
3. Transfer table data in parallel
    * Uses SQLines Data for bulk parallel data movement
    * Script: scripts/12_two_step_sqldata.sh
4. Finalize deferred objects
    * Applies constraints/indexes/routines after data load
    * Script: scripts/13_two_step_finalize.sh
5. Validate result
    * Runs post-migration validation checks
    * Script: scripts/07_validate.sh


Binlog mode --> Use when target is already MariaDB-ready.

1. Check everything first
    * Checks connectivity, privileges, binlog readiness
    * Script: scripts/00_preflight_binlog.sh
    * Also runs compatibility precheck: scripts/00_precheck.sh
2. Take a starting copy
    * Snapshot dump from source and restore to target
    * Script: scripts/14_binlog_seed.sh
3. Capture “where to continue” point
    * Extracts binlog file + position during dump
    * Script: scripts/14_binlog_seed.sh (writes BINLOG_COORD_FILE)
4. Start live sync
    * Configures CHANGE MASTER and starts replication
    * Script: scripts/15_binlog_start_replication.sh
5. Verify sync health
    * Checks IO/SQL thread status and lag/error
    * Script: scripts/16_binlog_verify.sh


Replace-slave mode --> Use when you want to convert an existing MySQL slave machine into a MariaDB slave.

1. Confirm target is a MySQL slave host
    * Verifies slave/replica state + readiness
    * Script: scripts/20_preflight_replace_slave.sh
    * Also runs compatibility precheck: scripts/00_precheck.sh
2. Backup old slave host
    * Backs up existing MySQL slave host data/config
    * Script: scripts/21_replace_slave_backup.sh
3. Install MariaDB on that host
    * Installs MariaDB packages on target host
    * Script: scripts/23_install_mariadb.sh
4. Switch from MySQL service to MariaDB
    * Stops MySQL, starts/configures MariaDB
    * Script: scripts/22_replace_slave_switch_engine.sh
5. Load initial data
    * Seeds MariaDB from source snapshot
    * Script: scripts/14_binlog_seed.sh
6. Start replication from source
    * Configures and starts MySQL->MariaDB replication
    * Script: scripts/15_binlog_start_replication.sh
7. Verify replication
    * Validates replication health and lag
    * Script: scripts/16_binlog_verify.sh
8. Optional cleanup
    * Removes old MySQL data if enabled
    * Script: scripts/24_replace_slave_cleanup.sh
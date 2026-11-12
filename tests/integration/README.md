# Sandbox harness

`sandboxes.sh` deploys the local MySQL and MariaDB instances used as the version
matrix for manual migration testing. It provisions servers only — it runs no
tests and makes no assertions.

    ./sandboxes.sh            deploy if missing, start if stopped
    ./sandboxes.sh stop       stop all instances
    ./sandboxes.sh status     show version per port
    ./sandboxes.sh delete     stop and remove all sandbox directories
    ./sandboxes.sh redeploy   delete and freshly deploy all instances

`delete` and `redeploy` destroy data on every port, not just one.

## The matrix

| Port | Server        |
|------|---------------|
| 3327 | MySQL 5.7     |
| 3328 | MySQL 8.0     |
| 3329 | MySQL 8.4     |
| 3311 | MariaDB 11.8  |
| 3312 | MariaDB 12.3  |
| 3313 | MariaDB 13.1  |

Ports are the instance identity — they name the sandbox directory, the pid
file and the connection target, so they stay fixed. Edit `INSTANCES` at the
top of the script to add or remove a server.

## Prerequisites

`mariadb-shell` must be on `PATH`; the script drives its sandbox feature for
both vendors.

Server tarballs are not in this repo. Unpack them into `~/servers` (override
with `SERVERS=`). Expected naming:

    ~/servers/mariadb-11.8.9-linux-glibc2.34-arm-64bit-sandbox
    ~/servers/mysql-8.0.46-linux-glibc2.28-aarch64
    ~/servers/mysql-8.0.46-linux-glibc2.28-aarch64
    ~/servers/mysql-8.4.13-linux-glibc2.28-aarch64

MariaDB directories must match the version in `INSTANCES` exactly. MySQL is
matched by series, so any patch release in `8.0.*` is picked up automatically
and the newest wins.

Sandbox datadirs are created under `~/.mariadb-shell/sandboxes` on first `up`.

## Defaults

ARM is assumed. On x86, pass `ARCH=x86`, which switches both the MariaDB
directory suffix and the MySQL architecture string.

Root password is `abc`, SSL is off, and `innodb_flush_log_at_trx_commit` and
`sync_binlog` are both 0. This is a throwaway local test bed — none of it is a
deployment pattern.

## What the instances are configured for

Binary logging is on everywhere in ROW format, with GTID forced on for MySQL,
so replication-based migration paths can be exercised without reconfiguring.
A keyring is enabled on the MySQL side for encrypted-tablespace testing —
MySQL 8.0 loads `keyring_file.so`, while 8.4 uses a component manifest, which
is why `deploy_instance` branches on version.

## MySQL 5.7

Port 3327 is reserved for 5.7 but will report `SKIP` on ARM: Oracle never
published an aarch64 build, and the `mysql:5.7` image is amd64-only. Running it
locally requires either an x86 host (`ARCH=x86 SERVERS=...` with a 5.7 tarball)
or qemu emulation. The glibc2.12 tarball links against OpenSSL 1.0, so check
`ldd bin/mysqld` on RHEL 9 before assuming it will start.

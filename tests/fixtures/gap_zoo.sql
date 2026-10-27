-- ===========================================================================
-- gap_zoo.sql -- one object per known-risk feature, for manual migration tests
-- ===========================================================================
--
-- Load into MySQL 8.0 or 8.4, run `mariadb-migrator --assess`, and compare the
-- findings against the expected class noted above each object. Then migrate
-- and confirm the predicted outcome actually happens.
--
-- Each comment records:  CLASS (error) -- what should happen on MariaDB
-- CLASS is LOAD_FAIL, SIGN_OFF or INFO, matching the "class" field in
-- report.json and the section the object should appear under in
-- load_failure_report.txt.
--
-- Three objects need setup this file cannot do on its own -- see README.md in
-- this directory: the two encryption cases need a keyring, the general
-- tablespace needs CREATE TABLESPACE first, and the violating CHECK row must
-- be inserted while the constraint is still unenforced.
--
-- Run with --force so one failure does not stop the rest:
--   mysql --force < gap_zoo.sql
-- ===========================================================================

DROP DATABASE IF EXISTS gap_zoo;
CREATE DATABASE gap_zoo;
USE gap_zoo;

-- SIGN_OFF -- becomes visible; SELECT * returns it
CREATE TABLE z_invisible_col (
  id   INT PRIMARY KEY,
  shown VARCHAR(20),
  hidden VARCHAR(20) INVISIBLE
);

-- SIGN_OFF -- becomes visible, and it is NOT NULL, so inserts without a
-- column list now need a value for it
CREATE TABLE z_invisible_col_notnull (
  id   INT PRIMARY KEY,
  hidden VARCHAR(20) NOT NULL DEFAULT 'x' INVISIBLE
);

-- SIGN_OFF -- the generated primary key becomes visible
CREATE TABLE z_invisible_generated_pk (
  payload VARCHAR(20)
);
-- requires sql_generate_invisible_primary_key=ON; see README

-- SIGN_OFF -- index becomes visible and the optimizer starts using it
CREATE TABLE z_invisible_index (
  id INT PRIMARY KEY,
  c  VARCHAR(20),
  INDEX idx_hidden (c) INVISIBLE
);

-- LOAD_FAIL (4025) -- enforcement switches on and the existing row is rejected
CREATE TABLE z_check_not_enforced_violating (
  id  INT PRIMARY KEY,
  qty INT,
  CONSTRAINT chk_qty_positive CHECK (qty > 0) NOT ENFORCED
);
INSERT INTO z_check_not_enforced_violating VALUES (1, -5);

-- SIGN_OFF -- loads today, but enforcement switches on for future writes
CREATE TABLE z_check_not_enforced_clean (
  id    INT PRIMARY KEY,
  price INT,
  CONSTRAINT chk_price_nonneg CHECK (price >= 0) NOT ENFORCED
);
INSERT INTO z_check_not_enforced_clean VALUES (1, 100);

-- SIGN_OFF -- the SRID restriction on the column is dropped
CREATE TABLE z_srid_column (
  id INT PRIMARY KEY,
  g  POINT SRID 4326 NOT NULL,
  SPATIAL INDEX(g)
);

-- INFO -- untagged _0900_ collations are aliases on MariaDB 11.4.5+.
-- Confirmed on 11.8.9: all four load and keep their names.
CREATE TABLE z_collation_0900_untagged (
  c_ai_ci VARCHAR(10) COLLATE utf8mb4_0900_ai_ci,
  c_as_ci VARCHAR(10) COLLATE utf8mb4_0900_as_ci,
  c_as_cs VARCHAR(10) COLLATE utf8mb4_0900_as_cs,
  c_bin   VARCHAR(10) COLLATE utf8mb4_0900_bin
);

-- LOAD_FAIL (1273) -- not in MariaDB's mysql_0900_mapping. ja confirmed on
-- 11.8.9; ru and zh are listed unsupported in strings/ctype-uca0900.c.
CREATE TABLE z_collation_0900_ja (c VARCHAR(10) COLLATE utf8mb4_ja_0900_as_cs);
CREATE TABLE z_collation_0900_ru (c VARCHAR(10) COLLATE utf8mb4_ru_0900_ai_ci);
CREATE TABLE z_collation_0900_zh (c VARCHAR(10) COLLATE utf8mb4_zh_0900_as_cs);

-- LOAD_FAIL (1273) -- 8.0.30 locale, absent from the alias table entirely.
-- nb was tested; bs, bg, gl, mn, nn, sr_latn and yo are expected to match.
CREATE TABLE z_collation_0900_nb (c VARCHAR(10) COLLATE utf8mb4_nb_0900_ai_ci);

-- LOAD_FAIL (1273) -- collation on a routine rather than a column. Fails even
-- though no column uses it, because the dump sets it before CREATE PROCEDURE.
-- See README: this needs a session collation change, not plain DDL.

-- LOAD_FAIL (1115) -- MariaDB has no gb18030
CREATE TABLE z_charset_gb18030 (c VARCHAR(10) CHARACTER SET gb18030);

-- LOAD_FAIL (1911) -- MariaDB uses ENCRYPTED=YES, not ENCRYPTION='Y'
-- Needs a keyring on the MySQL side; see README.
CREATE TABLE z_table_encrypted (id INT PRIMARY KEY) ENCRYPTION='Y';

-- SIGN_OFF -- clause survives in the DDL and MariaDB stores file-per-table
-- Needs CREATE TABLESPACE first; see README.
CREATE TABLE z_tablespace_general (id INT PRIMARY KEY) TABLESPACE zoo_ts1;

-- SIGN_OFF -- dropped; extension reverts to the MariaDB default
CREATE TABLE z_autoextend (id INT PRIMARY KEY) AUTOEXTEND_SIZE=4194304;

-- LOAD_FAIL (1064) on 11.8, works on 13.1 -- MDEV-13594
DELIMITER //
CREATE PROCEDURE z_json_arrow()
BEGIN
  SELECT CAST('{"a":1}' AS JSON) ->> '$.a';
END //
DELIMITER ;

-- LOAD_FAIL (1064) -- LATERAL in a view
CREATE TABLE z_lateral_base (id INT PRIMARY KEY, n INT);
CREATE VIEW z_lateral_view AS
  SELECT b.id, d.n FROM z_lateral_base b,
  LATERAL (SELECT n FROM z_lateral_base x WHERE x.id = b.id) d;

-- LOAD_FAIL (1064) -- LATERAL in a procedure
DELIMITER //
CREATE PROCEDURE z_lateral_proc()
BEGIN
  SELECT b.id FROM z_lateral_base b,
  LATERAL (SELECT 1 AS one) d;
END //
DELIMITER ;

-- LOAD_FAIL (1305) -- MySQL-only function in a routine
DELIMITER //
CREATE FUNCTION z_uuid_fn() RETURNS BINARY(16) DETERMINISTIC
BEGIN
  RETURN UUID_TO_BIN('6ccd780c-baba-1026-9564-5b8c656024db');
END //
DELIMITER ;

-- LOAD_FAIL (1064) -- MEMBER OF has no MariaDB equivalent
CREATE VIEW z_member_of AS
  SELECT 1 AS hit WHERE 1 MEMBER OF (CAST('[1,2,3]' AS JSON));

SELECT 'gap_zoo loaded. See README.md for the objects this file cannot create alone.' AS note;

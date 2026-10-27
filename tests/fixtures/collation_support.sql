-- Which _0900_ collations does a given MariaDB build actually carry?
--
-- The migrator answers this at assess time by comparing against the target's
-- own information_schema.COLLATIONS, so nothing here is needed for a
-- migration. It is kept because it is the quickest way to check a new target
-- by hand, and because it is what established the split below.
--
-- Run against the TARGET:
--   mariadb --force -vvv < collation_support.sql 2>&1 | tee collations-<version>.txt
--
-- Result on 11.8.9 (2026-10): the four untagged variants load and keep their
-- names; utf8mb4_ja_0900_as_cs fails with 1273; most language-tagged
-- collations (cs, da, de_pb, eo, es, et, hr, hu, is, la, lt, lv, pl, ro, sk,
-- sl, sv, tr, vi) are present. ja, ru and zh are marked unsupported in
-- MariaDB's strings/ctype-uca0900.c; the locales MySQL added in 8.0.30 (bs,
-- bg, gl, mn, nb, nn, sr_latn, yo) are absent from the mapping entirely.

SELECT COLLATION_NAME, CHARACTER_SET_NAME, IS_DEFAULT
FROM information_schema.COLLATIONS
WHERE COLLATION_NAME LIKE '%0900%'
ORDER BY COLLATION_NAME;

DROP DATABASE IF EXISTS c0900;
CREATE DATABASE c0900;
USE c0900;

CREATE TABLE t_ai_ci (c VARCHAR(10) COLLATE utf8mb4_0900_ai_ci);
CREATE TABLE t_as_ci (c VARCHAR(10) COLLATE utf8mb4_0900_as_ci);
CREATE TABLE t_as_cs (c VARCHAR(10) COLLATE utf8mb4_0900_as_cs);
CREATE TABLE t_bin   (c VARCHAR(10) COLLATE utf8mb4_0900_bin);

-- Expected to fail with 1273 on current builds
CREATE TABLE t_ja (c VARCHAR(10) COLLATE utf8mb4_ja_0900_as_cs);
CREATE TABLE t_ru (c VARCHAR(10) COLLATE utf8mb4_ru_0900_ai_ci);
CREATE TABLE t_zh (c VARCHAR(10) COLLATE utf8mb4_zh_0900_as_cs);
CREATE TABLE t_nb (c VARCHAR(10) COLLATE utf8mb4_nb_0900_ai_ci);

CREATE TABLE t_tbl_ai_ci (c VARCHAR(10)) COLLATE=utf8mb4_0900_ai_ci;
CREATE DATABASE c0900_db COLLATE=utf8mb4_0900_ai_ci;

-- Did any surviving collation get substituted underneath?
SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA='c0900' AND COLLATION_NAME IS NOT NULL
ORDER BY TABLE_NAME;

-- Sort behaviour, which is what actually bites. Compare against MySQL 8.
INSERT INTO t_ai_ci VALUES ('a'),('A'),(CONVERT('ä' USING utf8mb4));
SELECT c FROM t_ai_ci ORDER BY c;

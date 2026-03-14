/*
MySQL 8.0 introduced roles (CREATE ROLE, GRANT role TO user).
MariaDB has its own role implementation but role_edges/default_roles
metadata does not transfer automatically. Users with assigned roles
may lose privileges after migration.
*/
SELECT FROM_USER, FROM_HOST, TO_USER, TO_HOST
FROM mysql.role_edges
ORDER BY TO_USER, FROM_USER;

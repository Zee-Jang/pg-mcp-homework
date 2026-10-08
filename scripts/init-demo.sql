-- Run once on a NEW homework-only PostgreSQL instance; never on a business database.
-- These public demo credentials contain no real secrets.
\set ON_ERROR_STOP on
CREATE ROLE homework_reader LOGIN PASSWORD 'homework-reader-demo'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE DATABASE homework_sales;
CREATE DATABASE homework_archive;

\connect homework_sales
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE TABLE orders (id integer PRIMARY KEY, customer text NOT NULL, amount numeric(12,2));
INSERT INTO orders VALUES (1, 'Alice', 120.00), (2, 'Bob', 80.00), (3, 'Carol', 200.00);
CREATE TABLE users (id integer PRIMARY KEY, name text NOT NULL, email text, password text);
INSERT INTO users VALUES (1, 'Alice', 'alice@example.invalid', 'demo-secret-never-return');
CREATE TABLE audit_log (id integer PRIMARY KEY, detail text);
INSERT INTO audit_log VALUES (1, 'demo restricted record');
GRANT CONNECT ON DATABASE homework_sales TO homework_reader;
GRANT USAGE ON SCHEMA public TO homework_reader;
GRANT SELECT ON orders TO homework_reader;
GRANT SELECT (id, name, email) ON users TO homework_reader;
ALTER ROLE homework_reader IN DATABASE homework_sales SET default_transaction_read_only = on;

\connect homework_archive
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE TABLE orders (id integer PRIMARY KEY, customer text NOT NULL, amount numeric(12,2));
INSERT INTO orders VALUES (1, 'Archived Alice', 900.00), (2, 'Archived Bob', 100.00);
CREATE TABLE users (id integer PRIMARY KEY, name text NOT NULL, email text, password text);
INSERT INTO users VALUES (1, 'Archived Alice', 'archive@example.invalid', 'demo-secret-never-return');
CREATE TABLE audit_log (id integer PRIMARY KEY, detail text);
INSERT INTO audit_log VALUES (1, 'demo restricted archive');
GRANT CONNECT ON DATABASE homework_archive TO homework_reader;
GRANT USAGE ON SCHEMA public TO homework_reader;
GRANT SELECT ON orders TO homework_reader;
GRANT SELECT (id, name, email) ON users TO homework_reader;
ALTER ROLE homework_reader IN DATABASE homework_archive SET default_transaction_read_only = on;

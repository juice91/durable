#!/bin/bash
# First start of the Compose quick start's Postgres (compose.yaml): the two databases, their roles and pgvector.
# The passwords are the files the `init` service generated; they reach psql on stdin, never on a command line.
set -euo pipefail
secrets=${EAP_SECRETS_DIR:-/run/eap-secrets}
app_password=$(<"${secrets}/app_db_password")
dbos_password=$(<"${secrets}/dbos_db_password")
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
\\set app_password '${app_password}'
\\set dbos_password '${dbos_password}'
SELECT format('CREATE ROLE eap_app WITH LOGIN PASSWORD %L', :'app_password')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'eap_app') \\gexec
SELECT format('CREATE ROLE eap_dbos WITH LOGIN PASSWORD %L', :'dbos_password')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'eap_dbos') \\gexec
SELECT 'CREATE DATABASE eap_app OWNER eap_app'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'eap_app') \\gexec
SELECT 'CREATE DATABASE eap_dbos OWNER eap_dbos'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'eap_dbos') \\gexec
REVOKE CONNECT ON DATABASE eap_app FROM PUBLIC;
REVOKE CONNECT ON DATABASE eap_dbos FROM PUBLIC;
GRANT CONNECT ON DATABASE eap_app TO eap_app;
GRANT CONNECT ON DATABASE eap_dbos TO eap_dbos;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname eap_app -c 'CREATE EXTENSION IF NOT EXISTS vector;'

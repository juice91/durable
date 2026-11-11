#!/bin/bash
set -e

echo "Setting up databases and roles..."

# Create roles if they don't exist
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<EOF
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eap_app') THEN
        CREATE ROLE eap_app WITH LOGIN PASSWORD 'eap_app_password';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eap_dbos') THEN
        CREATE ROLE eap_dbos WITH LOGIN PASSWORD 'eap_dbos_password';
    END IF;
END \$\$;
EOF

# Create databases with correct ownership (eap_app owns eap_app, eap_dbos owns eap_dbos)
echo "Creating eap_app database owned by eap_app..."
createdb -U "$POSTGRES_USER" -O eap_app eap_app

echo "Creating eap_dbos database owned by eap_dbos..."
createdb -U "$POSTGRES_USER" -O eap_dbos eap_dbos

# Revoke CONNECT from PUBLIC on both databases
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<EOF
REVOKE CONNECT ON DATABASE eap_app FROM PUBLIC;
REVOKE CONNECT ON DATABASE eap_dbos FROM PUBLIC;
GRANT CONNECT ON DATABASE eap_app TO eap_app;
GRANT CONNECT ON DATABASE eap_dbos TO eap_dbos;
GRANT CONNECT ON DATABASE eap_app TO $POSTGRES_USER;
GRANT CONNECT ON DATABASE eap_dbos TO $POSTGRES_USER;
EOF

echo "Database setup complete!"

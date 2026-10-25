#!/bin/bash
# Runs once, on the first start of an empty data volume.
# Creates the two application roles; ledger schemas and their grants are created by store.py (gate3_owner).
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     -v app_pw="$TWIN_APP_PASSWORD" -v reader_pw="$TWIN_READER_PASSWORD" <<'SQL'
CREATE ROLE twin_app    LOGIN PASSWORD :'app_pw';
CREATE ROLE twin_reader LOGIN PASSWORD :'reader_pw';
-- Nobody but the owner may create objects in the public schema
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON DATABASE gate3 FROM PUBLIC;
GRANT CONNECT ON DATABASE gate3 TO twin_app, twin_reader;
SQL

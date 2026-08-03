#!/bin/bash
# Runs once, on first cluster init — before any application migration.
# Creates the n8n role and its own schema so a noisy or compromised n8n cannot
# reach the application tables (System-Design.md §4, Deployment-Plan Stage 1.4).
# Table-level grants live in migrations/002_n8n_grants.sql, because the tables
# they reference do not exist yet at this point.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE "$N8N_DB_USER" LOGIN PASSWORD '$N8N_DB_PASSWORD';
    CREATE SCHEMA n8n AUTHORIZATION "$N8N_DB_USER";

    -- n8n's own migrations insist on CREATE at the database level even though
    -- its schema already exists (verified against 1.45: "permission denied for
    -- database" at init without this).
    GRANT CONNECT, CREATE ON DATABASE "$POSTGRES_DB" TO "$N8N_DB_USER";

    REVOKE ALL ON SCHEMA public FROM "$N8N_DB_USER";
    GRANT USAGE ON SCHEMA public TO "$N8N_DB_USER";
EOSQL

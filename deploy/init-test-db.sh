#!/bin/bash
# Runs once at first container init (docker-entrypoint-initdb.d) to provision a
# second, isolated database for the test suite (GC_TEST_ADMIN_DATABASE_URL /
# GC_TEST_DATABASE_URL) alongside the dev database.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE gc_intel_test OWNER $POSTGRES_USER;
EOSQL

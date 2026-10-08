#!/bin/sh
# Cria o usuário somente leitura usado pela API. A senha vem de TSE_LEITURA_PASSWORD (.env).
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v senha="$TSE_LEITURA_PASSWORD" -v banco="$POSTGRES_DB" <<'EOSQL'
CREATE ROLE tse_leitura LOGIN PASSWORD :'senha';
REVOKE ALL ON DATABASE :"banco" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"banco" TO tse_leitura;
GRANT USAGE ON SCHEMA tse TO tse_leitura;
GRANT SELECT ON ALL TABLES IN SCHEMA tse TO tse_leitura;
ALTER DEFAULT PRIVILEGES IN SCHEMA tse GRANT SELECT ON TABLES TO tse_leitura;
EOSQL

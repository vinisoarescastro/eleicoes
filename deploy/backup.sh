#!/usr/bin/env bash
# Backup do banco (formato custom do pg_dump, compactado). Mantém os últimos MANTER_DIAS dias.
# Restauração: docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < arquivo.dump
set -euo pipefail
DIR="${DIR:-/opt/eleicoes}"
DESTINO="${DESTINO:-/var/backups/eleicoes}"
MANTER_DIAS="${MANTER_DIAS:-3}"

mkdir -p "${DESTINO}"
chmod 700 "${DESTINO}"
cd "${DIR}"

arquivo="${DESTINO}/tse_$(date +%Y%m%d_%H%M).dump"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "${arquivo}.parcial"
mv "${arquivo}.parcial" "${arquivo}"
echo "$(date '+%F %T') backup criado: ${arquivo} ($(du -h "${arquivo}" | cut -f1))"

# Remove apenas backups deste projeto mais antigos que MANTER_DIAS
find "${DESTINO}" -maxdepth 1 -name 'tse_*.dump' -mtime +"${MANTER_DIAS}" -print -delete

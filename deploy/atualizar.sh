#!/usr/bin/env bash
# Atualiza a aplicação na VPS com a versão do GitHub: código, imagens e migrações do banco.
# Uso (como root, na VPS):  bash /opt/eleicoes/deploy/atualizar.sh
# Os dados não são apagados; as migrações novas (etl/migrations) são aplicadas pelo ETL ao iniciar.
set -euo pipefail
DIR="${DIR:-/opt/eleicoes}"
cd "${DIR}"

echo "==> Versão atual: $(git rev-parse --short HEAD)"
git pull --ff-only
echo "==> Nova versão:  $(git rev-parse --short HEAD)"

docker compose --profile etl build
docker compose up -d db api

# Aplica migrações pendentes (execução rápida: só baixa arquivos que mudaram)
flock -w 600 /run/eleicoes-etl.lock docker compose run --rm etl --arquivos CANDIDATOS

curl -fsS http://127.0.0.1:8010/health && echo " <- API ok"

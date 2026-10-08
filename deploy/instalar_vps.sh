#!/usr/bin/env bash
# Instala e publica o mapa das Eleições 2026 em uma VPS Ubuntu (22.04/24.04) ou Debian (12).
#
# Uso (como root, na VPS):
#   DOMINIO=mapa.seudominio.com.br bash instalar_vps.sh
#
# Variáveis opcionais:
#   DIR=/opt/eleicoes                  pasta de instalação
#   REPO=https://github.com/...git     repositório (use URL SSH + deploy key se o repositório for privado)
#   CONFIGURAR_FIREWALL=sim            libera apenas SSH, 80 e 443 (ufw)
#   CARGA_INICIAL=sim                  dispara a carga completa do TSE em segundo plano (~1 h)
#
# O script é idempotente: pode ser executado de novo para corrigir/atualizar a instalação.
# Um .env existente NUNCA é sobrescrito.
set -euo pipefail

DOMINIO="${DOMINIO:?Defina DOMINIO, ex.: DOMINIO=mapa.seudominio.com.br}"
DIR="${DIR:-/opt/eleicoes}"
REPO="${REPO:-https://github.com/vinisoarescastro/eleicoes.git}"
CONFIGURAR_FIREWALL="${CONFIGURAR_FIREWALL:-sim}"
CARGA_INICIAL="${CARGA_INICIAL:-sim}"
LOG_ETL=/var/log/eleicoes-etl.log

[ "$(id -u)" -eq 0 ] || { echo "Execute como root (sudo)."; exit 1; }
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo "Sistema não suportado: $ID (use Ubuntu ou Debian)."; exit 1 ;; esac
etapa() { echo; echo "==> $*"; }

etapa "Pacotes base"
apt-get update -y
apt-get install -y ca-certificates curl gnupg git openssl cron

etapa "Docker (repositório oficial)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

etapa "Caddy (proxy reverso com HTTPS automático)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

etapa "Código da aplicação em ${DIR}"
if [ -d "${DIR}/.git" ]; then
  git -C "${DIR}" pull --ff-only
else
  git clone "${REPO}" "${DIR}"
fi
cd "${DIR}"

etapa "Arquivo .env (senhas geradas aleatoriamente; não é sobrescrito se já existir)"
if [ ! -f .env ]; then
  ram_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  shared=$(( ram_mb / 4 )); [ "${shared}" -lt 128 ] && shared=128
  maint=$(( ram_mb / 8 )); [ "${maint}" -gt 1024 ] && maint=1024; [ "${maint}" -lt 64 ] && maint=64
  sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" \
      -e "s/^TSE_LEITURA_PASSWORD=.*/TSE_LEITURA_PASSWORD=$(openssl rand -hex 24)/" \
      -e "s/^ETL_ARQUIVOS=.*/ETL_ARQUIVOS=ALL/" \
      -e "s/^PG_SHARED_BUFFERS=.*/PG_SHARED_BUFFERS=${shared}MB/" \
      -e "s/^PG_MAINTENANCE_WORK_MEM=.*/PG_MAINTENANCE_WORK_MEM=${maint}MB/" \
      .env.example > .env
  chmod 600 .env
  echo "Criado .env (RAM ${ram_mb} MB -> shared_buffers ${shared}MB). Guarde uma cópia em local seguro."
else
  echo ".env já existe: mantido."
fi

etapa "Banco e API (Docker Compose)"
docker compose up -d --build db api
docker compose --profile etl build etl

etapa "Proxy HTTPS (Caddy) para ${DOMINIO}"
[ -f /etc/caddy/Caddyfile ] && cp -n /etc/caddy/Caddyfile /etc/caddy/Caddyfile.original || true
sed "s/__DOMINIO__/${DOMINIO}/g" deploy/Caddyfile.modelo > /etc/caddy/Caddyfile
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy
caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy

etapa "Rotinas: atualização dos dados (de hora em hora) e backup diário"
cat > /etc/cron.d/eleicoes <<CRON
# Atualização dos dados do TSE (só baixa o que mudou; flock evita execuções sobrepostas)
15 * * * * root cd ${DIR} && flock -n /run/eleicoes-etl.lock docker compose run --rm etl --arquivos ALL >> ${LOG_ETL} 2>&1
# Backup do banco (mantém os últimos dias; ver deploy/backup.sh)
30 3 * * * root DIR=${DIR} bash ${DIR}/deploy/backup.sh >> /var/log/eleicoes-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/eleicoes
cat > /etc/logrotate.d/eleicoes <<ROT
${LOG_ETL} /var/log/eleicoes-backup.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
}
ROT

if [ "${CONFIGURAR_FIREWALL}" = "sim" ]; then
  etapa "Firewall (ufw): SSH, HTTP e HTTPS"
  apt-get install -y ufw
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

if [ "${CARGA_INICIAL}" = "sim" ]; then
  etapa "Carga inicial dos dados do TSE em segundo plano (~1 h). Acompanhe: tail -f ${LOG_ETL}"
  nohup flock -n /run/eleicoes-etl.lock docker compose run --rm etl --arquivos ALL >> "${LOG_ETL}" 2>&1 &
fi

etapa "Concluído"
echo "Mapa: https://${DOMINIO}  (o certificado HTTPS é emitido na primeira visita; o DNS precisa apontar para esta VPS)"
echo "Saúde da API: curl -s http://127.0.0.1:8010/health"

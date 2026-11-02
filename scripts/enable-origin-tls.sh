#!/usr/bin/env bash
# 启用源站 HTTPS(为 Cloudflare SSL 模式 Full 做准备),可重复执行。
#
# 配置来源同 deploy.sh:CARAOK_SERVER / CARAOK_DOMAIN / CARAOK_APP_DIR
# (环境变量 > scripts/deploy.conf)。
#
# 做的事:
#   1) 若 secrets/ 下没有证书,生成自签证书(Full 模式不校验证书链,够用;
#      若要 Full(strict),请换成 CF 面板签发的 Origin CA 证书)
#   2) 用 compose.tls.yml 叠加部署(web 额外监听 443)
#   3) 验证 TLS 握手
#   4) 验证 CF 链路仍正常;失败则回滚到 HTTP-only
set -euo pipefail

CONF_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy.conf"
[ -f "$CONF_FILE" ] && source "$CONF_FILE"

SERVER="${CARAOK_SERVER:-}"
DOMAIN="${CARAOK_DOMAIN:-}"
APP_DIR="${CARAOK_APP_DIR:-/opt/caraok}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
r() { ssh "${SSH_OPTS[@]}" "$SERVER" "$@"; }
banner() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

[ -n "$SERVER" ] || { echo "✗ 缺少 CARAOK_SERVER" >&2; exit 1; }
[ -n "$DOMAIN" ] || { echo "✗ 缺少 CARAOK_DOMAIN" >&2; exit 1; }

banner "PHASE 1 证书检查"
r "set -e
cd $APP_DIR
if [ ! -s secrets/origin.pem ] || [ ! -s secrets/origin.key ]; then
  mkdir -p secrets
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout secrets/origin.key -out secrets/origin.pem \
    -subj '/CN=$DOMAIN' -addext 'subjectAltName=DNS:$DOMAIN' >/dev/null 2>&1
  chmod 600 secrets/origin.key
  echo '已生成自签证书(如需 Full strict,请换成 CF Origin CA 证书)'
else
  echo '复用已有证书'
fi
openssl x509 -in secrets/origin.pem -noout -subject -dates | sed 's/^/  /'
chmod 644 secrets/origin.pem"

banner "PHASE 2 上传 TLS 配置并叠加部署"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scp -q "${SSH_OPTS[@]}" "$SCRIPT_DIR/Caddyfile.tls" "$SERVER:$APP_DIR/Caddyfile.tls"
scp -q "${SSH_OPTS[@]}" "$SCRIPT_DIR/../compose.tls.yml" "$SERVER:$APP_DIR/compose.tls.yml"
# web 容器的 Caddyfile 是构建期 COPY 进去的,运行期需用挂载覆盖
r "cd $APP_DIR && cp Caddyfile.tls Caddyfile.active && \
   docker compose -f docker-compose.yml -f compose.proxied.yml -f compose.tls.yml up -d 2>&1 | tail -3"
r "sleep 5 && docker ps --filter name=caraok --format '{{.Names}} {{.Status}}'"

banner "PHASE 3 验证源站 TLS"
if r "curl -sk --max-time 10 --resolve $DOMAIN:8443:172.17.0.1 https://$DOMAIN:8443/healthz | grep -q ok"; then
  echo "✓ 源站 443 TLS 正常"
else
  echo "✗ 源站 TLS 失败,回滚到 HTTP-only" >&2
  r "cd $APP_DIR && docker compose -f docker-compose.yml -f compose.proxied.yml up -d 2>&1 | tail -1"
  exit 1
fi

banner "PHASE 4 验证 CF 链路未被影响"
if r "curl -s --max-time 15 https://$DOMAIN/healthz | grep -q ok"; then
  echo "✓ CF 链路正常(此时仍是 Flexible,回源走 80,Caddy 同时监听 80/443)"
else
  echo "✗ CF 链路异常,回滚" >&2
  r "cd $APP_DIR && docker compose -f docker-compose.yml -f compose.proxied.yml up -d 2>&1 | tail -1"
  exit 1
fi

cat <<EOF

完成。现在到 Cloudflare 面板操作(只需一次):
  SSL/TLS → Overview → 选择 "Full"(或 "Full (strict)" —— 需先把
  $APP_DIR/secrets/origin.pem 换成 CF 面板签发的 Origin CA 证书)

切换后本脚本可安全重跑,不会造成中断。
EOF

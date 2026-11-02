#!/usr/bin/env bash
# Caraok — 一键部署到远端 VPS(经由 Cloudflare 代理的域名)
#
# 部署拓扑假设(与仓库默认配置一致):
#   Cloudflare(边缘 TLS)→ 源站 80 端口的 nginx(按 Host vhost 分流)
#     → 172.17.0.1:8091 的 Caddy/FastAPI compose 栈
#   可选:源站 443(自签或 CF Origin CA 证书)供 Cloudflare SSL 模式 Full 回源,
#   见 Caddyfile.tls / compose.tls.yml / scripts/enable-origin-tls.sh。
#
# 配置来源(优先级:环境变量 > scripts/deploy.conf > 必填项报错):
#   CARAOK_SERVER           ssh 目标,如 root@your-vps          (必填)
#   CARAOK_DOMAIN           对外域名,如 audio.your-domain.example (必填)
#   CARAOK_ORIGIN_IP        源站公网 IP,用于"直连已被阻断"的自检  (可选)
#   CARAOK_APP_DIR          服务器上的应用目录                    (默认 /opt/caraok)
#   CARAOK_BIND             compose 栈绑定的宿主网关地址:端口      (默认 172.17.0.1:8091)
#   CARAOK_NGINX_CONTAINER  源站 nginx 的容器名                   (默认 nginx-tcp-proxy)
#   CARAOK_NGINX_CONF       源站 nginx.conf 在宿主上的路径          (默认 /etc/nginx/nginx.conf)
#
# 用法: bash scripts/deploy.sh
set -euo pipefail

CONF_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy.conf"
[ -f "$CONF_FILE" ] && source "$CONF_FILE"

SERVER="${CARAOK_SERVER:-}"
DOMAIN="${CARAOK_DOMAIN:-}"
ORIGIN_IP="${CARAOK_ORIGIN_IP:-}"
APP_DIR="${CARAOK_APP_DIR:-/opt/caraok}"
BIND="${CARAOK_BIND:-172.17.0.1:8091}"
NGINX_CONTAINER="${CARAOK_NGINX_CONTAINER:-nginx-tcp-proxy}"
NGINX_CONF="${CARAOK_NGINX_CONF:-/etc/nginx/nginx.conf}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

[ -n "$SERVER" ] || { echo "✗ 缺少 CARAOK_SERVER(ssh 目标)。请在 scripts/deploy.conf 或环境变量中设置。" >&2; exit 1; }
[ -n "$DOMAIN" ] || { echo "✗ 缺少 CARAOK_DOMAIN(对外域名)。请在 scripts/deploy.conf 或环境变量中设置。" >&2; }

repo_root() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }
r() { ssh "${SSH_OPTS[@]}" "$SERVER" "$@"; }
banner() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

# ------------------------------------------------------ PHASE 0: 环境确认
banner "PHASE 0 环境确认"
r '
head -2 /etc/os-release | tail -1
docker --version && docker compose version | head -1
free -m | awk "NR==2{printf \"mem: %dMB total, %dMB avail\n\", \$2, \$7}"
command -v rsync >/dev/null || apt-get install -y -qq rsync
'
if [ -n "$ORIGIN_IP" ]; then
  DNS_OK=$(r "getent ahosts $DOMAIN | awk '{print \$1}' | grep -qx '$ORIGIN_IP' && echo yes || echo no")
  [ "$DNS_OK" = "yes" ] && echo "✓ DNS: $DOMAIN 经代理回源 $ORIGIN_IP" \
    || echo "⚠ DNS: $DOMAIN 未直接解析到 $ORIGIN_IP(经 Cloudflare 代理属预期)"
fi

# ------------------------------------------------------ PHASE 1: 代码 + 密钥
banner "PHASE 1 上传代码 + 生成 .env(密钥只在服务器)"
cd "$(repo_root)"
# 绝不允许部署破坏只存在于服务器上的运行时状态:
#   media/             已下载音频(rsync --delete 会清掉)
#   config/tracks.yaml 首次导入后在服务器上改过
#   data/              SQLite 曲库
#   compose.proxied.yml / Caddyfile.active / Caddyfile.tmp  服务器上生成
# 被排除的路径同时受 --delete 保护(未使用 --delete-excluded)。
rsync -az --delete \
  --exclude .git --exclude node_modules --exclude dist --exclude .env \
  --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  --exclude .ytdl-cache --exclude youtube_cookies.txt --exclude secrets \
  --exclude data --exclude media \
  --exclude 'config/tracks.yaml' \
  --exclude compose.proxied.yml --exclude Caddyfile.tmp \
  --exclude compose.local.yml --exclude Caddyfile.local \
  --exclude compose.tls.yml --exclude Caddyfile.active \
  --exclude deploy.conf \
  ./ "$SERVER:$APP_DIR/"
# config/tracks.yaml 仅在目标缺失时补种(--ignore-existing),已有文件一律不动:
# 曲库以 SQLite 为准,YAML 只是首次启动的种子。
rsync -az --ignore-existing -R ./config/tracks.yaml "$SERVER:$APP_DIR/"
r "
cd $APP_DIR
if [ ! -f .env ]; then
  umask 077
  printf 'DOMAIN=%s\nACCESS_TOKEN=%s\nAPP_SECRET_KEY=%s\nAUDIO_ALLOWED_HOSTS=%s\nCOOKIE_SECURE=true\nSESSION_TTL_SECONDS=2592000\nTRACKS_CONFIG=/app/config/tracks.yaml\nDB_PATH=/app/data/caraok.db\nPUBLIC_MEDIA_BASE=https://%s/media\n' \
    '$DOMAIN' \"\$(openssl rand -hex 32)\" \"\$(openssl rand -hex 32)\" '$DOMAIN' '$DOMAIN' > .env
  echo '.env 已生成(600)'
else
  current=\$(grep -E '^AUDIO_ALLOWED_HOSTS=' .env | head -1 | cut -d= -f2-)
  case \"\$current\" in
    '')
      sed -i.bak 's|^AUDIO_ALLOWED_HOSTS=.*|AUDIO_ALLOWED_HOSTS=$DOMAIN|' .env ;;
    *example.com*)
      sed -i.bak 's|^AUDIO_ALLOWED_HOSTS=.*|AUDIO_ALLOWED_HOSTS=$DOMAIN|' .env
      echo '⚠ 修正占位值 AUDIO_ALLOWED_HOSTS -> $DOMAIN' ;;
    *,${DOMAIN},*|${DOMAIN},*|*,${DOMAIN})
      ;;
    *)
      sed -i.bak \"s|^AUDIO_ALLOWED_HOSTS=.*|AUDIO_ALLOWED_HOSTS=\$current,$DOMAIN|\" .env
      echo '⚠ AUDIO_ALLOWED_HOSTS 补入 $DOMAIN' ;;
  esac
  grep -q '^AUDIO_ALLOWED_HOSTS=' .env || echo 'AUDIO_ALLOWED_HOSTS=$DOMAIN' >> .env
  grep -q '^PUBLIC_MEDIA_BASE=' .env || echo 'PUBLIC_MEDIA_BASE=https://$DOMAIN/media' >> .env
  grep -q '^DB_PATH=' .env || echo 'DB_PATH=/app/data/caraok.db' >> .env
  # 下载管道需要本 compose 网络的实际网关(docker0 的 172.17.0.1 通常不可用)
  GW=\$(docker network inspect \${COMPOSE_PROJECT_NAME:-\$(basename $APP_DIR)_}-private-audio \
        --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
  if [ -n \"\$GW\" ]; then
    grep -q '^YTDLP_PROXY=' .env || echo \"YTDLP_PROXY=socks5h://\$GW:40001\" >> .env
    grep -q '^YTDLP_POT_URL=' .env || echo \"YTDLP_POT_URL=http://\$GW:4416\" >> .env
  fi
  rm -f .env.bak
  echo '.env 已存在,保留原值(已校验 AUDIO_ALLOWED_HOSTS)'
fi"

# ------------------------------------------------------ PHASE 2: 构建 + 启动
banner "PHASE 2 构建并启动(绑定 $BIND)"
# 运行时目录先就位:SQLite 库、媒体、缓存必须归容器用户(10001)所有,
# 否则 docker 会以 root 建档导致下载写入失败。
r "mkdir -p $APP_DIR/data $APP_DIR/media
chown -R 10001:10001 $APP_DIR/media $APP_DIR/.ytdl-cache $APP_DIR/config $APP_DIR/data 2>/dev/null || true"
# 内层 Caddy 的站点地址由 Cloudflare 回源方式决定:
#   - 有源站证书 -> 双协议(80 + 443),供 CF Full 模式回源
#   - 无证书     -> 仅 80,供 CF Flexible 回源
# 按证书是否存在自动选择,重部署不会把已启用的 TLS 掉回 HTTP-only。
r "cd $APP_DIR && \\
   printf 'services:\\n  web:\\n    ports: !override [\\"%s:80\\"]\\n' '$BIND' > compose.proxied.yml && \\
   if [ -s secrets/origin.pem ] && [ -s secrets/origin.key ]; then \\
     cp Caddyfile.tls Caddyfile.active && echo '源站 TLS: 启用(80 + 443)' && \\
     docker compose -f docker-compose.yml -f compose.proxied.yml -f compose.tls.yml up -d --build; \\
   else \\
     sed 's/{\\$DOMAIN}/:80/' Caddyfile > Caddyfile.active && echo '源站 TLS: 未启用(仅 80)' && \\
     docker compose -f docker-compose.yml -f compose.proxied.yml up -d --build; \\
   fi"
r "sleep 6 && docker ps --filter 'name=caraok' --format '{{.Names}} {{.Status}}'"
echo "--- 内网直连验证 ---"
r "if [ -s secrets/origin.pem ]; then \\
     curl -sk --max-time 10 --resolve $DOMAIN:8443:172.17.0.1 https://$DOMAIN:8443/healthz >/dev/null \\
       && echo '✓ 源站 443 TLS 正常(CF Full 可用)' \\
       || echo '✗ 源站 443 失败(CF 请保持在 Flexible)'; \\
   fi"
r "curl -s --max-time 10 http://$BIND/healthz"

# ------------------------------------------------------ PHASE 3: nginx vhost
banner "PHASE 3 nginx vhost 托管块刷新(real_ip + 代理头覆写)"
r "set -e
cp $NGINX_CONF $NGINX_CONF.bak.caraok
python3 $APP_DIR/scripts/vhost.py --conf $NGINX_CONF --domain $DOMAIN
if docker exec $NGINX_CONTAINER nginx -t; then
  docker exec $NGINX_CONTAINER nginx -s reload
  echo '✓ vhost 托管块已刷新并 reload'
else
  cp $NGINX_CONF.bak.caraok $NGINX_CONF
  docker exec $NGINX_CONTAINER nginx -s reload
  echo '✗ nginx -t 失败,已回滚' >&2
  exit 1
fi"
echo "--- nginx 路由验证(Host: $DOMAIN) ---"
r "curl -s --max-time 10 -H 'Host: $DOMAIN' http://127.0.0.1/healthz"
echo
echo "--- 现有默认站点无损确认 ---"
r "curl -s --max-time 10 http://127.0.0.1/ | head -c 120; echo"

# ------------------------------------------------------ PHASE 3.5: 源站 80 仅允 Cloudflare(#5)
banner "PHASE 3.5 源站 80 锁定为仅 Cloudflare 网段"
r "set -e
bash $APP_DIR/scripts/firewall-cf.sh
cat > /etc/systemd/system/caraok-firewall.service <<'SV'
[Unit]
Description=Restore caraok Cloudflare-only port 80 firewall
After=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
ExecStart=$APP_DIR/scripts/firewall-cf.sh
[Install]
WantedBy=multi-user.target
SV
systemctl daemon-reload
systemctl enable caraok-firewall.service >/dev/null 2>&1 || true"
# 验证:经 CF 必须正常(失败自动回滚);直连源站必须被阻断
r "if curl -s --max-time 15 https://$DOMAIN/healthz | grep -q ok; then echo '✓ CF 链路正常'; else echo '⚠ CF 链路异常,回滚防火墙'; iptables -D INPUT -p tcp --dport 80 ! -i lo -m set ! --match-set caraok_cf src -j DROP 2>/dev/null; iptables -D DOCKER-USER -p tcp --dport 80 -m set ! --match-set caraok_cf src -j DROP 2>/dev/null; fi"
if [ -n "$ORIGIN_IP" ]; then
  r "curl -s --max-time 8 -o /dev/null -w 'direct-ip: %{http_code}\n' -H 'Host: $DOMAIN' http://$ORIGIN_IP/healthz || echo 'direct-ip: 已阻断 ✓'"
fi

# ------------------------------------------------------ PHASE 4: 端到端验证
banner "PHASE 4 经 Cloudflare 全链路验证"
r "curl -sS --max-time 20 https://$DOMAIN/healthz && echo || echo '⚠ CF 链路暂未通(DNS/CF 缓存,通常 1-2 分钟内生效)'"
r "curl -sI --max-time 20 https://$DOMAIN/ | grep -iE '^(HTTP|content-security-policy|strict-transport|x-content-type|x-frame|referrer|server)' || true"

# ------------------------------------------------------ PHASE 5: 交付
banner "完成 — 登录 Token(请妥善保存)"
r "grep '^ACCESS_TOKEN=' $APP_DIR/.env"
cat <<EOF
访问: https://$DOMAIN/
曲库: 登录后在 /admin 页面管理(持久化于 $APP_DIR/data/caraok.db)。
  config/tracks.yaml 只在数据库首次创建时导入一次,之后改它不再生效。
  备份对象是 data/caraok.db + media/ + .env,不是 tracks.yaml。
本地媒体: 下载的音频只能经鉴权接口 /api/v1/stream/{id} 读取,/media/* 返回 404。
新增音源主机(远程 URL)时,写入 $APP_DIR/.env 的 AUDIO_ALLOWED_HOSTS 后需:
  cd $APP_DIR && docker compose -f docker-compose.yml -f compose.proxied.yml up -d --force-recreate api
EOF

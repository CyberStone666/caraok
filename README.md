# Caraok — 私人车机 PWA 音频

个人使用的车机音频 Web App。主页按分类展示作品，点击进入播放器；浏览器只访问同源 PWA/API，音频由 FastAPI 以流式方式发给已登录的客户端。远程音源由服务端配置中继，也可以把 YouTube 音频下载到本机 `media/` 后作为本地曲目播放（该能力默认关闭，见下文「重要边界」）。

## 重要边界

- **远程音源**：`url` 必须是你拥有或明确获得转发权的 HTTPS 音频文件地址。项目不会因为"私人使用"而自动获得授权。
- **本地下载**：管理后台内置 yt-dlp 下载管道，但它**默认关闭**——只有容器内存在可用的 `YTDLP_COOKIES` 文件时才启用，否则下载接口直接不可用（音频播放不受影响）。是否启用、下载什么内容、是否拥有相应授权，全部由部署者自行决定并承担合规责任。项目本身不主张任何授权。
- 无论远程还是本地，音频都**只能经鉴权接口** `/api/v1/stream/{id}` 读取；`media/` 不做静态托管，`/media/*` 一律 404。

## 功能

- 曲库持久化在 SQLite（`data/caraok.db`，WAL 模式）。`config/tracks.yaml` 只在数据库首次创建时导入一次，之后修改它不再生效。
- 管理后台：新增/编辑/删除曲目、上下架、录入远程 URL、发起下载并查看进度。
- 客户端只看到作品元数据，拿不到上游 URL 或签名查询参数。
- 流式接口 `GET/HEAD /api/v1/stream/{id}`：支持单段 `Range` / `If-Range` / 条件请求，透传 200/206/304/416 与 `Content-Range`、`ETag` 等头；本地文件直接读盘，不做自我反代。
- 手动处理上游 HTTPS 重定向，每一跳都必须命中 `AUDIO_ALLOWED_HOSTS`。
- Access Token 登录后换成签名 HttpOnly Cookie；浏览器不持久化主 Token，`<audio>` 靠同源 Cookie 播放。
- PWA 只缓存前端壳，不缓存 `/api/*`、鉴权数据或音频流；Service Worker 缓存名随构建产物哈希更新。
- 响应式车机 UI、进度拖动、上一首/下一首、Media Session（取决于车机浏览器支持）。

## 目录

```text
.
├── backend/
│   ├── app/
│   │   ├── main.py          # API、会话、流式中继（含本地媒体直读）
│   │   ├── admin.py         # 管理接口与 yt-dlp 下载管理器
│   │   ├── catalog.py       # SQLite 曲库 + YAML 首次导入
│   │   ├── media.py         # PUBLIC_MEDIA_BASE → media_dir 的文件解析
│   │   ├── origin.py        # Origin 校验
│   │   └── settings.py
│   ├── tests/
│   ├── Dockerfile
│   └── requirements.txt
├── config/tracks.yaml       # 首次启动的曲库种子（之后不再读取）
├── data/                    # 运行时生成：caraok.db（已排除同步）
├── media/                   # 运行时生成：下载的音频（已排除同步）
├── frontend/                # React PWA + /admin 页面
├── scripts/
│   ├── deploy.sh            # 本仓库使用的生产部署脚本
│   ├── vhost.py             # nginx vhost 托管块生成
│   └── firewall-cf.sh       # 源站仅允许 Cloudflare 网段
├── Caddyfile / Caddyfile.local
├── docker-compose.yml / compose.local.yml
└── .env.example
```

## 1. 部署

### 1A. 本仓库实际使用的拓扑（推荐）

```text
客户端 → Cloudflare(边缘 TLS) → 源站 nginx-tcp-proxy(443, vhost 路由)
       → caraok web 容器(172.17.0.1:8091, 内层 Caddy 只听 :80, 仅 docker 内网可达)
       → api 容器(:8000, 不发布端口)
```

前置条件：

1. 域名的 DNS 已由 Cloudflare 代理。
2. 在 `scripts/deploy.conf` 填入站点信息（从 `scripts/deploy.conf.example` 复制，
   该文件已被 .gitignore 排除，不会泄露服务器地址）：

   ```bash
   cp scripts/deploy.conf.example scripts/deploy.conf
   # 编辑 CARAOK_SERVER / CARAOK_DOMAIN / CARAOK_ORIGIN_IP
   ```
2. **源站 443 已配置 Cloudflare Origin Certificate，Cloudflare SSL/TLS 模式必须是 Full (strict) 或 Full，不能是 Flexible**——内层 Caddy 只监听 HTTP，Flexible 会让回源链路变成明文。
3. 源站已存在 `nginx-tcp-proxy` 容器（脚本会备份 + `nginx -t` + reload，失败自动回滚）。

执行：

```bash
bash scripts/deploy.sh
```

脚本做四件事：rsync 代码（**不覆盖** `media/`、`data/`、`config/tracks.yaml`、`compose.proxied.yml`、`.env`）、生成/校正 `.env`、构建并启动栈、刷新 nginx vhost 并用 `firewall-cf.sh` 把源站 80 锁定为仅 Cloudflare 网段。

### 1B. 通用 VPS（Caddy 直接对外）

```bash
git clone https://github.com/CyberStone666/caraok.git caraok && cd caraok
cp .env.example .env && chmod 600 .env
# 编辑 .env：DOMAIN / ACCESS_TOKEN / APP_SECRET_KEY / AUDIO_ALLOWED_HOSTS
docker compose up -d --build
```

Caddy 会为 `DOMAIN` 自动申请证书；需放行 TCP 80/443、UDP 443。不要把 8000 端口发布到公网。

### `.env` 关键项

```bash
DOMAIN=audio.your-domain.example
ACCESS_TOKEN=<openssl rand -hex 32>
APP_SECRET_KEY=<另一轮 openssl rand -hex 32>

# 精确主机名，逗号分隔。必须包含 DOMAIN 本身——下载到本机的音频就挂在该域名下，
# 否则播放本地曲目会被 allowlist 拒绝（502）。
AUDIO_ALLOWED_HOSTS=audio.your-domain.example,cdn.your-audio-host.example

# 本地媒体的公开前缀；主机名必须与 AUDIO_ALLOWED_HOSTS 中的一致
PUBLIC_MEDIA_BASE=https://audio.your-domain.example/media

COOKIE_SECURE=true
SESSION_TTL_SECONDS=2592000
TRACKS_CONFIG=/app/config/tracks.yaml
DB_PATH=/app/data/caraok.db
```

`ACCESS_TOKEN`、`APP_SECRET_KEY` 长度不足 32 字符时 API 拒绝启动；`AUDIO_ALLOWED_HOSTS` 为空或含协议/端口/路径时同样拒绝启动。

## 2. 曲库管理

登录 `https://你的域名/admin`：

- 分类与曲目列表、上下架、删除（删除时一并清掉本地音频文件）。
- 新增曲目可填远程 HTTPS URL，或使用已下载到 `media/` 的文件。
- 若启用了下载管道，可提交链接；进度通过 `/api/v1/admin/downloads` 轮询。

`config/tracks.yaml` 只在 `data/caraok.db` 不存在时导入一次。**之后请改数据库或走后台**，改 YAML 没有任何效果。种子格式示例：

```yaml
categories:
  - id: podcast
    name: 播客
    order: 1
tracks:
  - id: episode-001
    title: 节目标题
    artist: 创作者
    category_id: podcast
    cover_url: https://images.your-domain.example/covers/episode-001.jpg
    url: https://media.your-domain.example/audio/episode-001.mp3
    mime_type: audio/mpeg
    enabled: true
    order: 1
```

约束：`id` 唯一且只含字母/数字/`_`/`-`；`url` 必须 HTTPS、无内嵌凭据、非 IP 字面量；主机名及每次重定向目标都必须在 `AUDIO_ALLOWED_HOSTS` 内；`mime_type` 限于 `audio/mpeg`、`audio/mp4`、`audio/aac`、`audio/ogg`、`audio/flac`、`audio/wav`、`audio/x-wav`、`audio/webm`、`audio/x-m4a`、`application/octet-stream`。

## 3. 本地开发

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ACCESS_TOKEN="$(openssl rand -hex 32)"
export APP_SECRET_KEY="$(openssl rand -hex 32)"
export AUDIO_ALLOWED_HOSTS="media.example.com"
export TRACKS_CONFIG="../config/tracks.yaml"
export DB_PATH="../data/caraok.db"
export MEDIA_DIR="../media"
export COOKIE_SECURE=false
uvicorn app.main:app --reload --app-dir .
```

另一个终端：

```bash
cd frontend && npm install && npm run dev
```

Vite 把 `/api` 代理到 `127.0.0.1:8000`。本地用 `COOKIE_SECURE=false`，生产必须为 `true`。

## 4. API

除 `/healthz` 与登录接口外，全部需要 Bearer Token 或有效会话 Cookie。

| Method | Endpoint | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/v1/auth/session` | `{ "token": "..." }` 换取 HttpOnly 会话 |
| GET | `/api/v1/auth/me` | 验证会话 |
| POST | `/api/v1/auth/logout` | 注销 |
| GET | `/api/v1/categories` | 分类及作品数 |
| GET | `/api/v1/tracks` | 全部启用作品（`?category=` 过滤） |
| GET | `/api/v1/tracks/{id}` | 单个作品元数据 |
| GET/HEAD | `/api/v1/stream/{id}` | 受保护的音频流（远程中继 / 本地直读） |
| GET | `/api/v1/admin/categories` | 全部分类（含空分类） |
| GET/POST | `/api/v1/admin/tracks` | 列出 / 新增曲目 |
| PATCH/DELETE | `/api/v1/admin/tracks/{id}` | 修改 / 删除 |
| GET/POST | `/api/v1/admin/downloads` | 下载任务列表 / 发起下载 |

播放与 Range 示例：

```bash
curl -I -H "Authorization: Bearer $ACCESS_TOKEN" \
  https://audio.your-domain.example/api/v1/stream/episode-001

curl -v -H "Authorization: Bearer $ACCESS_TOKEN" -H "Range: bytes=0-1023" \
  https://audio.your-domain.example/api/v1/stream/episode-001 -o /tmp/sample.part
```

## 5. 更新与备份

更新代码后重建：

```bash
bash scripts/deploy.sh          # 1A 拓扑
# 或
docker compose up -d --build    # 1B 拓扑
```

**备份对象**（只有这些不可再生）：

| 路径 | 内容 |
|---|---|
| `data/caraok.db` | 曲库（分类 / 曲目 / 上下架状态） |
| `media/` | 下载到本机的音频文件 |
| `.env` | 密钥与 allowlist |
| caddy volume | TLS 证书（仅 1B 拓扑） |

`config/tracks.yaml` 已不是运行时数据源，备份它只保留了首次种子。

## 6. 安全与限制

- Token 是个人访问凭证；登录接口按客户端 IP 在 15 分钟内限制 8 次失败。经可信反代访问时客户端身份取自 `CF-Connecting-IP`（Caddy 会剥离客户端伪造的同名头）或 `X-Forwarded-For` 最后一跳；直连时取 socket 对端。泄露后请更换 `ACCESS_TOKEN` 并 `docker compose up -d --force-recreate api`（`restart` 不会重载环境变量）。
- `/media/*` 由 Caddy 固定返回 404，web 容器不再挂载 `media/`；本地音频只能经 `/api/v1/stream/{id}` 读取。
- 会话 Cookie 为 HttpOnly、SameSite=Strict、生产 Secure；前端不持久化主 Token。
- API 不提供任意 URL 代理，只能播曲库里启用的 track ID。
- 仅 HTTPS 且精确匹配 allowlist 的来源与重定向主机可被访问，降低 SSRF 风险。
- 下载管道可以关闭（不提供 `YTDLP_COOKIES`）；启用时它对 `media/` 有写权限，请在可信环境使用。
- 单人私人服务：没有注册、权限分级与多用户审计。
- Media Session、锁屏与方向盘按键取决于车机浏览器，需实机验证。
- PWA 离线只缓存应用壳，播放需要网络。

## 7. 审查记录与测试

多轮安全与协议审查见 [`SECURITY_REVIEW.md`](./SECURITY_REVIEW.md)。

```bash
cd backend
pip install -r requirements-dev.txt
pytest -q
```

覆盖：登录鉴权与限流、上游 URL 不泄露、Range/条件请求语义、allowlist 校验、本地媒体鉴权直读与路径穿越、下载并发上限、曲库并发写。

## License

[MIT](./LICENSE)

# Caraok MVP — 多轮审查记录

审查目标：个人内部使用、海外 VPS（Cloudflare → nginx-tcp-proxy → Caddy → FastAPI）；车机浏览器打开 PWA，主页展示分类与作品，点击进入纯音频播放器；曲库持久化在 SQLite（`data/caraok.db`，首次由 `config/tracks.yaml` 导入），音频一律经 `/api/v1/stream/{id}` 鉴权流式下发，下载到本机的音频同样只走该接口；客户端必须登录 Token。管理后台内置可选的 yt-dlp 下载管道，默认关闭，是否启用与合规责任在部署者。

## Round 1 — 功能与架构

- [x] 配置文件 `config/tracks.yaml` 定义分类、作品、封面、来源 URL、MIME、启用状态和排序。
- [x] 首页展示分类 chips 和作品列表；点击作品进入独立播放器页。
- [x] 播放器提供播放/暂停、上一首/下一首、进度条和队列。
- [x] `<audio>` 始终挂载在应用根布局内，返回列表不会销毁当前媒体元素。
- [x] 音频通过同源 `/api/v1/stream/{id}` 获取，不通过 iframe。
- [x] Service Worker 仅缓存应用壳，不缓存 `/api/*` 或音频流。

## Round 2 — 鉴权与攻击面

- [x] 客户端只能提交 track ID，不能提交任意 URL，避免开放代理。
- [x] 上游 URL 仅从服务器 YAML 读取；API 元数据不返回音频源 URL、查询签名或上游响应中的 Location。
- [x] 仅接受 HTTPS、默认 443 端口、精确 allowlist DNS 主机；拒绝 IP 字面量、localhost、`.local` 和 `.internal` 主机。
- [x] 每次重定向都重新校验 HTTPS、DNS hostname、非 IP 字面量与 allowlist，最多跟随 3 次；不自动跟随重定向。
- [x] 上游 HTTP 客户端关闭环境代理信任，不转发客户端 Cookie、Authorization、Referer 等敏感请求头。
- [x] 登录 Token 使用常量时间比较；登录接口每个客户端 IP 的 15 分钟窗口最多允许 8 次失败尝试；非法 Cookie Secure 配置会直接拒绝启动，不会静默降级为不安全 Cookie。
- [x] 登录换取签名 HttpOnly、SameSite=Strict Cookie；生产环境 Secure；cookie path 限定 `/api`。
- [x] 登录与注销校验 Origin；API 响应 no-store；没有开放 CORS。
- [x] 会话签名密钥同时依赖 `APP_SECRET_KEY` 和 `ACCESS_TOKEN`，更换任一项会使所有旧会话失效。
- [x] 配置校验错误与流式错误日志不输出带签名参数的完整上游 URL。

## Round 3 — 音频协议与资源生命周期

- [x] 支持 GET 和 HEAD。
- [x] 透传单段 `Range`、`If-Range`、`If-None-Match`、`If-Modified-Since`。
- [x] 透传 200、206、304、416 中适用于媒体播放的状态码和 Content-Type、Content-Length、Content-Range、Accept-Ranges、ETag、Last-Modified 等响应头。
- [x] 拒绝多段 Range；上游 206 缺少 Content-Range 时返回 502；416 不透传上游 HTML 错误体或错误 Content-Length。
- [x] 使用 HTTPX 异步原始字节迭代器与 FastAPI StreamingResponse，不读取整个音频文件到内存。
- [x] 上游响应在正常完成、客户端取消、HEAD、错误状态和重定向时关闭。
- [x] 配置上游 60 秒读取空闲超时；播放过程中不会因总时长长而设置总时长限制。
- [x] 配置 Content-Type 白名单，避免把 HTML 错误页面当作音频下发。

## Round 4 — 配置、PWA 与部署

- [x] YAML 变更按 mtime 热加载，无须重启服务或重建前端。
- [x] 新配置校验失败时保留 last-known-good 配置。
- [x] 前端生产构建放入 Caddy 镜像；API 8000 端口仅在 Docker 内网暴露。
- [x] Caddy 终止 TLS、自动证书续期、SPA fallback、基础安全响应头与 CSP。
- [x] Python API 容器以非 root UID 运行；配置目录只读挂载。
- [x] Vite dev proxy 保留 Host，匹配登录接口的 Origin 校验。
- [x] Manifest 提供 192/512 PNG 图标；Service Worker 注册需要安全上下文，生产环境由 HTTPS 提供。
- [x] 依赖选择更新为 React 19.3、Vite 8.3、`@vitejs/plugin-react` 6；Node 22.12+ 满足 Vite 8 要求。

## Round 5 — 最终边界回归

- [x] 上游返回 416 时，网关不转发上游错误页面的 Content-Type / Content-Length，只保留必要的 Range 元数据。
- [x] 重定向目标即使出现在 allowlist 中，也拒绝 IP 字面量，避免误配置时访问私有或云 metadata 地址。
- [x] `COOKIE_SECURE` 只接受明确的 true/false 字面值；拼写错误直接拒绝启动，防止静默降级。
- [x] Media Session 同时检查 `MediaMetadata` 能力，缺少该 API 时播放器仍可通过页面控件工作。

## Round 6 — 深度复审（2026-10-31，全量代码精读 + 真实集成验证）

本轮逐文件精读全部 26 个源文件，对可疑点均以真实库源码/实测裁定，修复以下问题：

- [x] **（高）登录限流可被伪造 `X-Forwarded-For` 绕过**。原 uvicorn CMD 使用 `--proxy-headers --forwarded-allow-ips "*"`，实测 uvicorn 0.39 `_TrustedHosts.get_trusted_client_host` 在通配信任下取 XFF **最左**值，而 Caddy 只是把真实客户端 IP **追加**到客户端可自填的链尾——攻击者每请求轮换首个伪造条目即可无限次猜测 Token，并使限流字典膨胀。修复：移除 uvicorn 代理头信任；应用层新增 `_login_throttle_key`，仅当 socket 对端为私网地址（可信反代拓扑）时取 XFF **最后一跳**（由 Caddy 追加、客户端不可控），否则取对端地址，键长截断 64。
- [x] （中）前端会话过期后停留在已登录态反复报错；现 401 时 `ApiError.status` 驱动回退登录页。
- [x] （低）`AUDIO_ALLOWED_HOSTS` 启动时校验条目为裸主机名（拒绝 scheme/端口/路径/内嵌空格/单标签），防止误配置导致所有流静默 502。
- [x] （低）Compose 两个服务增加 json-file 日志轮转（10m×3），防止长期运行磁盘占满。
- [x] （低）前端 fetch 网络异常改为友好中文提示；CSP 补 `form-action 'self'`；README 的 MIME 列表与限流描述和代码同步。
- [x] （低）前端 Dockerfile 从无锁文件 `npm install` 改为 `npm ci`（仓库新增 `package-lock.json`），构建可复现。
- [x] 已排除的疑点（经实测）：itsdangerous 2.2.0 对畸形会话 Cookie 抛出 `BadSignature`（`BadData` 子类），被现有异常捕获覆盖，返回 401 而非 500——已补回归测试固化；Caddy 默认向既有 XFF 追加真实客户端 IP；416/304/HEAD 的头转发行为符合 RFC 语义。

### Round 6 验证记录

- 后端 pytest：**25 passed**（新增限流键 4 项、hosts 校验、畸形 Cookie 共 7 项测试）。
- 前端 `tsc -b` 通过；`docker compose build` 内含 `npm run build` 复验通过。
- Docker 集成（colima + compose，真实公网音频源）：`/healthz`、登录换 Cookie、未认证 401、`Range: bytes=0-1023` 与 `bytes=5000000-5000999` 中继结果与上游**逐字节一致**（206 + Content-Range 透传）、越界 416 不含上游错误页头、HEAD 返回完整 Content-Length、多段 Range 416、上游 URL 不出现在任何响应。
- 限流端到端：直连容器 8 次失败后同桶 429、异桶不受影响、耗尽桶内伪造首段 + 真 Token 仍 429；经真实 Caddy 链路，**每个请求轮换唯一伪造 XFF 仍在第 9 次触发 429**（修复前该模式可无限绕过），CSP 含 `form-action 'self'`，全部安全头在位。
- 本轮集成使用一次性测试环境（随机密钥、`COOKIE_SECURE=false`、localhost），验证后已全部清理；`.env` 未提交。

## Round 7 — P0 四项（#1–#4）

本轮处理"能直接造成数据泄露或数据丢失"的四个问题。修复基于 `3c3d6c8`（P1/P2/P3 已由并行线合入）。

- [x] **#1 `/media/*` 鉴权绕过（P0）**。web 容器曾把 `./media` 以只读方式挂到 `/srv/media`，而 Caddy 的 SPA 兜底 `handle` 带 `file_server`，因此任何人 `GET /media/<file>` 就能拿到 m4a，完全绕过 Token 与会话。
  三处修复：① `Caddyfile` / `Caddyfile.local` 在兜底 `handle` 之前新增 `handle /media/* { respond "Not found" 404 }`；② `docker-compose.yml` / `compose.local.yml` 移除 web 的 `./media:/srv/media:ro`；③ `stream_track` 识别 `PUBLIC_MEDIA_BASE` 前缀后改为**本地直读**（新增 `app/media.py:resolve_local_media` 与 `main.py:_local_media_response`），自行实现 200/206/304/416/HEAD，替代原先"请求打到公网域名再回环到自己"的自代理。
  文件解析只接受 `media_dir` 下的裸文件名：含 `/`、`\`、`.`、`..` 或解析后越界一律拒绝，因此伪造曲库条目无法读到 `media/` 之外的文件。
- [x] **#2 部署脚本的 `rsync --delete` 会删生产数据（P0）**。原脚本排除了 `.git`、`node_modules`、`dist`、`.env`、`.venv`、`__pycache__`、`.pytest_cache`、`.ytdl-cache`、`youtube_cookies.txt`、`secrets`、`data`，但**没有排除** `media/`、`config/tracks.yaml`、`compose.proxied.yml`。每次部署都会：删掉服务器上已下载的全部音频、用本地版本覆盖服务器上手改过的种子 YAML、删掉脚本自己生成的 compose 覆盖文件（导致下次 `-f compose.proxied.yml` 直接失败）。
  修复：新增 `--exclude media`、`--exclude 'config/tracks.yaml'`、`--exclude compose.proxied.yml`、`--exclude Caddyfile.tmp`、`--exclude .workbuddy`。rsync 的 exclude 在 `--delete` 下同样保护接收端（脚本没有使用 `--delete-excluded`）。另补 `mkdir -p $APP_DIR/media`：`media/` 被排除后首次部署不再由 Docker 以 root 创建目录，避免容器用户（10001）无法写入。
- [x] **#3 `.env` 的 `AUDIO_ALLOWED_HOSTS` 是占位值（P0）**。首次部署写入的是 `AUDIO_ALLOWED_HOSTS=media.example.com`，而 `PUBLIC_MEDIA_BASE=https://$DOMAIN/media`，于是所有本地曲目在 `_safe_https_host` 处被拒，播放返回 502。
  修复：新建 `.env` 时直接写 `$DOMAIN`；已存在的 `.env` 按四种情况校正——空值→写入域名、含 `example.com` 占位→替换、已含域名→不动、含其他自定义主机→追加域名；同时补 `DB_PATH` 默认值。
- [x] **#4 文档失真（P0）**。README 仍称 `config/tracks.yaml` 是唯一数据源且"改完 mtime 热加载"，未提 SQLite、`/media/*` 已封死、下载默认关闭、备份对象已从 YAML 变成 `data/caraok.db`；开篇"不解析或转发 YouTube 音轨"与内置 yt-dlp 管道自相矛盾，容易让部署者误判风险。
  修复：README 重写——补真实拓扑（CF → nginx → Caddy → API）与 Full(strict) 前置条件、`.env` 必须包含 `DOMAIN`、后台管理路径、备份表（`data/caraok.db` + `media/` + `.env`）、`tracks.yaml` 仅首次种子；合规表述改为"下载默认关闭，启用与责任在部署者"。本节开头的审查目标同步更正。

### Round 7 验证记录（#1–#4）

- 后端 pytest：**61 passed**（新增 `backend/tests/test_local_media.py`）。
- 本地直读用例把 `create_upstream_client` 换成"一被调用即抛 `ConnectError`"的桩：任何回落到 httpx 的路径都会得到 502，因此 200/206 只可能来自本地读盘。覆盖：未认证 401、全量 200 与 `Content-Length`、`Range: bytes=0-3`、`bytes=-10`、多段与越界 416（含 `Content-Range: bytes */size`）、HEAD 无体、`If-None-Match` 304、过期 `If-Range` 回全量、文件缺失 404、路径穿越与站外 URL 拒绝、无 `PUBLIC_MEDIA_BASE` 时禁用本地分支。
- `rsync --delete --dry-run` 对照实验（同一组源/目标）：未加新排除时输出 `*deleting media/song.m4a`、`*deleting media/`、`*deleting data/caraok.db`、`*deleting Caddyfile.tmp` 与 `>f.s..... config/tracks.yaml`；应用新排除后只剩预期的新增文件。证明 #2 是真实可复现的数据丢失，且修复有效。
- `.env` 校正逻辑四种输入实测：占位值→域名；自定义 CDN→追加域名；已含域名→不变；缺行→补默认值。
- `bash -n scripts/deploy.sh` 通过；`docker-compose.yml` / `compose.local.yml` 移除挂载后仍能被 Compose 解析（本机未执行 `compose config`，见下）。
- **未在本机验证**：`docker compose build/up`、Caddy 容器内 `caddy validate`、Cloudflare Full(strict) 真实回源、nginx `-t`。这些必须在 VPS 上按「上线前检查」执行。

## Round 8 — 对 Round 7 的再评估（2026-11-01，基线 `082681c`）

Round 7 交付后重新评估，重点不是重读代码，而是**用探针打自己刚写的分支**。结论：P0 四项的修复成立，但本地直读分支有三处工程缺口，已在本轮补掉。

### 对本地直读分支的探针（21 项，全部通过）

用 1 MiB 素材 + `TestClient`，并把 `create_upstream_client` 换成"一被调用即抛 `ConnectError`"的桩（任何回落到 httpx 的路径必然 502，因此 200/206 只能来自本地读盘）：

| 组 | 用例 | 结果 |
|---|---|---|
| 全量 | `GET` 无 Range，逐字节比对 + `Content-Length` | 200，1048576B 一致 |
| Range 边界 | `0-0`、`-1`、`0-`、`999999-9999999`（end 截断）、`1048575-1048575` | 206，`Content-Range` 与 `Content-Length` 均正确 |
| Range 拒绝 | `1048576-`（越界）、`-0`、`5-2`、`abc`、空值、多段 `0-1,3-4` | 全部 416 |
| 条件请求 | `If-None-Match: W/<etag>`、`*`、ETag + Range 同时出现 | 304（304 优先于 Range，符合 RFC 9110） |
| HEAD | `HEAD` + `Range: bytes=10-19` | 206，无体，`Content-Length: 10` |
| 空文件 | 无 Range / `bytes=0-` | 200 + CL 0；416 + `bytes */0` |
| 逃逸 | `media/link.m4a → /etc/passwd`（符号链接） | 502，未泄露任何字节 |
| 逃逸 | url 含 `../tracks.yaml` | 502，未读到配置文件 |
| 未知曲目 | `/stream/does-not-exist` | 404 |

### 本轮新发现并修复（🟡 3 项）

- [x] **本地流缺 `X-Accel-Buffering: no`**。远程中继分支一直带这个头，本地直读分支漏了。生产链路是 nginx-tcp-proxy → Caddy，nginx 默认 `proxy_buffering on`，会把大文件先落到临时盘再吐给车机，白白增加首字节延迟和磁盘 IO，进度拖动时尤其明显。补上与远程分支一致的头。
- [x] **中断时文件句柄释放依赖 GC**。原实现用 `iterate_in_threadpool(_file_chunks(...))` 包装同步生成器，而 Starlette 0.38 的 `StreamingResponse` 在客户端断开时不会调用生成器关闭（`stream_response` 里没有 `finally`）。车机上拖进度条会反复中断连接，句柄要等 GC 才释放。改为显式 `async` 生成器 + `anyio.to_thread` 读块 + `finally` 关文件，断开即释放。
- [x] **首次部署会得到空曲库**。Round 7 为保护服务器上手改的配置，把 `config/tracks.yaml` 从 rsync 排除了——但首次部署时服务器**本来就没有**这个文件，结果 `Catalog._migrate_yaml_seed` 直接返回（第 223 行 `not yaml_seed.is_file()`），曲库为空，而系统没有任何"创建分类"的接口，部署完无法添加曲目。
  修复：主 rsync 之后追加 `rsync -az --ignore-existing -R ./config/tracks.yaml`，只在目标缺失时补种，已有文件一律不动。本机模拟验证：目标缺失→写入本地种子；目标已存在→保留服务器版本。

### 未修，建议后续处理（🟢）

- `admin._file_status` / `delete_track` 各自实现了一遍"URL → media_dir 路径"的解析（`"/" not in name` + `is_relative_to`），与 `media.resolve_local_media` 重复。目前两处逻辑等价且都拒绝了穿越，暂不构成缺陷；但后续改路径规则时容易漏改一处，建议统一收敛到 `resolve_local_media`。

### 对剩余未关闭 issue 的核验（#10–#19）

代码层面均已实现，且多数有测试覆盖：

| Issue | 证据 | 测试 |
|---|---|---|
| #10 自代理 | `main.py` 本地分支直接读盘；本轮探针证明上游不可达时仍 200 | 探针 21 项 |
| #11 畸形 Origin 500 | `origin.py` 捕获 `urlsplit` 的 `ValueError` → 403，admin 与登录共用 | `test_admin_writes_reject_malformed_origin` |
| #12 PATCH null | `admin.py:525` `model_dump(exclude_unset=True, exclude_none=True)` | `test_admin_patch_null_leaves_fields_unchanged` |
| #13 非法 mime 500 | `DirectTrackBody.valid_mime_type` → 422 | `test_admin_add_rejects_bad_mime` |
| #14 空分类不可选 | `GET /admin/categories` 返回 count=0 的分类；前端 `api.adminCategories()` | `test_admin_categories_include_empty` |
| #15 上下架文案 | `admin.tsx:282` 按钮文案即将要执行的动作 | 人工核对 |
| #16 文件名/cookie 冲突 | `allocate_media_stem`（含 job_id + 去重）、`_writable_cookies` 每 job 独立副本 | `test_media_stem_does_not_collide`、`test_cookie_copy_is_per_job` |
| #17 无超时/孤儿进程 | `DOWNLOAD_TIMEOUT_SECONDS=900` + `wait_for` + `killpg` + `shutdown()` 取消 | `test_shutdown_cancels_inflight_tasks` |
| #18 供应链 | Dockerfile 锁 deno 2.9.8 并 `sha256sum -c`（amd64/arm64 分别固定）、`--no-remote-components`、requirements 全部 `==` | 人工核对 |
| #19 jobs 只增不删 | `JOB_HISTORY_LIMIT=50` + `_prune_jobs()`，提交与结束时各裁一次 | `test_download_history_is_capped` |

### Round 8 验证记录

- 后端 pytest：**61 passed**（本轮改动后复跑）。
- 探针：**21/21 PASS**（脚本为一次性，不入库；关键用例已由 `tests/test_local_media.py` 固化）。
- `bash -n scripts/deploy.sh` 通过；`--ignore-existing` 补种语义本地模拟通过。
- 仍未验证：`docker compose build/up`、Caddy 容器内 `caddy validate`、真实 Cloudflare Full(strict) 回源、nginx `-t`。

## 本次实际验证

- 后端 pytest：**19 passed**（最终复测无 warning）。
- Python `compileall`：通过。
- Docker Compose YAML、tracks YAML、manifest JSON、package JSON：语法解析通过。
- 前端 TSX/TS 源文件：TypeScript transpile 语法检查通过；使用临时 React 类型桩执行严格 TS 检查通过。

## 尚无法在当前执行环境完成的真实集成验证

- 当前环境没有 Docker CLI，不能在此处实际运行 `docker compose build/up` 或 Caddy 配置校验。
- 当前环境无法访问 npm registry，`npm install` 超时；因此没有声称完成完整的 `npm run build`。
- 没有用户的真实音频源 URL，尚未验证该音源是否正确支持 Range、是否会重定向到其他 CDN、签名 URL 是否有效。
- 没有目标车机硬件，不能保证该车机浏览器支持 PWA 安装、后台音频或 Media Session 按键；Media Session 并非所有浏览器都支持。

上线前必须在 VPS 用真实域名、真实 Token 和授权音频源完成一次 `docker compose up -d --build`，并在车机实测播放、暂停、拖动进度、锁屏/后台及下一首。`AUDIO_ALLOWED_HOSTS` 只应加入你信任的公网音频服务商域名。

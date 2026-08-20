# linux-autobook

在无图形界面的 Linux 服务器上运行的分布式文献传递系统。中心下载网关独占百度登录 Cookie；任意数量的普通 Worker 通过 TLS 网关取得原始文件，解压并将 PDG 页面合成为 PDF，最后上传到 Cloudreve、创建分享链接并回报结果。网站使用原子租约领取，避免多台服务器重复处理同一任务。

> 本项目只适用于你有权访问和传递的文献。请遵守内容版权、百度网盘服务条款、目标网站规则以及所在地法律。

## 功能概览

- 无需 Windows、百度网盘桌面客户端或 GUI 自动化，可在纯命令行 Linux 上运行。
- 使用群文件库的服务端搜索接口按 SS 号检索，不需要递归扫描数百万文件。
- 单个 Worker 内置线程池，可同时处理多个独立任务；默认并发数为 3。
- 支持多台 Linux Worker 横向扩展；百度 Cookie 只保存在中心下载网关。
- 网站任务使用 5 分钟可续租租约；Worker 每 60 秒心跳，宕机任务最多自动重排 3 次。
- 内置 HTTPS 管理面板，可配置全部网关/Worker参数、扫码登录、密码字典、服务状态和日志。
- 单个 Worker 串行请求领取；网站数据库原子分配任务，跨进程、跨服务器也不会重复领取。
- 支持百度网盘 App 扫码登录，登录 Cookie 以 `0600` 权限保存。
- 优先使用群文件短期直链；失败后自动转存到个人网盘临时目录。
- 优先使用 aria2；百度 CDN 拒绝整文件或大 Range 请求时，自动改用 4 MiB 顺序分段下载。
- 支持 PDF 直传以及 ZIP、UVZ、RAR/RAR5、7z、tar、gzip、bzip2、xz、CBZ 等归档格式。
- 支持最多 3 层、每层最多 32 个嵌套压缩包，扩展名不正确时也会检查文件头。
- 支持无密码和密码字典解压。
- 支持常见超星 PDG 页面、书籍元数据和目录写入；任何页面解码失败都会中止任务，不生成空白占位页冒充成功。
- 上传到 Cloudreve 后创建限时分享链接，并将进度、成功结果或错误回报给任务网站。
- 每个任务使用独立工作目录，结束后自动清理本地中间文件。

## 已验证的完整流程

项目曾在 Ubuntu 测试服务器上完成真实端到端验证：

- SS 号 `13128895`：群文件库命中 UVZ，解压并转换 242 页 PDG，生成的 PDF 242 页均包含页面图像。
- 百度 CDN 对整文件和 8 MiB Range 返回 HTTP 403 时，4 MiB 顺序 Range 回退能够完整下载并通过大小校验。
- 上传后的分享文件重新下载验证：文件大小与网盘元数据一致，PDF 可正常打开，242/242 页内容非空。
- 三任务并发验证：SS `12607753`、`14686528`、`13128895` 在 1 秒内全部进入处理状态，分别在 15、19、34 秒后完成；两个 PDF 直传任务和一个 UVZ/242 页 PDG 转换任务均成功生成分享文件。
- 本地和 Linux 服务器测试套件均通过 31 项测试。
- 分布式版本验证：12 个并发 claim 请求争抢 6 条合成任务时，恰好得到 6 个不同任务和 6 个不同租约；合法心跳全部续租，伪造租约、完成后的重复回调均被拒绝。
- 中心网关真实取件验证：SS `12607753` 经群搜索、百度下载、HTTPS 流式传输及 SHA-256 校验，取得 17,584,393 字节 PDF，随后网关与 Worker 临时文件均被清理。

以上记录用于说明已覆盖的路径，不保证任何百度接口、第三方网站或所有 PDG 变体永久兼容。

## 工作流程

```text
任务网站待处理队列（原子领取 + 可续租租约）
        │
        ▼
串行领取任务并提取 SS 号
        │
        ▼
中心 HTTPS 百度下载网关 ──► 群文件库服务端搜索
        │
        ▼
选择 PDF / 归档文件 ──► 直链下载 ──► 转存下载回退
        │
        ▼
PDF 直传 或 7z 解压 / 嵌套解压 / PDG 解码合并
        │
        ▼
Cloudreve 分片上传 ──► 创建限时分享链接
        │
        ▼
向任务网站回报成功或失败
```

## 并发模型

`CONCURRENCY` 控制同一进程内最多同时执行的任务数。主循环一次只调用一次领取接口，领取成功后把任务提交到线程池；活动任务达到上限时暂停领取，直到有任务结束。

不同任务分别使用：

- `runtime/downloads/task_<任务ID>_<SS号>`：下载目录；
- `runtime/work/task_<任务ID>_<SS号>`：解压和 PDF 生成目录；
- 独立 Cloudreve 上传会话。

默认值 `CONCURRENCY=3` 适用于约 4 核、8–16 GiB 内存的普通 VPS。PDG 转换和大压缩包解压会消耗 CPU、内存与磁盘，建议先从 2–3 开始，不要盲目设置为十几或几十。

`--once` 明确只领取一个任务，主要用于测试；要启用并发必须运行不带 `--once` 的常驻模式。

## 支持的输入

### 文件与归档

| 类型 | 处理方式 |
| --- | --- |
| PDF | 复制到任务输出名称后直接上传 |
| ZIP / UVZ / CBZ | 由 7-Zip 解压；UVZ 通常是使用自定义扩展名的 ZIP 容器 |
| RAR / RAR5 / 7z | 由系统 7-Zip 解压 |
| tar / gz / bz2 / xz / tgz / tbz2 / txz | 由系统 7-Zip 解压 |
| 扩展名错误或缺失 | 根据 ZIP、RAR、7z、gzip、bzip2、xz、tar 文件头识别 |
| 嵌套归档 | 最多递归 3 层，每层最多处理 32 个归档 |

压缩包中已存在 PDF 时，选择体积最大的 PDF；否则收集 PDG 页面并转换。

### PDG

当前转换器覆盖：

- 00H：私有 CCITT/JPEG 页面流；
- 02H：由内置 WASM 解码器处理的加密页面；
- 03H：先执行兼容解密，再交给 00H 解码路径；
- 11H：校验页面数据区后转换为兼容的 00H 标记；
- 实际为 JPG、PNG、TIFF 等标准图像但使用 `.pdg` 后缀的页面。

04H、05H、6xH、AxH、FFH 等未覆盖或未取得真实样本验证的变体会明确失败并回报错误。转换器不会用空白 TIFF 替代无法解码的页面。

## 系统要求

推荐：

- Ubuntu 22.04 / 24.04 或兼容的 Debian 系发行版；
- Python 3.10 及以上；
- 7-Zip（Ubuntu 包通常为 `p7zip-full`）；
- aria2；
- 至少 2 CPU、4 GiB 内存；并发 3 建议 4 CPU、8 GiB 以上；
- 足够容纳多个任务同时下载和解压的磁盘空间。

安装系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip p7zip-full aria2
```

确认 RAR/RAR5 支持：

```bash
7z i | grep -E 'Rar|Rar5'
```

## 安装

### 一键安装（推荐）

在 Ubuntu/Debian 服务器取得完整仓库后执行：

```bash
git clone https://github.com/moli-xia/linux-autobook.git
cd linux-autobook
sudo bash install.sh
```

私有仓库可先使用已登录的 GitHub CLI：

```bash
gh repo clone moli-xia/linux-autobook && cd linux-autobook && sudo bash install.sh
```

不带参数运行时会显示中文菜单，可选择：

- 安装“网关 + Worker”、仅网关或仅 Worker；
- 更新程序并保留配置、管理密码、百度登录态和 runtime 数据；
- 查看当前角色、TLS 模式和三个服务状态；
- 卸载并自动备份配置/runtime，或使用 `--purge` 彻底删除。

脚本会自动安装依赖、创建虚拟环境和 `autobook` 系统用户，只安装所选角色的 systemd unit，并运行测试。完成后终端显示管理面板链接，例如：

```text
Admin panel: https://服务器IP:8766/
Default username: admin
Default password: admin
```

登录后先打开“快速设置”。网关和 Worker 在必需凭据配置完成前保持停止；即使从命令行误启动，配置错误也会使用退出码 78 告诉 systemd 不要反复重启。

可选安装参数：

```bash
# 单机安装
sudo bash install.sh --action install --role all --public-host 203.0.113.10 --non-interactive

# 中心网关
sudo bash install.sh --action install --role gateway --public-host 203.0.113.10 --non-interactive

# 计算节点（公有 CA 证书无需填写 --gateway-ca-file）
sudo bash install.sh --action install --role worker \
  --gateway-url https://gateway.example.com:8765 \
  --gateway-token '与中心网关一致的令牌' --non-interactive
```

### 域名和免费 SSL 证书

域名 A/AAAA 记录已经指向服务器且公网可访问 80 端口时，安装菜单输入域名，或执行：

```bash
sudo bash install.sh --action install --role all \
  --domain books.example.com --acme-email admin@example.com --non-interactive
```

脚本使用开源 [Certbot](https://github.com/certbot/certbot) 向 [Let’s Encrypt](https://letsencrypt.org/) 申请证书。Certbot 的 systemd timer 自动续期，`deploy/autobook-cert-deploy.sh` 在续期后以正确权限复制证书并重启面板/网关。HTTP-01 验证必须能从公网访问 80 端口；签发失败时安装不会中断，而是保留自签名证书并给出修复提示。

没有域名时自动生成包含实际 IP/主机名 SAN 的自签名证书，浏览器首次访问会显示安全警告。Worker 连接自签名网关时填写 `BAIDU_GATEWAY_CA_FILE`；连接 Let’s Encrypt 等公有 CA 网关时留空，使用系统信任库。

### Docker 部署（支持 amd64 与 arm64）

镜像把管理面板、下载网关和 Worker 打包在一起，由面板内置的进程管理器托管，不依赖 systemd。
完整说明见 **[docs/Docker部署说明.md](docs/Docker部署说明.md)**。

```bash
git clone https://github.com/moli-xia/linux-autobook.git && cd linux-autobook
cp docker/.env.example .env      # 至少填写 AUTOBOOK_PUBLIC_HOST
docker compose up -d
```

首次启动会自动生成两套自签名证书和随机共享令牌、写好全部默认配置、
并用内置的 307 条常见密码初始化解压字典。配置与运行数据放在具名卷里，升级不丢。

`AUTOBOOK_ROLE` 控制角色：`all`（网关 + Worker）、`gateway`、`worker`。
自建镜像用 `./docker/build.sh`，默认同时构建 `linux/amd64` 与 `linux/arm64`。

容器部署与 systemd 部署可以混用，两者只通过 HTTPS + 共享令牌通信。

### 手工安装

```bash
sudo git clone https://github.com/moli-xia/linux-autobook.git /opt/autobook-linux
cd /opt/autobook-linux

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cp .env.example .env
cp password.example.txt password.txt
chmod 600 .env password.txt
```

在 `.env` 中填写任务网站 Worker Token、目标群组和结果网盘配置。不要把 `.env`、二维码、扫码凭据或真实密码字典提交到 Git。

## 配置

程序启动时读取项目根目录的 `.env`，但不会覆盖系统中已经存在的环境变量。

## Web 管理面板

> 完整的图文操作说明见 **[docs/管理面板使用说明.md](docs/管理面板使用说明.md)**，
> 从「机器刚装好系统」讲到「任务跑起来」，含逐条配置解释和故障排查对照表。

安装完成后访问脚本输出的链接（默认 `https://本机IP:8766/`），默认账号密码均为 `admin`。
面板是一个纯标准库实现的单页应用，只依赖 Python 自带模块，前端资源全部本地托管。

界面按使用顺序分为八页：

| 页面 | 作用 |
| --- | --- |
| **概览** | 主机资源、服务状态与操作按钮、阻止启动的待办清单（每条可一键跳到对应输入框） |
| **配置向导** | 分步只显示必需字段，完成的步骤自动打勾，最后一步直接检测并启动 |
| **配置** | 全部参数，按角色分组；「常用 / 全部参数」切换；每项自带说明与示例 |
| **连通性检测** | 实测任务网站令牌、网关 TLS 与令牌、Cloudreve 账号、百度登录态与目标群 |
| **百度登录** | 网关节点扫码登录，状态自动轮询，成功后自动重启网关 |
| **运行日志** | 选服务 / 行数 / 关键字过滤 / 自动刷新，敏感内容自动打码 |
| **任务记录** | 从 Worker 日志还原的任务表：书名、状态、进度、交付链接 |
| **解压密码** | 内置 307 条常见解压密码，可逐条增删改、搜索、批量编辑 |
| **节点管理** | 主服务器统一查看所有 Worker 的状态、任务与日志，远程启停服务，一键下发配置 |
| **维护** | 依赖修复、权限修复、证书重生成、备份、在线更新、角色切换、密码字典编辑 |

面板相比手工配置的价值：

- **不用查文档**：每个字段都带一句人话说明和示例值；
- **错在哪一目了然**：连通性检测逐项给出结论和修复建议，而不是让你去翻 journald；
- **不会配一半就启动**：必填项未完成时启动按钮锁定，避免 systemd 重启风暴；
- **常见问题一键修**：缺依赖、权限错、证书过期都在维护页一个按钮解决；
- **加机器很快**：新 Worker 装完只需复制一串接入码到主服务器，再一键下发令牌、网盘账号和网关证书；角色也可在面板里直接切换。

安全机制：

- 管理密码使用 PBKDF2-SHA256（600,000 次）和随机盐保存，不保存明文；
- Session Cookie 带 `Secure`、`HttpOnly`、`SameSite=Strict`，写操作校验 CSRF 请求头；
- 登录失败按来源 IP 限速（10 分钟内 8 次锁定）；
- 敏感字段不会回显，留空保存表示保持原值；日志中的令牌与下载直链自动打码；
- 配置文件以 `0600` 权限原子替换；只允许操作固定的三个 systemd 服务；
- 响应带 HSTS、CSP（`default-src 'self'`）、`X-Frame-Options: DENY` 等安全头。

管理面板需要写入 root-only 配置、控制 systemd 并安装系统依赖，因此以 root 服务运行。
**请务必用防火墙把 `8766/tcp` 限制到可信管理 IP**；有域名时优先使用安装脚本集成的
Let's Encrypt 证书。管理账号状态位于 `/etc/linux-autobook/admin-state.json`，
删除该文件并重启 `autobook-admin` 即可重置为 `admin/admin`。

安装脚本还会注册一个 `autobook` 命令：

```bash
sudo autobook                      # 安装 / 更新 / 状态 / 卸载 菜单
sudo autobook --action status
sudo autobook --action update
```

### 任务网站

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SITE_BASE_URL` | `https://544544.xyz` | 文献传递任务网站根地址 |
| `WORKER_TOKEN` | 空 | Worker 接口令牌，必填 |
| `WORKER_ID` | `linux-worker-1` | 当前 Worker 的唯一标识 |
| `WORKER_QUEUE` | `pdf` | 领取队列：`pdf`、`ocr` 或网站支持的其他值 |
| `POLL_SECONDS` | `15` | 队列为空或领取失败后的等待秒数 |
| `CONCURRENCY` | `3` | 同时处理的任务上限，最小值为 1 |
| `LEASE_HEARTBEAT_SECONDS` | `60` | Worker 续租间隔；必须明显小于网站的 300 秒租约 |

### 百度下载网关（普通 Worker）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BAIDU_GATEWAY_URL` | 空 | 中心网关 HTTPS 地址；设置后 Worker 不读取百度 Cookie |
| `BAIDU_GATEWAY_TOKEN` | 空 | Worker 与网关共享的高强度随机令牌 |
| `BAIDU_GATEWAY_CA_FILE` | 单机自签名时为 `runtime/gateway.crt` | 自签名/私有 CA 的证书路径；公有 CA 证书留空并使用系统信任库 |
| `BAIDU_GATEWAY_TIMEOUT_SECONDS` | `7200` | 单次检索下载总等待时间 |
| `BAIDU_GATEWAY_POLL_SECONDS` | `3` | 网关任务状态轮询间隔 |

### 百度网盘

以下凭据只配置在中心网关。普通 Worker 不需要也不应复制这些值。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BAIDU_AUTH_FILE` | `runtime/baidu_credentials.json` | 扫码登录后保存的 Cookie 文件 |
| `BAIDU_QR_PATH` | `runtime/baidu-login-qr.png` | 登录二维码输出位置 |
| `BAIDU_QR_TIMEOUT_SECONDS` | `120` | 等待扫码确认的秒数，最低 30 |
| `BAIDU_PROXY` | 空 | 登录阶段可选代理，如 `socks5h://127.0.0.1:10808` |
| `BAIDU_BDUSS` / `BAIDU_STOKEN` | 空 | 可选手工 Cookie；推荐使用扫码文件 |
| `BAIDU_BAIDUID` | 空 | 可选 BAIDUID Cookie |
| `BAIDU_GROUP_NAME` | `读秀12群` | 未填写 gid 时按群名解析 |
| `BAIDU_GROUP_GID` | 空 | 目标群 gid；已知时建议直接填写 |
| `BAIDU_SAVE_DIR` | `/autobook_inbox` | 转存到个人网盘的临时目录，会自动清理 |
| `DOWNLOAD_UA` | 桌面网盘 UA | 下载请求 User-Agent，通常无需修改 |
| `ARIA2_SPLIT` | `16` | aria2 分片数 |
| `ARIA2_MAX_CONNECTION` | `16` | aria2 单服务器连接上限 |
| `DOWNLOAD_TIMEOUT_SECONDS` | `1800` | 下载、解压等长操作超时 |

`BAIDU_SAVE_DIR` 必须是专用目录。网关按 job ID 使用独立子目录并在下载后清理，不要在该目录存放个人文件。

### 网关服务端

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GATEWAY_BIND` / `GATEWAY_PORT` | `127.0.0.1` / `8765` | 网关监听地址与端口；跨主机时设 `0.0.0.0` 并配置防火墙 |
| `GATEWAY_TLS_CERT` / `GATEWAY_TLS_KEY` | `runtime/gateway.crt/key` | TLS 证书与私钥 |
| `GATEWAY_CONCURRENCY` | `3` | 同时进行的百度检索/下载数 |
| `GATEWAY_CACHE_TTL_SECONDS` | `3600` | Worker 未取走结果时的缓存寿命 |
| `GATEWAY_JOB_ROOT` | `runtime/gateway/jobs` | 网关隔离任务目录 |

### 本地路径

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WORK_ROOT` | `runtime/work` | 解压和生成 PDF 的临时目录 |
| `DOWNLOAD_ROOT` | `runtime/downloads` | 百度下载目录 |
| `INDEX_DB` | `runtime/library_index.sqlite3` | 搜索结果缓存数据库 |
| `PASSWORD_DICT` | `password.txt` | 每行一个解压密码；程序会先尝试空密码 |
| `SEVEN_ZIP_BIN` | `7z` | 7-Zip 命令路径 |
| `ARIA2C_BIN` | `aria2c` | aria2 命令路径 |
| `FULL_SYNC_MAX_PAGES` | `2000` | 仅遗留全量索引诊断的最大页数 |

### Cloudreve 结果网盘

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DRIVE_EMAIL` | 空 | Cloudreve 登录邮箱，必填 |
| `DRIVE_PASSWORD` | 空 | Cloudreve 登录密码，必填 |
| `DRIVE_BASE_URL` | `https://drive.netupdown.com` | Cloudreve 根地址 |
| `DRIVE_POLICY_ID` | 空 | 可选存储策略 ID；留空使用服务端默认值 |
| `DRIVE_TARGET_DIR` | `transfer` | 上传目标目录 |
| `DRIVE_EXPIRE_DAYS` | `7` | 分享链接有效天数 |
| `DRIVE_REQUIRE_UPLOAD_DATE_VERIFY` | `1` | 上传后必须验证文件日期，避免异常的 1970 时间 |

### PDF

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PDG_DPI` | `200` | 将 PDG 页面封装为 PDF 时使用的 DPI |

## 百度扫码登录

```bash
cd /opt/autobook-linux
.venv/bin/python run_worker.py --baidu-login
```

命令会：

1. 创建二维码 PNG；
2. 输出二维码文件路径和可在浏览器打开的图片地址；
3. 等待使用百度网盘 App 扫码并在手机确认；
4. 保存 Cookie 到 `BAIDU_AUTH_FILE`，权限设为 `0600`；
5. 验证网盘登录态和目标群组可见性。

无桌面服务器可以把二维码文件通过安全通道下载到本机查看。Cookie 失效后重新执行扫码登录即可，不需要改代码。

扫码取得的是访问群文件库网页接口需要的百度 Passport 登录态。普通 OAuth/OpenAPI access token 不等同于群文件库登录态，无法直接替代这一步。

## 运行与预检

```bash
cd /opt/autobook-linux

# 检查配置、百度登录态、目标群组、临时转存目录和 PDG 解码器
.venv/bin/python run_worker.py --check

# 测试：最多领取一个任务，完成后退出
.venv/bin/python run_worker.py --once

# 生产：持续轮询并按 CONCURRENCY 并发处理
.venv/bin/python run_worker.py
```

## 分布式部署与 systemd 常驻

仓库提供网关和 Worker 两个服务，默认以权限受限的 `autobook` 用户运行。中心节点选择“仅百度下载网关”：

```bash
sudo bash install.sh
# 选择：安装或重新配置 -> 仅百度下载网关
```

在网关面板完成百度扫码和预检后启动网关。网关 TLS 证书必须包含实际域名或 IP 的 SAN；推荐在安装菜单输入域名，让 Certbot 自动签发和续期。自签名部署需要把公钥证书安全复制给每台 Worker，私钥只留在网关。

```bash
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
  -keyout runtime/gateway.key -out runtime/gateway.crt \
  -subj '/CN=gateway.example' -addext 'subjectAltName=DNS:gateway.example'
chmod 600 runtime/gateway.key
.venv/bin/python run_gateway.py --check
```

每台计算节点选择“仅 Worker”，使用独立 `WORKER_ID`；它只保存任务网站、网关和 Cloudreve 配置，不含百度 Cookie：

```bash
sudo bash install.sh
# 选择：安装或重新配置 -> 仅 Worker
# 输入中心网关 URL、共享令牌；公有证书的 CA 路径留空
```

更新、查看状态或卸载均重新运行菜单：

```bash
sudo bash /opt/autobook-linux/install.sh
```

“更新”会从 GitHub 取得干净源码，再调用相同安装流程；角色、配置、管理密码、百度凭据和 runtime 数据均保留。“卸载”默认先创建权限为 `0600` 的 `/var/backups/linux-autobook/backup-*.tar.gz`，非交互彻底卸载可使用 `--action uninstall --purge --non-interactive`。

停止自动领取任务：

```bash
sudo systemctl disable --now autobook-worker.service
```

不要让两台主机使用相同 `WORKER_ID`。网站插件文件位于 `site_plugin/`，部署说明见该目录 README。租约令牌同时绑定任务、Worker ID 和处理状态；旧 Worker 的迟到回调会被拒绝。

确认本机进程：

```bash
pgrep -af '[r]un_worker.py'
```

## 搜索与索引

正常任务会调用群文件库的服务端搜索接口，响应速度与客户端搜索接近。SQLite 的主要作用是缓存已经搜索到的文件元数据，减少重复查询，并在多个同 SS 号结果中稳定选择优先级更高的文件：PDF 优先，其次是已支持归档。

不需要为了正常搜索而递归遍历全部群文件。对数百万乃至上千万文件执行全量递归索引耗时长、容易触发限流，也会产生庞大且很快过期的本地数据。

项目保留以下诊断命令，但不建议对大型群文件库使用 `--full`：

```bash
.venv/bin/python run_worker.py --sync-index
.venv/bin/python run_worker.py --sync-index --full
```

## 下载回退策略

1. 使用群文件服务端搜索返回的短期 dlink；
2. aria2 按配置并发分段下载；
3. aria2 失败时改用 4 MiB 顺序 HTTP Range，并逐段校验 `Content-Range` 和字节数；
4. 群直链整体失败时，将文件转存到 `BAIDU_SAVE_DIR`，等待个人网盘文件出现后重新取得 dlink；
5. 下载后校验文件存在、非空且大小与百度元数据完全一致；
6. 无论成功或失败，清理个人网盘临时转存和本地任务目录。

短期 dlink 和签名查询参数不会写入最终任务结果。日志会隐藏 aria2 错误输出中的下载 URL。

## 密码字典

仓库不包含部署机器上的真实 `password.txt`。首次安装可复制示例：

```bash
cp password.example.txt password.txt
chmod 600 password.txt
```

每行写一个候选密码。程序总是先测试空密码，然后按文件顺序逐项运行 `7z t`，命中后才执行 `7z x`。请只加入公开的文献包密码，不要把个人账号密码放入该文件。

## 测试

```bash
cd /opt/autobook-linux
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖：

- SS 号提取和输出文件名；
- 群文件库搜索签名、结果过滤和映射；
- 百度扫码凭据保存/读取；
- 4 MiB Range 下载重组；
- 归档扩展名、魔数和大小写识别；
- SQLite 索引选择优先级；
- 03H/11H PDG 兼容处理；
- 页面解码失败时禁止生成空白占位页。

单元测试不会替代真实账号、群组、百度 CDN、任务网站和 Cloudreve 的端到端测试。

## 日志与运行状态

systemd 模式：

```bash
journalctl -u autobook-worker.service --since today
journalctl -u autobook-worker.service -f
```

常见正常日志顺序：

```text
百度网盘登录态正常
目标群文件库访问正常
Worker 启动 ... concurrency=3
领取任务 #...
正在群文件库检索 SS=...
生成 PDF
上传到网盘并创建分享链接
任务 #... 完成: https://...
```

## 故障排查

### Worker 启动不了或显示 inactive

先打开管理面板“概况”和“快速设置”。最常见原因是遗漏任务网站 Token、中心网关 URL/令牌或结果网盘账号密码。面板会逐项列出，不完整时主动阻止启动。

```bash
systemctl status autobook-worker.service
journalctl -u autobook-worker.service -n 80 --no-pager
```

配置未就绪会退出 78，并由 `RestartPreventExitStatus=78` 阻止重启风暴；这不是 systemd 故障。填写后在面板点击“保存并启动 Worker”，或先到“工具与诊断”运行预检。

公有 CA 证书网关应把 `BAIDU_GATEWAY_CA_FILE` 留空。只有自签名/私有 CA 才填写证书路径；填写了不存在的路径也会被面板明确拦截。

### 缺少百度登录凭据

错误包含“缺少配置：百度登录凭据”时执行：

```bash
.venv/bin/python run_worker.py --baidu-login
```

### 能登录但看不到群组

- 确认扫码账号已经加入目标群；
- 优先在 `.env` 填写准确的 `BAIDU_GROUP_GID`；
- 执行 `run_worker.py --check` 查看预检结果。

### 百度下载 HTTP 403

Worker 会先自动尝试 4 MiB Range 回退和个人网盘转存。若最终仍失败：

- 检查 Cookie 是否过期；
- 检查 VPS 到百度 CDN 的网络；
- 降低 `ARIA2_SPLIT` 和 `ARIA2_MAX_CONNECTION`；
- 重新扫码登录后运行 `--check`。

### RAR 无法打开

检查 7-Zip 是否包含 RAR/RAR5 解码器，并确认密码字典：

```bash
7z i | grep -E 'Rar|Rar5'
7z t -p'候选密码' /path/to/book.rar
```

### 压缩包内未发现 PDF 或 PDG

可能原因包括：压缩密码不在字典中、嵌套层数超过限制、页面采用未支持格式，或压缩包本身损坏。Worker 会保留任务错误信息，但会删除本地中间目录。

### PDG 页面解码失败

这是完整性保护机制。请保留失败文件的格式字节、页面编号和脱敏日志用于添加兼容处理，不要把失败改成空白页继续上传。

### Cloudreve 上传失败

- 检查 `DRIVE_BASE_URL`、账号和密码；
- 让 `DRIVE_POLICY_ID` 留空以使用服务端默认存储策略；
- 检查服务器时间是否正确；
- 确认目标目录存在或账号有创建权限。

## 安全说明

- `.env`、`runtime/`、`password.txt`、Cookie、二维码、PDF 和下载文件均由 `.gitignore` 排除。
- 扫码 Cookie 文件使用 `0600` 权限；请同时限制项目目录和服务器 SSH 权限。
- 不要在 Issue、日志或截图中发布 `WORKER_TOKEN`、BDUSS、STOKEN、Cloudreve 密码、临时 dlink 或任务 token。
- `BAIDU_GATEWAY_TOKEN` 也属于高敏感凭据；网关必须使用 HTTPS，Worker 必须校验证书。
- 用防火墙把网关端口限制到 Worker 出口 IP；令牌泄露时同时轮换网关和所有 Worker 的环境文件。
- 任务网站接口、百度群文件接口均应启用 HTTPS。
- 分享链接本身代表临时读取权限，请按需缩短 `DRIVE_EXPIRE_DAYS`。

## 项目结构

```text
.
├── autobook_linux/
│   ├── archive.py           # 归档识别、密码测试和解压
│   ├── baidu_auth.py        # 百度二维码登录与凭据存储
│   ├── baidu_pan.py         # 群搜索、转存、下载和回退
│   ├── config.py            # .env 配置
│   ├── gateway_client.py    # Worker 侧 HTTPS 下载客户端
│   ├── gateway_server.py    # 中心百度下载网关
│   ├── library_index.py     # 搜索结果 SQLite 缓存与选择
│   ├── pdg_crypto.py        # 03H/11H 兼容转换
│   ├── pipeline.py          # 单任务端到端流水线
│   ├── site_client.py       # 任务网站 Worker API
│   ├── worker.py            # 并发领取和线程池
│   ├── panel/               # Web 管理面板（纯标准库）
│   │   ├── server.py        # HTTP 路由、会话与 JSON API
│   │   ├── schema.py        # 配置项定义、说明文字与校验规则
│   │   ├── diagnostics.py   # 就绪判定与真实连通性检测
│   │   ├── services.py      # systemd 状态、控制与日志脱敏
│   │   ├── maintenance.py   # 一键维护动作与任务记录还原
│   │   ├── jobs.py          # 后台任务与 systemd 托管执行
│   │   ├── baidu.py         # 扫码登录流程
│   │   ├── auth.py          # 口令、会话与登录限速
│   │   ├── passwords.py     # 解压密码字典的读写与增删改
│   │   ├── nodes.py         # 节点注册表、接入码与指纹固定的节点客户端
│   │   ├── supervisor.py    # 容器内的进程管理器（systemd 的替代）
│   │   ├── data/            # 内置解压密码字典
│   │   └── static/          # 单页前端（HTML/CSS/JS）
│   └── vendor/
│       ├── pdg2pdf.py       # PDG 转换器
│       ├── pdg-decoder.wasm # PDG WASM 解码器
│       └── upload_to_drive.py
├── deploy/
│   ├── autobook-admin.service
│   ├── autobook-cert-deploy.sh # Let's Encrypt 续期部署钩子
│   ├── autobook-gateway.service
│   ├── autobook-worker.service
│   └── examples/
├── docker/
│   ├── build.sh             # 多架构镜像构建脚本
│   ├── entrypoint.sh        # 容器首次启动的自动配置
│   └── .env.example
├── docs/
│   ├── 管理面板使用说明.md   # 面板完整操作手册
│   └── Docker部署说明.md     # 容器部署手册
├── site_plugin/             # 544544.xyz 原子租约插件文件
├── tests/
├── .env.example
├── .gitignore
├── password.example.txt
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── install.sh
├── run_admin.py
├── run_worker.py
└── run_gateway.py
```

## 第三方组件与接口说明

- `vendor/pdg2pdf.py` 与 `vendor/pdg-decoder.wasm` 基于 MIT 许可项目 [bj5/pdg2pdf_open](https://github.com/bj5/pdg2pdf_open)，本项目在其上增加了兼容与完整性保护。
- 可选受信任证书由开源 [Certbot](https://github.com/certbot/certbot) 通过 Let’s Encrypt ACME 服务签发和自动续期；未输入域名时不安装 Certbot。
- 百度群文件库使用的是网页/桌面客户端相关接口，不是稳定承诺的公开 OpenAPI；百度更新接口后可能需要适配。
- Cloudreve 上传实现面向 v4 API；不同实例的存储策略、权限和返回字段可能不同。

## 维护建议

- 每次更新依赖或百度下载逻辑后运行全部测试和至少一个真实小文件任务。
- 每次修改 PDG 解码逻辑后验证页数、非空页面数和页面图像对象，不只检查“PDF 文件存在”。
- 对并发升级进行 2、3、N 任务逐级压测，观察 CPU、内存、磁盘和百度限流，再调整 `CONCURRENCY`。
- 定期备份 `.env` 和扫码凭据，但不要备份到公开 Git 仓库。

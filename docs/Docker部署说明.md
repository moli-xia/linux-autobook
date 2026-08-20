# linux-autobook Docker 部署说明

镜像同时支持 **linux/amd64** 与 **linux/arm64**（x86 服务器和 ARM 服务器都能跑）。
一个容器里包含管理面板、百度下载网关和任务 Worker，三者由面板内置的进程管理器托管，
可以在网页上随时启停，不需要 systemd。

---

## 目录

1. [三条命令跑起来](#三条命令跑起来)
2. [镜像里有什么](#镜像里有什么)
3. [环境变量](#环境变量)
4. [三种部署形态](#三种部署形态)
5. [数据、备份与升级](#数据备份与升级)
6. [自己构建多架构镜像](#自己构建多架构镜像)
7. [常见问题](#常见问题)

---

## 三条命令跑起来

前提：服务器已安装 Docker（没有的话 `curl -fsSL https://get.docker.com | sh`）。

```bash
git clone https://github.com/moli-xia/linux-autobook.git && cd linux-autobook
cp docker/.env.example .env && sed -i 's/^AUTOBOOK_PUBLIC_HOST=.*/AUTOBOOK_PUBLIC_HOST=你的服务器IP/' .env
docker compose up -d
```

浏览器打开 `https://你的服务器IP:8766/`，用 `admin` / `admin` 登录，
按左侧「配置向导」走一遍即可。自签名证书会让浏览器提示风险，点「继续访问」。

首次启动容器会自动完成这些事，**不需要你手工做**：

- 生成面板和网关两套自签名证书（含服务器 IP 的 SAN）
- 生成一个 64 位随机的网关共享令牌
- 写好 gateway.env / worker.env 的全部默认值
- 用程序内置的 307 条常见解压密码初始化密码字典

---

## 镜像里有什么

| 组件 | 说明 |
| --- | --- |
| Python 3.12 + 项目依赖 | 独立 venv，位于 `/opt/autobook-linux/.venv` |
| `7z`（p7zip-full） | Debian 的 p7zip-full 就是上游 7-Zip 26.x，**内置 Rar / Rar5 解压支持** |
| `aria2c` | 百度网盘多线程下载 |
| `openssl` | 首次启动生成自签名证书 |
| 管理面板 | 监听 8766，HTTPS |
| 百度下载网关 | 监听 8765，HTTPS，仅网关角色启用 |
| 任务 Worker | 无监听端口，仅 Worker 角色启用 |

容器以 root 运行：容器本身已经是隔离边界，且挂载卷不需要再对齐宿主机 uid。

---

## 环境变量

写在项目根目录的 `.env` 里（`docker compose` 会自动读取）。

### 必填

| 变量 | 说明 |
| --- | --- |
| `AUTOBOOK_PUBLIC_HOST` | 服务器公网 IP 或域名。会写进自签名证书，也是面板显示的访问地址。**不填容器不会启动。** |

### 常用

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTOBOOK_ROLE` | `all` | `all` = 网关 + Worker（主服务器）；`gateway` = 仅网关；`worker` = 仅 Worker |
| `AUTOBOOK_IMAGE` | `ghcr.io/moli-xia/linux-autobook:latest` | 用自建镜像时改这里 |
| `ADMIN_PORT` | `8766` | 管理面板端口 |
| `GATEWAY_PORT` | `8765` | 下载网关端口，仅网关角色需要对外放行 |
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志时间戳 |

### 首次初始化用（之后请在面板里改）

这些变量只在配置文件里还没有对应键时才写入，**改了它们不会覆盖你在面板里做的修改**。

| 变量 | 说明 |
| --- | --- |
| `AUTOBOOK_SITE_URL` | 任务网站地址 |
| `AUTOBOOK_WORKER_TOKEN` | 任务网站 Worker 令牌 |
| `AUTOBOOK_WORKER_ID` | Worker 名称，多台不能重复 |
| `AUTOBOOK_CONCURRENCY` | 并发任务数，每个任务约占 1 核 1 GB |
| `AUTOBOOK_DRIVE_URL` / `AUTOBOOK_DRIVE_EMAIL` / `AUTOBOOK_DRIVE_PASSWORD` | 结果网盘 |
| `AUTOBOOK_GATEWAY_TOKEN` | 网关共享令牌。不填时网关角色会自动随机生成 |
| `AUTOBOOK_GATEWAY_URL` / `AUTOBOOK_GATEWAY_CA_FILE` | 仅 `worker` 角色需要：中心网关地址与证书路径 |

---

## 三种部署形态

### 形态一：单机主服务器（`AUTOBOOK_ROLE=all`）

一个容器同时跑网关和 Worker，Worker 通过 `https://127.0.0.1:8765` 连本机网关，
证书路径自动配好。适合大多数场景。

```env
AUTOBOOK_ROLE=all
AUTOBOOK_PUBLIC_HOST=1.2.3.4
```

部署后进面板 →「百度登录」扫码 → 启动网关和 Worker。

### 形态二：中心网关（`AUTOBOOK_ROLE=gateway`）

只跑网关，供多台 Worker 调用。

```env
AUTOBOOK_ROLE=gateway
AUTOBOOK_PUBLIC_HOST=1.2.3.4
```

部署后进面板扫码登录，然后在「配置向导」里点「显示」复制**网关共享令牌**，
再把网关证书导出给每台 Worker：

```bash
docker exec autobook cat /opt/autobook-linux/runtime/gateway.crt > gateway.crt
```

别忘了在云厂商安全组放行 TCP 8765。

### 形态三：计算节点（`AUTOBOOK_ROLE=worker`）

只跑 Worker，可以部署任意多台。

```env
AUTOBOOK_ROLE=worker
AUTOBOOK_PUBLIC_HOST=5.6.7.8
AUTOBOOK_GATEWAY_URL=https://1.2.3.4:8765
AUTOBOOK_GATEWAY_TOKEN=从网关面板复制的令牌
AUTOBOOK_GATEWAY_CA_FILE=/opt/autobook-linux/runtime/gateway.crt
```

把上面导出的 `gateway.crt` 放进容器的 runtime 卷：

```bash
docker cp gateway.crt autobook:/opt/autobook-linux/runtime/gateway.crt
docker restart autobook
```

> 容器和 systemd 两种部署可以混用：网关用 Docker、Worker 用 `install.sh`，或者反过来，
> 它们之间只通过 HTTPS + 共享令牌通信，互不感知对方怎么部署。

---

## 数据、备份与升级

配置和运行数据都在具名卷里，重建容器不会丢：

| 卷 | 挂载点 | 内容 |
| --- | --- | --- |
| `autobook-config` | `/etc/linux-autobook` | 全部配置、证书、面板账号 |
| `autobook-runtime` | `/opt/autobook-linux/runtime` | 百度登录凭据、密码字典、日志、临时文件 |

**备份**：面板「维护 → 备份配置」，或者在宿主机上

```bash
docker run --rm -v autobook-docker_autobook-config:/c -v $PWD:/out alpine \
  tar czf /out/autobook-config-$(date +%F).tar.gz -C /c .
```

**升级**：

```bash
docker compose pull && docker compose up -d
```

配置和数据都在卷里，升级后原样保留。（面板里的「更新程序」按钮只适用于 systemd 部署，
容器部署会提示你用上面这两条命令。）

**查看面板自身日志**：`docker logs -f autobook`。
网关和 Worker 的日志在面板「运行日志」页里看，也在 `runtime/logs/` 下。

---

## 自己构建多架构镜像

```bash
# 推送到你自己的仓库（同时构建 amd64 与 arm64）
IMAGE=ghcr.io/你的用户名/autobook:latest ./docker/build.sh

# 只构建本机架构并加载到本地 docker
PLATFORMS=linux/amd64 PUSH=0 ./docker/build.sh

# 构建多架构但不推送，导出成 OCI 归档
PUSH=0 ./docker/build.sh   # 产物：autobook-multiarch.tar
```

脚本会自动注册 QEMU（`tonistiigi/binfmt`）并创建 buildx builder。
构建过程中会在镜像里跑一遍完整单元测试，测试不过就不会出镜像。

---

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `AUTOBOOK_PUBLIC_HOST` 报错、容器起不来 | `.env` 里必须填服务器 IP 或域名 |
| 浏览器提示证书不安全 | 自签名证书的正常表现，点「继续访问」；或用反向代理配置正式证书 |
| 换了服务器 IP，面板打不开 | 改 `.env` 的 `AUTOBOOK_PUBLIC_HOST` 后 `docker compose up -d`，证书会自动重新生成 |
| 网关起不来 | 面板「百度登录」扫码；未登录时网关不会启动 |
| Worker 连不上网关 | 检查安全组是否放行 8765、令牌两边是否一致、`gateway.crt` 是否已复制到 Worker |
| 想改角色 | 面板「维护 → 本机角色」直接切换，容器模式下不需要重装；也可以改 `.env` 后 `docker compose up -d` |
| 容器内存不够、任务失败 | 面板「配置 → 并发任务数」调低，每个并发约需 1 核 1 GB |
| 想进容器排查 | `docker exec -it autobook bash` |

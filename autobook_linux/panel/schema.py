"""Declarative description of every configuration value the panel manages.

Each field carries enough metadata for the front-end to render a labelled
control, a plain-language explanation, an example value, and validation hints,
so operators never have to consult the README to fill the form in.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    id: str
    target: str          # gateway | worker
    title: str
    summary: str
    essential: bool = True   # shown in the basic view, not only under "advanced"


@dataclass(frozen=True)
class Field:
    key: str
    target: str          # gateway | worker | both
    group: str
    label: str
    kind: str = "text"   # text | password | number | select | url | path
    default: str = ""
    options: tuple[tuple[str, str], ...] = ()
    help: str = ""
    example: str = ""
    essential: bool = False   # part of the minimal set required to start
    required: bool = False
    placeholder: str = ""
    unit: str = ""
    min_value: int | None = None
    max_value: int | None = None
    keep_blank: bool = False  # a blank value is meaningful and must be preserved


GROUPS: tuple[Group, ...] = (
    Group("site", "worker", "任务网站", "Worker 从这里领取任务、上报进度并交付结果。"),
    Group("gateway_client", "worker", "下载网关连接", "Worker 不直接接触百度账号，所有下载都通过中心网关完成。"),
    Group("drive", "worker", "结果网盘", "成品 PDF 上传到 Cloudreve 网盘并生成分享链接。"),
    Group("cleanup", "worker", "存储清理", "自动删除已过期的交付文件，避免网盘被无限占满。", essential=False),
    Group("processing", "worker", "处理参数", "并发、清晰度等影响速度与资源占用的参数。", essential=False),
    Group("paths", "worker", "本地路径与命令", "工作目录和外部命令位置，通常保持默认。", essential=False),
    Group("gateway_server", "gateway", "网关服务", "中心网关自身的监听地址、证书与并发。"),
    Group("baidu_account", "gateway", "百度账号", "网关使用的百度网盘登录态与目标群组。"),
    Group("baidu_download", "gateway", "百度下载参数", "下载线程数与超时，影响下载速度和稳定性。", essential=False),
)


FIELDS: tuple[Field, ...] = (
    # ---------------------------------------------------------------- worker
    Field(
        "SITE_BASE_URL", "worker", "site", "任务网站地址", kind="url",
        default="https://544544.xyz", essential=True, required=True,
        help="发布文献传递任务的网站根地址，必须包含 https:// 且结尾不要带斜杠或路径。",
        example="https://544544.xyz",
    ),
    Field(
        "WORKER_TOKEN", "worker", "site", "Worker 令牌", kind="password",
        essential=True, required=True,
        help="网站后台「文献传递 → Worker 管理」里生成的令牌，用来证明本机有权领取任务。填错会一直领不到任务。",
        example="约 32 位随机字符",
    ),
    Field(
        "WORKER_ID", "worker", "site", "Worker 名称", essential=True, required=True,
        help="本机在网站上的显示名称，多台机器不能重复，方便区分任务由哪台服务器处理。",
        example="hk-node-1",
    ),
    Field(
        "WORKER_QUEUE", "worker", "site", "领取的任务队列", kind="select", default="pdf",
        options=(("pdf", "pdf — 仅普通 PDF 任务"), ("ocr", "ocr — 仅 OCR 任务"), ("all", "all — 全部任务")),
        essential=True,
        help="限制本机领取哪一类任务。只有一台机器时建议选 all；多台机器分工时按类型拆分。",
    ),
    Field(
        "POLL_SECONDS", "worker", "site", "空闲轮询间隔", kind="number", default="15", unit="秒",
        min_value=5, max_value=600,
        help="没有任务时每隔多久去网站问一次。调小响应更快但请求更频繁，建议 10–30 秒。",
    ),
    Field(
        "LEASE_HEARTBEAT_SECONDS", "worker", "site", "任务续租心跳", kind="number", default="60", unit="秒",
        min_value=15, max_value=600,
        help="处理中的任务每隔多久向网站续租一次，防止被判超时后分配给别的机器。",
    ),
    Field(
        "BAIDU_GATEWAY_URL", "worker", "gateway_client", "网关地址", kind="url",
        essential=True, required=True,
        help="中心下载网关的 HTTPS 地址，格式为 https://主机:端口。本机同时也是网关时填 https://127.0.0.1:8765。",
        example="https://45.142.166.74:8765",
    ),
    Field(
        "BAIDU_GATEWAY_TOKEN", "both", "gateway_client", "网关共享令牌", kind="password",
        essential=True, required=True,
        help="网关与所有 Worker 必须使用完全相同的令牌。在网关机器的「网关服务」分组里复制过来即可。",
        example="64 位十六进制随机串",
    ),
    Field(
        "BAIDU_GATEWAY_CA_FILE", "worker", "gateway_client", "网关证书文件", kind="path",
        essential=True, keep_blank=True,
        help="网关使用自签名证书时，把网关机器上的 gateway.crt 复制到本机并填写路径；网关使用域名和公有证书时请留空。",
        example="/opt/autobook-linux/runtime/gateway.crt",
        placeholder="公有证书请留空",
    ),
    Field(
        "BAIDU_GATEWAY_TIMEOUT_SECONDS", "worker", "gateway_client", "下载总超时", kind="number",
        default="7200", unit="秒", min_value=300, max_value=86400,
        help="单本书从网关取回的最长等待时间，超大书籍可适当调高。",
    ),
    Field(
        "BAIDU_GATEWAY_POLL_SECONDS", "worker", "gateway_client", "网关状态轮询间隔", kind="number",
        default="3", unit="秒", min_value=1, max_value=60,
        help="等待网关下载时的查询间隔，一般不用改。",
    ),
    Field(
        "DRIVE_BASE_URL", "worker", "drive", "网盘地址", kind="url",
        default="https://drive.netupdown.com", essential=True, required=True,
        help="Cloudreve 网盘的根地址，成品会上传到这里并生成分享链接。",
        example="https://drive.netupdown.com",
    ),
    Field(
        "DRIVE_EMAIL", "worker", "drive", "网盘账号", essential=True, required=True,
        help="Cloudreve 登录邮箱，多台 Worker 可以共用同一个账号。",
        example="delivery@example.com",
    ),
    Field(
        "DRIVE_PASSWORD", "worker", "drive", "网盘密码", kind="password", essential=True, required=True,
        help="Cloudreve 登录密码。保存后不再显示，留空提交表示保持原值不变。",
    ),
    Field(
        "DRIVE_TARGET_DIR", "worker", "drive", "上传目录", default="transfer", essential=True,
        help="成品在网盘中的存放目录名，不存在时会自动创建。",
        example="transfer",
    ),
    Field(
        "DRIVE_EXPIRE_DAYS", "worker", "drive", "分享有效期", kind="number", default="7", unit="天",
        min_value=1, max_value=365,
        help="生成的分享链接多少天后失效。",
    ),
    Field(
        "CLEANUP_ENABLED", "both", "cleanup", "自动清理", kind="select",
        default="1", options=(("1", "开启（推荐）"), ("0", "关闭")),
        help="定期删除分享已过期的成品文件和百度转存目录里的残留文件。关闭后网盘会持续增长，需要自己手动清理。",
    ),
    Field(
        "CLEANUP_INTERVAL_HOURS", "both", "cleanup", "清理间隔", kind="number",
        default="6", unit="小时", min_value=1, max_value=720,
        help="每隔多久自动执行一次清理。服务启动 2 分钟后先跑一次。",
    ),
    Field(
        "DRIVE_CLEANUP_GRACE_DAYS", "worker", "cleanup", "过期宽限期", kind="number",
        default="1", unit="天", min_value=0, max_value=90,
        help="分享失效后再多保留几天才删除，防止时钟误差导致仍然有效的链接被提前删掉。",
    ),
    Field(
        "DRIVE_POLICY_ID", "worker", "drive", "存储策略 ID", keep_blank=True,
        help="Cloudreve 配置了多个存储策略时才需要指定，绝大多数情况留空即可。",
    ),
    Field(
        "DRIVE_REQUIRE_UPLOAD_DATE_VERIFY", "worker", "drive", "校验上传日期", kind="select",
        default="1", options=(("1", "开启（推荐）"), ("0", "关闭")),
        help="上传后回查文件日期，确认拿到的是本次新上传的文件而不是旧的同名文件。",
    ),
    Field(
        "CONCURRENCY", "worker", "processing", "并发任务数", kind="number", default="3",
        essential=True, min_value=1, max_value=16,
        help="同时处理几本书。每个任务大约占用 1 个 CPU 核心和 1 GB 内存，建议不要超过 CPU 核心数。",
        example="1 核 2G 内存填 1；4 核 8G 填 3",
    ),
    Field(
        "PDG_DPI", "worker", "processing", "输出清晰度", kind="number", default="200", unit="DPI",
        min_value=72, max_value=600,
        help="PDG 转 PDF 的分辨率。200 已经很清晰；调到 300 体积和耗时都会明显增加。",
    ),
    Field(
        "WORK_ROOT", "worker", "paths", "任务工作目录", kind="path",
        default="/opt/autobook-linux/runtime/work",
        help="解压和转换的临时目录，任务结束后自动清理，需要有足够剩余空间。",
    ),
    Field(
        "DOWNLOAD_ROOT", "worker", "paths", "下载目录", kind="path",
        default="/opt/autobook-linux/runtime/downloads",
        help="从网关取回的压缩包暂存目录。",
    ),
    Field(
        "INDEX_DB", "worker", "paths", "本地索引数据库", kind="path",
        default="/opt/autobook-linux/runtime/library_index.sqlite3",
        help="直连百度模式下缓存群文件索引；网关模式下不会使用。",
    ),
    Field(
        "PASSWORD_DICT", "worker", "paths", "解压密码字典", kind="path",
        default="/opt/autobook-linux/password.txt",
        help="逐个尝试解压加密压缩包的候选密码，可在「维护」页面直接编辑内容。",
    ),
    Field(
        "SEVEN_ZIP_BIN", "worker", "paths", "7-Zip 命令", default="7z",
        help="解压命令。系统已安装 p7zip-full 时保持 7z 即可。",
    ),
    Field(
        "ARIA2C_BIN", "worker", "paths", "aria2c 命令", default="aria2c",
        help="多线程下载命令。系统已安装 aria2 时保持 aria2c 即可。",
    ),
    # --------------------------------------------------------------- gateway
    Field(
        "GATEWAY_BIND", "gateway", "gateway_server", "监听地址", default="0.0.0.0", essential=True,
        help="0.0.0.0 表示允许其它服务器上的 Worker 连接；只给本机使用可填 127.0.0.1。",
    ),
    Field(
        "GATEWAY_PORT", "gateway", "gateway_server", "监听端口", kind="number", default="8765",
        essential=True, min_value=1, max_value=65535,
        help="网关端口，需要在防火墙或安全组放行 TCP。所有 Worker 的网关地址都要写这个端口。",
    ),
    Field(
        "GATEWAY_CONCURRENCY", "gateway", "gateway_server", "百度下载并发数", kind="number",
        default="3", essential=True, min_value=1, max_value=16,
        help="网关同时下载几本书。受百度限速影响，一般 3–5 之间效果最好。",
    ),
    Field(
        "GATEWAY_CACHE_TTL_SECONDS", "gateway", "gateway_server", "完成文件缓存时长", kind="number",
        default="3600", unit="秒", min_value=300, max_value=86400,
        help="下载完成的文件在网关上保留多久，方便 Worker 取回失败后重试。",
    ),
    Field(
        "GATEWAY_TLS_CERT", "gateway", "gateway_server", "TLS 证书", kind="path",
        default="/opt/autobook-linux/runtime/gateway.crt",
        help="网关的 HTTPS 证书。安装脚本会自动生成自签名证书，一般无需修改。",
    ),
    Field(
        "GATEWAY_TLS_KEY", "gateway", "gateway_server", "TLS 私钥", kind="path",
        default="/opt/autobook-linux/runtime/gateway.key",
        help="与上面证书配对的私钥文件路径。",
    ),
    Field(
        "GATEWAY_JOB_ROOT", "gateway", "gateway_server", "网关任务目录", kind="path",
        default="/opt/autobook-linux/runtime/gateway/jobs",
        help="网关下载文件的暂存目录，过期后自动清理。",
    ),
    Field(
        "PDG_FALLBACK_ENABLED", "gateway", "gateway_server", "PDG 转换 Wine 兜底", kind="select",
        default="0", options=(("1", "开启"), ("0", "关闭")),
        help="HH 04H 直接调用；其他 PDG 在开放转换器报错、输出无效或页数不完整时启动临时 Pdg2Pic 容器。",
    ),
    Field(
        "PDG_FALLBACK_IMAGE", "gateway", "gateway_server", "Pdg2Pic 容器镜像",
        default="autobook-pdg2pic-wine:local",
        help="本机预先构建的 Wine 转换镜像。每次请求临时启动，转换完成后删除容器。",
    ),
    Field(
        "PDG_FALLBACK_DOCKER_SOCKET", "gateway", "gateway_server", "Docker socket", kind="path",
        default="/var/run/docker.sock",
        help="网关容器内的 Docker Engine socket，用于按需创建转换容器。",
    ),
    Field(
        "PDG_FALLBACK_RUNTIME_VOLUME", "gateway", "gateway_server", "共享运行卷名称",
        keep_blank=True,
        help="Docker 部署时填写映射到 /opt/autobook-linux/runtime 的具名卷。只共享运行数据，不能共享含凭据的配置卷。",
        example="autobook-docker_autobook-runtime",
    ),
    Field(
        "PDG_FALLBACK_JOB_ROOT", "gateway", "gateway_server", "PDG 兜底临时目录", kind="path",
        default="/opt/autobook-linux/runtime/pdg-fallback/jobs",
        help="上传的 PDG 与转换结果所在目录；响应发送完成后自动删除。",
    ),
    Field(
        "PDG_FALLBACK_TIMEOUT_SECONDS", "gateway", "gateway_server", "PDG 兜底转换超时", kind="number",
        default="7200", unit="秒", min_value=60, max_value=86400,
        help="单本 PDG 书籍允许 Pdg2Pic 运行的最长时间。",
    ),
    Field(
        "PDG_FALLBACK_MAX_UPLOAD_MB", "gateway", "gateway_server", "PDG 兜底上传上限", kind="number",
        default="1024", unit="MB", min_value=16, max_value=8192,
        help="Worker 上传到网关的单本 PDG 压缩包大小上限。",
    ),
    Field(
        "PDG_FALLBACK_MEMORY_MB", "gateway", "gateway_server", "转换内存上限", kind="number",
        default="2048", unit="MB", min_value=512, max_value=16384,
        help="每个临时 Wine 容器可使用的最大内存。",
    ),
    Field(
        "PDG_FALLBACK_CPUS", "gateway", "gateway_server", "转换 CPU 上限", kind="number",
        default="2", unit="核", min_value=1, max_value=16,
        help="每个临时 Wine 容器可使用的 CPU 核数。",
    ),
    Field(
        "BAIDU_GROUP_NAME", "gateway", "baidu_account", "目标群名称", default="读秀12群",
        essential=True, keep_blank=True,
        help="资源所在的百度网盘群名称。填了下面的群 GID 时这一项可以留空。",
        example="读秀12群",
    ),
    Field(
        "BAIDU_GROUP_GID", "gateway", "baidu_account", "目标群 GID", essential=True, keep_blank=True,
        help="百度群的数字 ID。填写后可跳过按名称查找，更稳定，推荐填写。",
        example="498636198303058255",
    ),
    Field(
        "BAIDU_SAVE_DIR", "gateway", "baidu_account", "网盘中转目录", default="/autobook_inbox",
        help="下载前先把群文件转存到自己网盘的这个目录，下载完成后自动删除。",
    ),
    Field(
        "BAIDU_INBOX_ORPHAN_HOURS", "gateway", "baidu_account", "残留文件清理时限", kind="number",
        default="6", unit="小时", min_value=1, max_value=720,
        help="中转目录里超过这个时长仍未被删除的文件视为任务中断留下的残留，自动清除。不要小于单个任务可能的最长下载时间。",
    ),
    Field(
        "BAIDU_AUTH_FILE", "gateway", "baidu_account", "扫码凭据文件", kind="path",
        default="/opt/autobook-linux/runtime/baidu_credentials.json",
        help="扫码登录后保存 Cookie 的位置，权限 0600。删除该文件等于退出登录。",
    ),
    Field(
        "BAIDU_PROXY", "gateway", "baidu_account", "百度代理", keep_blank=True,
        help="服务器无法直连百度时才填，支持 http:// 与 socks5h:// 两种格式，大部分机器留空即可。",
        example="socks5h://127.0.0.1:10808",
    ),
    Field(
        "BAIDU_BDUSS", "gateway", "baidu_account", "BDUSS（手工 Cookie）", kind="password", keep_blank=True,
        help="通常不需要填，推荐用扫码登录。只有无法扫码时才从浏览器复制 BDUSS 与 STOKEN，两者必须同时填写。",
    ),
    Field(
        "BAIDU_STOKEN", "gateway", "baidu_account", "STOKEN（手工 Cookie）", kind="password", keep_blank=True,
        help="与 BDUSS 配对使用的 Cookie，同样只在无法扫码时才需要。",
    ),
    Field(
        "BAIDU_QR_TIMEOUT_SECONDS", "gateway", "baidu_account", "扫码等待时长", kind="number",
        default="120", unit="秒", min_value=60, max_value=600,
        help="二维码的有效等待时间，超时后需要重新生成。",
    ),
    Field(
        "ARIA2_SPLIT", "gateway", "baidu_download", "下载分片数", kind="number", default="16",
        min_value=1, max_value=64,
        help="单个文件切成多少片并行下载。非会员账号调高无效，SVIP 账号 16 比较合适。",
    ),
    Field(
        "ARIA2_MAX_CONNECTION", "gateway", "baidu_download", "单服务器最大连接数", kind="number",
        default="16", min_value=1, max_value=64,
        help="与分片数配套使用，一般保持相同数值。",
    ),
    Field(
        "DOWNLOAD_TIMEOUT_SECONDS", "gateway", "baidu_download", "单文件下载超时", kind="number",
        default="1800", unit="秒", min_value=120, max_value=21600,
        help="单个压缩包的下载与解压超时时间。",
    ),
    Field(
        "DOWNLOAD_UA", "gateway", "baidu_download", "下载 User-Agent",
        default="netdisk;P2SP;3.0.20.56;netdisk;7.36.0.6;PC;PC-Windows;10.0.22621;WindowsBaiduYunGuanJia",
        help="伪装成网盘客户端才能跑满 SVIP 速度，不要随意修改。",
    ),
    Field(
        "FULL_SYNC_MAX_PAGES", "gateway", "baidu_download", "索引同步最大页数", kind="number",
        default="2000", min_value=1, max_value=100000,
        help="仅手工执行群文件索引同步时使用，日常任务不会触发。",
    ),
)


FIELDS_BY_KEY: dict[str, Field] = {item.key: item for item in FIELDS}
SECRET_KEYS = {item.key for item in FIELDS if item.kind == "password"}


def fields_for(target: str) -> list[Field]:
    """All fields stored in the ``target`` env file (gateway or worker)."""
    return [item for item in FIELDS if item.target in {target, "both"}]


def key_order(target: str) -> list[str]:
    return [item.key for item in fields_for(target)]


def apply_defaults(values: dict[str, str], target: str) -> dict[str, str]:
    """Fill in defaults for keys that are missing or blank-but-not-meaningful."""
    for item in fields_for(target):
        if not item.default:
            continue
        current = values.get(item.key)
        if current is None or (not current and not item.keep_blank):
            values[item.key] = item.default
    return values


def groups_for_roles(roles: list[str]) -> list[Group]:
    return [group for group in GROUPS if group.target in roles]


def public_schema(roles: list[str]) -> list[dict]:
    """Serialise the schema for the front-end, grouped and filtered by role."""
    payload: list[dict] = []
    for group in groups_for_roles(roles):
        items = [item for item in FIELDS if item.group == group.id]
        payload.append(
            {
                "id": group.id,
                "target": group.target,
                "title": group.title,
                "summary": group.summary,
                "essential": group.essential,
                "fields": [
                    {
                        "key": item.key,
                        "label": item.label,
                        "kind": item.kind,
                        "default": item.default,
                        "options": [{"value": value, "label": label} for value, label in item.options],
                        "help": item.help,
                        "example": item.example,
                        "essential": item.essential,
                        "required": item.required,
                        "placeholder": item.placeholder,
                        "unit": item.unit,
                        "secret": item.kind == "password",
                        "min": item.min_value,
                        "max": item.max_value,
                    }
                    for item in items
                ],
            }
        )
    return payload

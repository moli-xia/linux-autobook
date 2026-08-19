# 544544.xyz 分布式租约集成

本目录保存与 Worker 协议配套的网站插件文件。部署前必须备份线上同名文件和数据库。

目标路径：

```text
lecms/plugin/le_doc_delivery/doc_delivery_control.class.php
lecms/plugin/le_doc_delivery/document_delivery_task_model.class.php
lecms/plugin/le_doc_delivery/install.php
```

上线后第一次调用 `doc_delivery-claim-ajax-1` 会幂等添加 `lease_id`、`lease_until`、`heartbeat_at` 三列。全新安装则由 `install.php` 直接创建。

协议要点：

- `claim` 使用单条 `UPDATE ... WHERE status=... ORDER BY id LIMIT 1` 原子领取；响应包含 `lease_id` 和 `lease_seconds`。
- `progress`、`heartbeat`、`complete` 必须同时提交 `worker_id`、`task_token`、`lease_id`。
- 心跳会延长租约；租约过期的任务自动重新排队，最多重试 3 次。
- 完成更新仍使用 Worker ID 和 lease ID 作为数据库条件，旧 Worker 不能覆盖新 Worker 的结果。

建议部署步骤：

```bash
stamp=$(date +%Y%m%d%H%M%S)
cp -a doc_delivery_control.class.php doc_delivery_control.class.php.bak.$stamp
cp -a document_delivery_task_model.class.php document_delivery_task_model.class.php.bak.$stamp
php -l /path/to/new/doc_delivery_control.class.php
php -l /path/to/new/document_delivery_task_model.class.php
```

确认 PHP 语法无误后再替换文件，并用空队列 claim 验证返回 `no_task`。不要把网站 Worker Token 写进仓库。

LECMS 会把插件编译到 `runtime/lecms_control/` 和 `runtime/lecms_model/`。更新源文件后应先备份、再移走对应的两个运行时缓存文件，让框架从新源文件重新生成；不要直接把插件源文件复制到运行时目录，因为生成版控制器会额外注入基类加载代码。随后重启 PHP-FPM，并确认首页和 claim 接口正常。

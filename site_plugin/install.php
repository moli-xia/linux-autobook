<?php
defined('ROOT_PATH') || exit;

$tableprefix = $_ENV['_config']['db']['master']['tablepre'];
$table = $tableprefix.'document_delivery_task';

$sql = "CREATE TABLE IF NOT EXISTS ".$table." (
  id int(10) unsigned NOT NULL AUTO_INCREMENT,
  task_token varchar(64) NOT NULL DEFAULT '' COMMENT '任务令牌',
  uid int(10) unsigned NOT NULL DEFAULT '0' COMMENT '用户ID',
  username varchar(80) NOT NULL DEFAULT '' COMMENT '用户名',
  book_cid int(10) unsigned NOT NULL DEFAULT '0' COMMENT '内容分类ID',
  book_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT '内容ID',
  book_title varchar(255) NOT NULL DEFAULT '' COMMENT '书名',
  ssno varchar(32) NOT NULL DEFAULT '' COMMENT 'SS码',
  keyword varchar(255) NOT NULL DEFAULT '' COMMENT '检索关键词',
  email varchar(120) NOT NULL DEFAULT '' COMMENT '接收邮箱',
  ip int(10) unsigned NOT NULL DEFAULT '0' COMMENT '提交IP',
  status tinyint(1) unsigned NOT NULL DEFAULT '1' COMMENT '1PDF排队 2PDF处理中 3完成 4失败 5取消 6OCR排队 7OCR处理中',
  result_url varchar(600) NOT NULL DEFAULT '' COMMENT '网盘链接',
  result_file varchar(600) NOT NULL DEFAULT '' COMMENT '本机文件',
  message varchar(600) NOT NULL DEFAULT '' COMMENT '状态消息',
  credit_cost int(10) unsigned NOT NULL DEFAULT '0' COMMENT '文献传递扣除积分',
  billing_type varchar(20) NOT NULL DEFAULT '' COMMENT '扣费方式',
  billing_log_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT '积分流水ID',
  output_format varchar(12) NOT NULL DEFAULT 'pdf' COMMENT '交付格式pdf/txt/docx/md/json/textpdf/zip',
  output_formats varchar(80) NOT NULL DEFAULT 'pdf' COMMENT '多选交付格式csv',
  ocr_cost int(10) unsigned NOT NULL DEFAULT '0' COMMENT 'OCR格式转换扣除积分',
  ocr_billing_type varchar(20) NOT NULL DEFAULT '' COMMENT 'OCR扣费方式',
  ocr_billing_log_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT 'OCR积分流水ID',
  worker_id varchar(80) NOT NULL DEFAULT '' COMMENT 'worker标识',
  lease_id varchar(64) NOT NULL DEFAULT '' COMMENT '分布式任务租约',
  lease_until int(10) unsigned NOT NULL DEFAULT '0' COMMENT '租约到期时间',
  heartbeat_at int(10) unsigned NOT NULL DEFAULT '0' COMMENT '最近心跳时间',
  retry_count tinyint(3) unsigned NOT NULL DEFAULT '0' COMMENT '重试次数',
  dateline int(10) unsigned NOT NULL DEFAULT '0' COMMENT '创建时间',
  updated_at int(10) unsigned NOT NULL DEFAULT '0' COMMENT '更新时间',
  started_at int(10) unsigned NOT NULL DEFAULT '0' COMMENT '开始时间',
  finished_at int(10) unsigned NOT NULL DEFAULT '0' COMMENT '完成时间',
  raw_output mediumtext COMMENT 'worker输出',
  PRIMARY KEY (id),
  UNIQUE KEY task_token (task_token),
  KEY status_id (status,id),
  KEY lease_id (lease_id),
  KEY book_idx (book_cid,book_id),
  KEY uid_idx (uid,status),
  KEY ip_idx (ip,status)
) ENGINE=MyISAM DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;";
$this->db->query($sql);

function le_doc_delivery_add_column_once($db, $table, $column, $ddl) {
    $exists = false;
    $res = $db->query("SHOW COLUMNS FROM ".$table." LIKE '".$column."'");
    if($res) {
        if(is_object($res) && method_exists($res, 'fetch')) $exists = $res->fetch() ? true : false;
        elseif(is_array($res)) $exists = !empty($res);
    }
    if(!$exists) $db->query("ALTER TABLE ".$table." ADD COLUMN ".$ddl);
}
le_doc_delivery_add_column_once($this->db, $table, 'credit_cost', "credit_cost int(10) unsigned NOT NULL DEFAULT '0' COMMENT '文献传递扣除积分'");
le_doc_delivery_add_column_once($this->db, $table, 'billing_type', "billing_type varchar(20) NOT NULL DEFAULT '' COMMENT '扣费方式'");
le_doc_delivery_add_column_once($this->db, $table, 'billing_log_id', "billing_log_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT '积分流水ID'");
le_doc_delivery_add_column_once($this->db, $table, 'output_format', "output_format varchar(12) NOT NULL DEFAULT 'pdf' COMMENT '交付格式pdf/txt/docx/md/json/textpdf/zip'");
le_doc_delivery_add_column_once($this->db, $table, 'output_formats', "output_formats varchar(80) NOT NULL DEFAULT 'pdf' COMMENT '多选交付格式csv'");
le_doc_delivery_add_column_once($this->db, $table, 'ocr_cost', "ocr_cost int(10) unsigned NOT NULL DEFAULT '0' COMMENT 'OCR格式转换扣除积分'");
le_doc_delivery_add_column_once($this->db, $table, 'ocr_billing_type', "ocr_billing_type varchar(20) NOT NULL DEFAULT '' COMMENT 'OCR扣费方式'");
le_doc_delivery_add_column_once($this->db, $table, 'ocr_billing_log_id', "ocr_billing_log_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT 'OCR积分流水ID'");
le_doc_delivery_add_column_once($this->db, $table, 'lease_id', "lease_id varchar(64) NOT NULL DEFAULT '' COMMENT '分布式任务租约'");
le_doc_delivery_add_column_once($this->db, $table, 'lease_until', "lease_until int(10) unsigned NOT NULL DEFAULT '0' COMMENT '租约到期时间'");
le_doc_delivery_add_column_once($this->db, $table, 'heartbeat_at', "heartbeat_at int(10) unsigned NOT NULL DEFAULT '0' COMMENT '最近心跳时间'");

$setting = (array)$this->kv->xget('le_doc_delivery_setting');
if(empty($setting)) {
    $setting = array(
        'enable' => 1,
        'allow_guest' => 1,
        'enable_email' => 1,
        'avg_minutes' => 18,
        'max_active_per_user' => 3,
        'task_timeout_minutes' => 180,
        'worker_token' => substr(md5(uniqid('', true).C('auth_key').mt_rand()), 0, 32),
    );
    $this->kv->set('le_doc_delivery_setting', $setting);
}

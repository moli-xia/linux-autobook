<?php
defined('ROOT_PATH') or exit;

class doc_delivery_control extends base_control {
    private $alimpay_setting_cache = null;
    private $billing_columns_ready = false;
    private $ocr_setting_cache = null;
    private $ocr_columns_ready = false;
    private $lease_columns_ready = false;

    private function json_out($status, $message = '', $data = array()) {
        header('Content-Type: application/json; charset=utf-8');
        $payload = array('kong_status' => $status ? 1 : 0, 'message' => $message);
        if($data) $payload += $data;
        exit(json_encode($payload));
    }

    private function setting() {
        $setting = (array)$this->kv->xget('le_doc_delivery_setting');
        $defaults = array(
            'enable' => 1,
            'allow_guest' => 1,
            'enable_email' => 1,
            'avg_minutes' => 5,
            'max_active_per_user' => 3,
            'task_timeout_minutes' => 180,
            'worker_token' => '',
        );
        return array_merge($defaults, $setting);
    }

    private function client_ip_long() {
        $ip = ip();
        $long = ip2long($ip);
        return $long === false ? 0 : sprintf('%u', $long);
    }

    private function clean_text($text, $length = 255) {
        $text = trim(strip_tags((string)$text));
        $text = preg_replace('/\s+/u', ' ', $text);
        if(function_exists('_substr')) return _substr($text, 0, $length);
        return mb_substr($text, 0, $length, 'UTF-8');
    }

    private function normalize_email($email) {
        $email = trim((string)$email);
        $email = urldecode($email);
        $email = strip_tags($email);
        return strtolower(trim($email));
    }

    private function user_email($uid) {
        $uid = (int)$uid;
        if(!$uid) return '';
        $user = $this->user->get($uid);
        if(empty($user['email'])) return '';
        $email = $this->normalize_email($user['email']);
        return check::check_email($email) ? $email : '';
    }

    private function task_email($task) {
        $email = isset($task['email']) ? $this->normalize_email($task['email']) : '';
        if($email !== '' && check::check_email($email)) return $email;
        return '';
    }

    private function append_task_message($message, $append) {
        $message = trim((string)$message);
        $append = trim((string)$append);
        if($append === '') return $this->clean_text($message, 600);
        return $this->clean_text($message.($message === '' ? '' : ' ').$append, 600);
    }

    private function summarize_traceback($text) {
        $text = trim((string)$text);
        if($text === '') return '';
        if(stripos($text, 'Traceback (most recent call last):') === false) return '';
        $lines = preg_split('/\r\n|\r|\n/', $text);
        $fallback = '';
        for($i = count($lines) - 1; $i >= 0; $i--) {
            $line = trim($lines[$i]);
            if($line === '') continue;
            if($fallback === '') $fallback = $line;
            if(preg_match('/^(ModuleNotFoundError|ImportError|RuntimeError|ValueError|TypeError|KeyError|FileNotFoundError|PermissionError|TimeoutError|ConnectionError|requests\.[A-Za-z.]+):/i', $line)) {
                return 'Python traceback: '.$line;
            }
        }
        if($fallback === '' || stripos($fallback, 'Traceback (most recent call last):') !== false) {
            return 'Python traceback: 格式转换脚本异常，请查看后台 worker 输出。';
        }
        return 'Python traceback: '.$fallback;
    }

    private function sanitize_worker_message($message, $raw_output = '') {
        $message = trim((string)$message);
        $raw_output = trim((string)$raw_output);
        $trace = $this->summarize_traceback($message);
        if($trace === '') $trace = $this->summarize_traceback($raw_output);
        if($trace !== '') return $this->clean_text($trace, 600);
        return $this->clean_text($message, 600);
    }

    private function mail_log($message, $task = array()) {
        if(!class_exists('log')) return;
        $id = !empty($task['id']) ? '#'.$task['id'].' ' : '';
        log::write($id.$message, 'doc_delivery_mail_error.php');
    }

    private function alimpay_setting() {
        if($this->alimpay_setting_cache !== null) return $this->alimpay_setting_cache;
        $file = PLUGIN_PATH.'le_alimpay/le_alimpay_client.class.php';
        if(is_file($file)) require_once $file;
        if(class_exists('le_alimpay_client')) {
            $this->alimpay_setting_cache = le_alimpay_client::setting();
        }else{
            $this->alimpay_setting_cache = array('doc_delivery_cost' => 5);
        }
        return $this->alimpay_setting_cache;
    }

    private function delivery_cost() {
        $setting = $this->alimpay_setting();
        return max(1, (int)(isset($setting['doc_delivery_cost']) ? $setting['doc_delivery_cost'] : 5));
    }

    private function ocr_setting() {
        if($this->ocr_setting_cache !== null) return $this->ocr_setting_cache;
        $file = PLUGIN_PATH.'le_ocr_txt/le_ocr_txt_client.class.php';
        if(is_file($file)) require_once $file;
        if(class_exists('le_ocr_txt_client')) {
            $this->ocr_setting_cache = le_ocr_txt_client::setting();
        }else{
            $this->ocr_setting_cache = array(
                'enable' => 0,
                'conversion_cost' => 5,
                'job_url' => '',
                'token' => '',
                'model' => 'PaddleOCR-VL-1.6',
                'poll_seconds' => 5,
                'timeout_seconds' => 1800,
                'submit_timeout_seconds' => 1800,
                'submit_retries' => 1,
                'daily_page_limit' => 0,
                'limit_notice' => '',
                'format_txt' => 1,
                'format_docx' => 1,
                'format_md' => 1,
                'format_json' => 1,
                'format_textpdf' => 1,
                'optional_payload' => array(),
            );
        }
        return $this->ocr_setting_cache;
    }

    private function ocr_enabled() {
        $setting = $this->ocr_setting();
        return !empty($setting['enable']);
    }

    private function ocr_cost() {
        $setting = $this->ocr_setting();
        return max(1, (int)(isset($setting['conversion_cost']) ? $setting['conversion_cost'] : 5));
    }

    private function ocr_format_enabled($format) {
        $format = strtolower((string)$format);
        if($format === 'markdown') $format = 'md';
        if($format === 'word') $format = 'docx';
        if($format === 'text_pdf' || $format === 'pdf_text' || $format === 'ocrpdf') $format = 'textpdf';
        if(!in_array($format, array('txt', 'docx', 'md', 'json', 'textpdf'))) return false;
        $setting = $this->ocr_setting();
        $file = PLUGIN_PATH.'le_ocr_txt/le_ocr_txt_client.class.php';
        if(is_file($file)) require_once $file;
        if(class_exists('le_ocr_txt_client')) return le_ocr_txt_client::format_enabled($format, $setting);
        return !empty($setting['format_'.$format]);
    }

    private function normalize_output_format_value($format) {
        $format = strtolower(trim((string)$format));
        if($format === 'markdown') $format = 'md';
        if($format === 'word') $format = 'docx';
        if($format === 'text_pdf' || $format === 'pdf_text' || $format === 'ocrpdf') $format = 'textpdf';
        return $format;
    }

    private function requested_output_formats() {
        $raw = trim((string)R('output_formats', 'R'));
        if($raw === '') $raw = trim((string)R('output_format', 'R'));
        if($raw === '') $raw = trim((string)R('delivery_format', 'R'));
        $parts = preg_split('/[,\s]+/', $raw);
        $formats = array();
        foreach((array)$parts as $part) {
            $format = $this->normalize_output_format_value($part);
            if($format === '' || !in_array($format, array('pdf', 'txt', 'docx', 'md', 'json', 'textpdf'))) continue;
            if($format !== 'pdf') {
                if(!$this->ocr_enabled()) $this->json_out(0, 'OCR 格式转换暂未开放，请先选择 PDF。');
                if(!$this->ocr_format_enabled($format)) $this->json_out(0, $this->format_label($format).' 暂未开放，请选择其他格式。');
            }
            if(!in_array($format, $formats)) $formats[] = $format;
        }
        if(empty($formats)) $formats[] = 'pdf';
        if(in_array('pdf', $formats)) {
            $ordered = array('pdf');
            foreach($formats as $format) if($format !== 'pdf') $ordered[] = $format;
            $formats = $ordered;
        }
        return $formats;
    }

    private function requested_output_format() {
        $format = $this->normalize_output_format_value(R('output_format', 'R'));
        if($format === '') $format = $this->normalize_output_format_value(R('delivery_format', 'R'));
        if(!in_array($format, array('txt', 'docx', 'md', 'json', 'textpdf'))) return 'pdf';
        if(!$this->ocr_enabled()) $this->json_out(0, 'OCR 格式转换暂未开放，请先选择 PDF。');
        if(!$this->ocr_format_enabled($format)) $this->json_out(0, $this->format_label($format).' 暂未开放，请选择其他格式。');
        return $format;
    }

    private function primary_output_format($formats) {
        $formats = (array)$formats;
        if(count($formats) === 1) return current($formats);
        return 'zip';
    }

    private function output_formats_csv($formats) {
        return implode(',', (array)$formats);
    }

    private function ocr_formats_count($formats) {
        $count = 0;
        foreach((array)$formats as $format) {
            $format = $this->normalize_output_format_value($format);
            if(in_array($format, array('txt', 'docx', 'md', 'json', 'textpdf'))) $count++;
        }
        return $count;
    }

    private function output_formats_label($formats) {
        $labels = array();
        foreach((array)$formats as $format) {
            $format = $this->normalize_output_format_value($format);
            if($format === '' || !in_array($format, array('pdf', 'txt', 'docx', 'md', 'json', 'textpdf', 'zip'))) continue;
            $labels[] = $this->format_label($format);
        }
        if(empty($labels)) $labels[] = 'PDF';
        return implode('+', $labels);
    }

    private function format_label($format) {
        $format = strtolower((string)$format);
        if($format === 'txt') return 'TXT 纯文本';
        if($format === 'docx') return 'Word 文档';
        if($format === 'md' || $format === 'markdown') return 'Markdown';
        if($format === 'json') return 'JSON 数据';
        if($format === 'textpdf' || $format === 'text_pdf' || $format === 'pdf_text' || $format === 'ocrpdf') return '文本 PDF';
        if($format === 'zip') return '打包文件';
        return 'PDF';
    }

    private function vip_daily_limit($user) {
        $setting = $this->alimpay_setting();
        if(class_exists('le_alimpay_client')) return le_alimpay_client::vip_daily_limit($user, $setting);
        return 0;
    }

    private function today_vip_used($uid, $start) {
        $uid = (int)$uid;
        if(!$uid) return 0;
        $model = $this->document_delivery_task;
        if(method_exists($model, 'today_vip_count_by_user')) {
            return (int)$model->today_vip_count_by_user($uid, $start);
        }
        return (int)$model->find_count(array(
            'uid' => $uid,
            'dateline' => array('>=' => (int)$start),
            'billing_type' => 'vip',
            'status' => array('IN' => array(1, 2, 3, 6, 7)),
        ));
    }

    private function prepare_billing($uid) {
        $uid = (int)$uid;
        if(!$uid) return array('ok' => 0, 'message' => '请登录后使用文献传递；每本书需要积分或 VIP 额度。');
        $user = $this->user->get($uid);
        if(empty($user)) return array('ok' => 0, 'message' => '用户不存在，请重新登录');
        $this->ensure_billing_columns();

        $cost = $this->delivery_cost();
        $dailyLimit = $this->vip_daily_limit($user);
        if($dailyLimit > 0) {
            $start = strtotime(date('Y-m-d 00:00:00', $_ENV['_time']));
            $used = $this->today_vip_used($uid, $start);
            if($used < $dailyLimit) {
                return array(
                    'ok' => 1,
                    'type' => 'vip',
                    'cost' => 0,
                    'daily_limit' => $dailyLimit,
                    'daily_left' => max(0, $dailyLimit - $used - 1),
                    'message' => 'VIP 免费额度，本次不扣积分。',
                );
            }
        }

        if((int)$user['credits'] < $cost) {
            return array(
                'ok' => 0,
                'message' => '积分不足，申请一本书需要 '.$cost.' 积分。请先到会员中心充值积分或开通 VIP。',
            );
        }
        return array(
            'ok' => 1,
            'type' => 'credits',
            'cost' => $cost,
            'balance' => (int)$user['credits'],
            'message' => '本次将扣除 '.$cost.' 积分。',
        );
    }

    private function apply_billing($billing, $task_id, $book_title) {
        if(empty($billing['ok'])) return array('ok' => 0, 'message' => isset($billing['message']) ? $billing['message'] : '扣费失败');
        $type = isset($billing['type']) ? $billing['type'] : '';
        $cost = isset($billing['cost']) ? (int)$billing['cost'] : 0;
        $logId = 0;
        $message = isset($billing['message']) ? $billing['message'] : '';

        if($type === 'credits' && $cost > 0) {
            $user = $this->user->get((int)$this->_uid);
            if(empty($user) || (int)$user['credits'] < $cost) {
                return array('ok' => 0, 'message' => '积分不足，无法提交文献传递申请');
            }
            $balance = (int)$user['credits'] - $cost;
            if(!$this->user->update(array('uid' => $user['uid'], 'credits' => $balance))) {
                return array('ok' => 0, 'message' => '扣除积分失败，请稍后重试');
            }
            if(class_exists('core')) {
                try {
                    $logId = $this->alimpay_credit_log->add_log($user['uid'], $user['username'], -$cost, $balance, 'doc_delivery', 'document_delivery_task', $task_id, '文献传递：《'.$book_title.'》');
                } catch(Exception $e) {
                    $logId = 0;
                }
            }
            $message = '已扣除 '.$cost.' 积分，当前余额 '.$balance.'。';
        }elseif($type === 'vip') {
            $message = '已使用 VIP 免费额度，今日剩余额度 '.(int)$billing['daily_left'].' 本。';
        }

        return array('ok' => 1, 'type' => $type, 'cost' => $cost, 'log_id' => (int)$logId, 'message' => $message);
    }

    private function prepare_ocr_billing($uid, $output_format, $reserved_cost) {
        $formats = is_array($output_format) ? $output_format : array($output_format);
        $ocrCount = $this->ocr_formats_count($formats);
        if($ocrCount <= 0) return array('ok' => 1, 'type' => '', 'cost' => 0, 'message' => '');
        $label = $this->output_formats_label($formats);
        $uid = (int)$uid;
        if(!$uid) return array('ok' => 0, 'message' => '请登录后使用 '.$label.' 转换；每本转换需要额外积分。');
        $user = $this->user->get($uid);
        if(empty($user)) return array('ok' => 0, 'message' => '用户不存在，请重新登录');
        $cost = $this->ocr_cost() * $ocrCount;
        $need = max(0, (int)$reserved_cost) + $cost;
        if((int)$user['credits'] < $need) {
            return array(
                'ok' => 0,
                'message' => '积分不足：'.$label.' 转换额外需要 '.$cost.' 积分，本次合计需要 '.$need.' 积分。请先充值积分。',
            );
        }
        return array(
            'ok' => 1,
            'type' => 'credits',
            'cost' => $cost,
            'balance' => (int)$user['credits'],
            'message' => $label.' 转换将额外扣除 '.$cost.' 积分。',
        );
    }

    private function apply_ocr_billing($billing, $task_id, $book_title, $output_format = 'txt') {
        if(empty($billing['ok'])) return array('ok' => 0, 'message' => isset($billing['message']) ? $billing['message'] : 'OCR 扣费失败');
        $cost = isset($billing['cost']) ? (int)$billing['cost'] : 0;
        if($cost <= 0) return array('ok' => 1, 'type' => '', 'cost' => 0, 'log_id' => 0, 'message' => '');
        $label = is_array($output_format) ? $this->output_formats_label($output_format) : $this->format_label($output_format);

        $user = $this->user->get((int)$this->_uid);
        if(empty($user) || (int)$user['credits'] < $cost) {
            return array('ok' => 0, 'message' => '积分不足，无法提交 '.$label.' 转换申请');
        }
        $balance = (int)$user['credits'] - $cost;
        if(!$this->user->update(array('uid' => $user['uid'], 'credits' => $balance))) {
            return array('ok' => 0, 'message' => '扣除 '.$label.' 转换积分失败，请稍后重试');
        }
        $logId = 0;
        try {
            $logId = $this->alimpay_credit_log->add_log($user['uid'], $user['username'], -$cost, $balance, 'ocr_format', 'document_delivery_task', $task_id, 'PDF 转 '.$label.'：《'.$book_title.'》');
        } catch(Exception $e) {
            $logId = 0;
        }
        return array('ok' => 1, 'type' => 'credits', 'cost' => $cost, 'log_id' => (int)$logId, 'message' => '已扣除 '.$label.' 转换 '.$cost.' 积分，当前余额 '.$balance.'。');
    }

    private function refund_billing_if_needed($task, $message) {
        if(empty($task) || (string)$task['billing_type'] !== 'credits' || (int)$task['credit_cost'] <= 0 || empty($task['uid'])) {
            return $message;
        }
        $cost = (int)$task['credit_cost'];
        $user = $this->user->get((int)$task['uid']);
        if(empty($user)) return $message;
        $balance = (int)$user['credits'] + $cost;
        if(!$this->user->update(array('uid' => $user['uid'], 'credits' => $balance))) return $message;
        try {
            $this->alimpay_credit_log->add_log($user['uid'], $user['username'], $cost, $balance, 'doc_delivery_refund', 'document_delivery_task', $task['id'], '文献传递失败退款：《'.$task['book_title'].'》');
        } catch(Exception $e) {}
        $refundMessage = '任务失败，已退回 '.$cost.' 积分，当前余额 '.$balance.'。';
        $message = $this->append_task_message($message, $refundMessage);
        $this->document_delivery_task->update(array(
            'id' => $task['id'],
            'billing_type' => 'credits_refunded',
            'message' => $message,
            'updated_at' => $_ENV['_time'],
        ));
        return $message;
    }

    private function refund_ocr_billing_if_needed($task, $message) {
        if(empty($task) || (string)$task['ocr_billing_type'] !== 'credits' || (int)$task['ocr_cost'] <= 0 || empty($task['uid'])) {
            return $message;
        }
        $cost = (int)$task['ocr_cost'];
        $label = !empty($task['output_formats'])
            ? $this->output_formats_label(preg_split('/[,\s]+/', $task['output_formats']))
            : $this->format_label(isset($task['output_format']) ? $task['output_format'] : 'txt');
        $user = $this->user->get((int)$task['uid']);
        if(empty($user)) return $message;
        $balance = (int)$user['credits'] + $cost;
        if(!$this->user->update(array('uid' => $user['uid'], 'credits' => $balance))) return $message;
        try {
            $this->alimpay_credit_log->add_log($user['uid'], $user['username'], $cost, $balance, 'ocr_format_refund', 'document_delivery_task', $task['id'], 'PDF 转 '.$label.' 退款：《'.$task['book_title'].'》');
        } catch(Exception $e) {}
        $refundMessage = '已退回 '.$label.' 转换 '.$cost.' 积分，当前余额 '.$balance.'。';
        $message = $this->append_task_message($message, $refundMessage);
        $this->document_delivery_task->update(array(
            'id' => $task['id'],
            'ocr_billing_type' => 'credits_refunded',
            'message' => $message,
            'updated_at' => $_ENV['_time'],
        ));
        return $message;
    }

    private function ensure_billing_columns() {
        if($this->billing_columns_ready) return;
        $table = $_ENV['_config']['db']['master']['tablepre'].'document_delivery_task';
        $this->add_column_once($table, 'credit_cost', "credit_cost int(10) unsigned NOT NULL DEFAULT '0' COMMENT '文献传递扣除积分'");
        $this->add_column_once($table, 'billing_type', "billing_type varchar(20) NOT NULL DEFAULT '' COMMENT '扣费方式'");
        $this->add_column_once($table, 'billing_log_id', "billing_log_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT '积分流水ID'");
        $this->billing_columns_ready = true;
    }

    private function ensure_ocr_columns() {
        if($this->ocr_columns_ready) return;
        $table = $_ENV['_config']['db']['master']['tablepre'].'document_delivery_task';
        $this->add_column_once($table, 'output_format', "output_format varchar(12) NOT NULL DEFAULT 'pdf' COMMENT '交付格式pdf/txt/docx/md/json/textpdf'");
        $this->add_column_once($table, 'output_formats', "output_formats varchar(80) NOT NULL DEFAULT 'pdf' COMMENT '多选交付格式csv'");
        $this->add_column_once($table, 'ocr_cost', "ocr_cost int(10) unsigned NOT NULL DEFAULT '0' COMMENT 'OCR格式转换扣除积分'");
        $this->add_column_once($table, 'ocr_billing_type', "ocr_billing_type varchar(20) NOT NULL DEFAULT '' COMMENT 'OCR扣费方式'");
        $this->add_column_once($table, 'ocr_billing_log_id', "ocr_billing_log_id int(10) unsigned NOT NULL DEFAULT '0' COMMENT 'OCR积分流水ID'");
        $this->ocr_columns_ready = true;
    }

    private function ensure_lease_columns() {
        if($this->lease_columns_ready) return;
        $table = $_ENV['_config']['db']['master']['tablepre'].'document_delivery_task';
        $this->add_column_once($table, 'lease_id', "lease_id varchar(64) NOT NULL DEFAULT '' COMMENT '分布式任务租约'");
        $this->add_column_once($table, 'lease_until', "lease_until int(10) unsigned NOT NULL DEFAULT '0' COMMENT '租约到期时间'");
        $this->add_column_once($table, 'heartbeat_at', "heartbeat_at int(10) unsigned NOT NULL DEFAULT '0' COMMENT '最近心跳时间'");
        $this->lease_columns_ready = true;
    }

    private function lease_seconds() {
        return 300;
    }

    private function worker_lease_input() {
        $worker_id = preg_replace('/[^A-Za-z0-9._:-]/', '_', $this->clean_text(R('worker_id', 'P'), 80));
        $lease_id = strtolower(trim((string)R('lease_id', 'P')));
        if($worker_id === '' || !preg_match('/^[a-f0-9]{32}$/', $lease_id)) return array();
        return array($worker_id, $lease_id);
    }

    private function add_column_once($table, $column, $ddl) {
        $exists = false;
        $res = $this->db->query("SHOW COLUMNS FROM ".$table." LIKE '".$column."'");
        if($res) {
            if(is_object($res) && method_exists($res, 'fetch')) $exists = $res->fetch() ? true : false;
            elseif(is_array($res)) $exists = !empty($res);
        }
        if(!$exists) $this->db->query("ALTER TABLE ".$table." ADD COLUMN ".$ddl);
    }

    private function pick_book_title($book, $post_title = '') {
        $post_title = $this->clean_text($post_title, 255);
        if(!empty($book) && is_array($book)) {
            foreach(array('title', 'subject', 'book_title', 'name') as $key) {
                if(isset($book[$key]) && trim((string)$book[$key]) !== '') {
                    return $this->clean_text($book[$key], 255);
                }
            }
        }
        return $post_title;
    }

    private function read_book($cid, $id) {
        $cid = (int)$cid;
        $id = (int)$id;
        if(!$cid || !$id) return array();
        $cate = $this->category->get_cache($cid);
        if(empty($cate) || empty($cate['table'])) return array();
        $this->cms_content->table = 'cms_'.$cate['table'];
        $this->cms_content_data->table = 'cms_'.$cate['table'].'_data';
        $book = $this->cms_content->read($id);
        if(empty($book) || (int)$book['cid'] != $cid) return array();
        $data = $this->cms_content_data->read($id);
        if($data) $book += $data;
        return $book;
    }

    private function detect_ssno($book) {
        $keys = array('ssno', 'ss', 'ssnum', 'sscode', 'source', 'title', 'subject', 'book_title', 'intro', 'content');
        $text = '';
        foreach($keys as $key) {
            if(isset($book[$key]) && $book[$key] !== '') $text .= ' '.$book[$key];
        }
        $text = strip_tags($text);
        if(preg_match('/(?:SS|ss|SS号|SS码|ss号|ss码)[\s:：#-]*([0-9]{5,12})/u', $text, $m)) {
            return $m[1];
        }
        if(preg_match('/_([0-9]{8})_/u', $text, $m)) {
            return $m[1];
        }
        if(preg_match('/(?:^|[^0-9])([0-9]{8})(?:[^0-9]|$)/u', $text, $m)) {
            return $m[1];
        }
        return '';
    }

    private function get_task_by_token($token) {
        $list = $this->document_delivery_task->find_fetch(array('task_token' => $token), array('id' => -1), 0, 1);
        return empty($list) ? array() : current($list);
    }

    private function build_task_response($task, $message = 'ok') {
        $setting = $this->setting();
        $task = $this->document_delivery_task->format_task($task, (int)$setting['avg_minutes']);
        if(empty($task['task_token']) && !empty($task['id'])) {
            $task['task_token'] = md5(uniqid('', true).C('auth_key').$task['id'].mt_rand());
            $this->document_delivery_task->update(array('id' => $task['id'], 'task_token' => $task['task_token'], 'updated_at' => $_ENV['_time']));
        }
        // re-read from DB to get full row (bypasses model::unique cache)
        $fullTask = $this->get_task_by_token($task['task_token']);
        if(!empty($fullTask)) $task = $fullTask;
        $task = $this->document_delivery_task->format_task($task, (int)$setting['avg_minutes']);
        $public = array(
            'id' => (int)$task['id'],
            'token' => $task['task_token'],
            'status' => (int)$task['status'],
            'status_label' => $task['status_label'],
            'book_title' => $task['book_title'],
            'ssno' => $task['ssno'],
            'queue_position' => (int)$task['queue_position'],
            'ahead_count' => (int)$task['ahead_count'],
            'estimated_wait_minutes' => (int)$task['estimated_wait_minutes'],
            'queue_stage' => isset($task['queue_stage']) ? $task['queue_stage'] : 'pdf',
            'queue_label' => isset($task['queue_label']) ? $task['queue_label'] : 'PDF 处理队列',
            'result_url' => $task['result_url'],
            'message' => $this->sanitize_worker_message(isset($task['message']) ? $task['message'] : '', isset($task['raw_output']) ? $task['raw_output'] : ''),
            'delivery_mode' => !empty($task['email']) ? 'email' : 'page',
            'output_format' => !empty($task['output_format']) && in_array($task['output_format'], array('txt', 'docx', 'md', 'json', 'textpdf', 'zip')) ? $task['output_format'] : 'pdf',
            'output_formats' => !empty($task['output_formats']) ? $task['output_formats'] : (!empty($task['output_format']) ? $task['output_format'] : 'pdf'),
            'output_label' => $this->format_label(isset($task['output_format']) ? $task['output_format'] : 'pdf'),
            'ocr_cost' => isset($task['ocr_cost']) ? (int)$task['ocr_cost'] : 0,
            'created_text' => $task['created_text'],
            'finished_text' => $task['finished_text'],
        );
        $this->json_out(1, $message, array('task' => $public, 'token' => $task['task_token'], 'id' => (int)$task['id']));
    }

    public function create() {
        $setting = $this->setting();
        if(empty($setting['enable'])) $this->json_out(0, '文献传递暂未开放');
        if(empty($this->_uid)) $this->json_out(0, '请登录后再提交文献传递');

        $uid = (int)$this->_uid;
        $cid = (int)R('cid', 'R');
        $book_id = (int)R('id', 'R');
        $book = $this->read_book($cid, $book_id);

        $post_title = urldecode(R('title', 'R'));
        $post_title = safe_str($post_title);
        $book_title = $this->pick_book_title($book, $post_title);
        if($book_title === '') $this->json_out(0, '缺少书名，无法创建任务');

        $submitted_email = $this->normalize_email(R('email', 'R'));
        $delivery_mode = 'email';
        $email = '';
        if($delivery_mode === 'email') {
            $email = $submitted_email;
            if($email === '' && $uid) $email = $this->user_email($uid);
            if($email === '') $this->json_out(0, '请填写接收邮箱，提交后可关闭页面；若未关闭页面，完成后仍会显示网盘链接。');
            if(!check::check_email($email)) $this->json_out(0, '邮箱格式不正确');
        }
        $output_formats = $this->requested_output_formats();
        $output_format = $this->primary_output_format($output_formats);
        $output_formats_csv = $this->output_formats_csv($output_formats);
        $this->ensure_ocr_columns();

        $ssno = $this->clean_text(R('ssno', 'R'), 32);
        if($ssno === '' && $book) $ssno = $this->detect_ssno($book);
        if($ssno === '') $ssno = $this->detect_ssno(array('title' => $book_title));
        $keyword = $ssno ? '' : $book_title;
        $ip = (int)$this->client_ip_long();

        $max_active = max(1, (int)$setting['max_active_per_user']);
        if($this->document_delivery_task->active_count_by_user($uid, $ip) >= $max_active) {
            $this->json_out(0, '你还有未完成的文献传递任务，请稍后再提交');
        }

        $duplicate = $this->document_delivery_task->find_active_duplicate($uid, $ip, $cid, $book_id, $book_title, $output_formats_csv);
        if($duplicate) {
            if(!isset($duplicate['email']) || $this->normalize_email($duplicate['email']) !== $email) {
                $this->document_delivery_task->update(array(
                    'id' => $duplicate['id'],
                    'email' => $email,
                    'updated_at' => $_ENV['_time'],
                ));
                $duplicate['email'] = $email;
            }
            $this->build_task_response($duplicate, '已有相同任务在队列中');
        }

        $billing = $this->prepare_billing($uid);
        if(empty($billing['ok'])) $this->json_out(0, $billing['message']);
        $this->ensure_billing_columns();
        $reservedCost = (isset($billing['type']) && $billing['type'] === 'credits') ? (int)$billing['cost'] : 0;
        $ocrBilling = $this->prepare_ocr_billing($uid, $output_formats, $reservedCost);
        if(empty($ocrBilling['ok'])) $this->json_out(0, $ocrBilling['message']);

        $username = '';
        if($uid && isset($this->_user['username'])) $username = $this->_user['username'];
        $token = md5(uniqid('', true).C('auth_key').$book_title.mt_rand());
        $now = $_ENV['_time'];
        $baseMessage = '任务已进入 PDF 处理队列，完成后会自动发送到邮箱；页面未关闭时也会显示网盘链接。';
        if($this->ocr_formats_count($output_formats) > 0) $baseMessage = $this->append_task_message($baseMessage, '已选择 '.$this->output_formats_label($output_formats).'，PDF 生成后会进入格式转换队列，可能用时较长。');
        $data = array(
            'task_token' => $token,
            'uid' => $uid,
            'username' => $this->clean_text($username, 80),
            'book_cid' => $cid,
            'book_id' => $book_id,
            'book_title' => $book_title,
            'ssno' => $ssno,
            'keyword' => $keyword,
            'email' => $email,
            'ip' => $ip,
            'status' => 1,
            'credit_cost' => isset($billing['cost']) ? (int)$billing['cost'] : 0,
            'billing_type' => isset($billing['type']) ? $billing['type'] : '',
            'output_format' => $output_format,
            'output_formats' => $output_formats_csv,
            'ocr_cost' => isset($ocrBilling['cost']) ? (int)$ocrBilling['cost'] : 0,
            'ocr_billing_type' => isset($ocrBilling['type']) ? $ocrBilling['type'] : '',
            'message' => $baseMessage,
            'dateline' => $now,
            'updated_at' => $now,
        );
        $id = $this->document_delivery_task->create($data);
        if(!$id) $this->json_out(0, '创建任务失败，请稍后重试');
        $data['id'] = $id;
        $charged = $this->apply_billing($billing, $id, $book_title);
        if(empty($charged['ok'])) {
            $this->document_delivery_task->delete($id);
            $this->json_out(0, $charged['message']);
        }
        $data['message'] = $this->append_task_message($data['message'], $charged['message']);
        $data['billing_log_id'] = isset($charged['log_id']) ? (int)$charged['log_id'] : 0;
        $ocrCharged = $this->apply_ocr_billing($ocrBilling, $id, $book_title, $output_formats);
        if(empty($ocrCharged['ok'])) {
            $this->document_delivery_task->update(array(
                'id' => $id,
                'billing_log_id' => $data['billing_log_id'],
                'message' => $data['message'],
                'updated_at' => $_ENV['_time'],
            ));
            $this->refund_billing_if_needed($data, $ocrCharged['message']);
            $this->document_delivery_task->delete($id);
            $this->json_out(0, $ocrCharged['message']);
        }
        $data['message'] = $this->append_task_message($data['message'], isset($ocrCharged['message']) ? $ocrCharged['message'] : '');
        $data['ocr_billing_log_id'] = isset($ocrCharged['log_id']) ? (int)$ocrCharged['log_id'] : 0;
        $this->document_delivery_task->update(array(
            'id' => $id,
            'message' => $data['message'],
            'billing_log_id' => $data['billing_log_id'],
            'ocr_billing_log_id' => $data['ocr_billing_log_id'],
            'updated_at' => $_ENV['_time'],
        ));
        $this->build_task_response($data, '任务已提交');
    }

    public function status() {
        $token = trim((string)R('token', 'R'));
        $id = (int)R('id', 'R');
        if($token === '' && !$id) {
            $this->json_out(0, '');
        }
        $task = $token === '' ? $this->get_task_by_public_id($id) : $this->get_task_by_token($token);
        if(empty($task)) {
            $this->json_out(0, '');
        }
        $this->build_task_response($task, 'ok');
    }

    public function sendmail() {
        $setting = $this->setting();
        if(!email::available($this->_cfg, 'doc_delivery')) $this->json_out(0, '邮件发送功能未开启');
        $token = trim((string)R('token', 'R'));
        $token = urldecode($token);
        $email = $this->normalize_email(R('email', 'R'));
        if($token === '') {
            $this->json_out(0, '');
        }
        if($email === '' || !check::check_email($email)) $this->json_out(0, '邮箱格式不正确');
        $list = $this->document_delivery_task->find_fetch(array('task_token' => $token), array('id' => -1), 0, 1);
        if(empty($list)) {
            $this->json_out(0, '');
        }
        $task = current($list);
        if((int)$task['status'] !== 3 || empty($task['result_url'])) $this->json_out(0, '任务尚未完成，暂不能发送邮件');
        $ok = $this->send_link_email($task, $email);
        if($ok) {
            $this->document_delivery_task->update(array('id' => $task['id'], 'email' => $email, 'updated_at' => $_ENV['_time']));
            $this->json_out(1, '网盘链接已发送到邮箱');
        }
        $this->json_out(0, '邮件发送失败，请检查网站邮箱配置');
    }

    public function claim() {
        if(!$this->auth_worker()) $this->json_out(0, 'worker token 不正确');
        $setting = $this->setting();
        $this->ensure_billing_columns();
        $this->ensure_ocr_columns();
        $this->ensure_lease_columns();
        $timeout = max(10, (int)$setting['task_timeout_minutes']) * 60;
        $ocrSetting = $this->ocr_setting();
        $ocrTimeout = max(
            12 * 3600,
            (int)(isset($ocrSetting['timeout_seconds']) ? $ocrSetting['timeout_seconds'] : 1800)
            + (int)(isset($ocrSetting['submit_timeout_seconds']) ? $ocrSetting['submit_timeout_seconds'] : 1800)
            + 3600
        );
        $expiredTasks = $this->document_delivery_task->recover_expired_leases($_ENV['_time'], 3);
        $legacyExpired = $this->document_delivery_task->timeout_processing($_ENV['_time'] - $timeout, $_ENV['_time'] - $ocrTimeout);
        foreach($legacyExpired as $legacyTask) $expiredTasks[] = $legacyTask;
        if(!empty($expiredTasks)) {
            foreach($expiredTasks as $expiredTask) {
                $expiredMessage = $this->refund_billing_if_needed($expiredTask, isset($expiredTask['message']) ? $expiredTask['message'] : '');
                $this->refund_ocr_billing_if_needed($expiredTask, $expiredMessage);
            }
        }
        $worker_queue = strtolower(trim((string)R('worker_queue', 'P')));
        if(!in_array($worker_queue, array('all', 'pdf', 'ocr'))) $worker_queue = 'all';
        $worker_id = preg_replace('/[^A-Za-z0-9._:-]/', '_', $this->clean_text(R('worker_id', 'P'), 80));
        if($worker_id === '') $this->json_out(0, '缺少 worker_id');
        $lease_id = md5(uniqid('', true).C('auth_key').$worker_id.mt_rand());
        $task = $this->document_delivery_task->claim_any_atomic($worker_queue, $worker_id, $lease_id, $_ENV['_time'], $this->lease_seconds());
        if(empty($task)) $this->json_out(1, 'no_task', array('task' => null));
        $queue_stage = ((int)$task['status'] === 7) ? 'ocr' : 'pdf';
        $output_format = !empty($task['output_format']) && in_array($task['output_format'], array('txt', 'docx', 'md', 'json', 'textpdf', 'zip')) ? $task['output_format'] : 'pdf';
        $output_formats_csv = !empty($task['output_formats']) ? $task['output_formats'] : $output_format;
        $task_output_formats = preg_split('/[,\s]+/', $output_formats_csv);
        if($this->ocr_formats_count($task_output_formats) > 0 && !$this->ocr_enabled()) {
            $message = 'OCR 格式转换插件未开启或配置不可用，任务已取消。';
            $this->document_delivery_task->update(array(
                'id' => $task['id'],
                'status' => 4,
                'message' => $message,
                'updated_at' => $_ENV['_time'],
                'finished_at' => $_ENV['_time'],
            ));
            $refundMessage = $this->refund_billing_if_needed($task, $message);
            $this->refund_ocr_billing_if_needed($task, $refundMessage);
            $this->json_out(1, 'ocr_not_available_skipped', array('task' => null));
        }
        if($queue_stage === 'ocr' && ($this->ocr_formats_count($task_output_formats) <= 0 || empty($task['result_file']))) {
            $message = '格式转换任务缺少已生成的 PDF 文件，已取消。';
            $this->document_delivery_task->update(array(
                'id' => $task['id'],
                'status' => 4,
                'message' => $message,
                'updated_at' => $_ENV['_time'],
                'finished_at' => $_ENV['_time'],
            ));
            $refundMessage = $this->refund_billing_if_needed($task, $message);
            $this->refund_ocr_billing_if_needed($task, $refundMessage);
            $this->json_out(1, 'invalid_ocr_task_skipped', array('task' => null));
        }
        if(empty($task['task_token'])) {
            $task['task_token'] = md5(uniqid('', true).C('auth_key').$task['id'].mt_rand());
        }
        if(empty($task['book_title']) && !empty($task['book_cid']) && !empty($task['book_id'])) {
            $book = $this->read_book($task['book_cid'], $task['book_id']);
            $task['book_title'] = $this->pick_book_title($book, '');
        }
        if(empty($task['keyword']) && empty($task['ssno']) && !empty($task['book_title'])) {
            $task['ssno'] = $this->detect_ssno(array('title' => $task['book_title']));
            $task['keyword'] = $task['ssno'] ? '' : $task['book_title'];
        }
        if($queue_stage === 'pdf' && empty($task['ssno']) && empty($task['keyword']) && empty($task['book_title'])) {
            $this->document_delivery_task->update(array(
                'id' => $task['id'],
                'task_token' => $task['task_token'],
                'status' => 4,
                'message' => '任务缺少书名、关键词或 SS 号，无法处理。',
                'updated_at' => $_ENV['_time'],
                'finished_at' => $_ENV['_time'],
            ));
            $task['message'] = '任务缺少书名、关键词或 SS 号，无法处理。';
            $refundMessage = $this->refund_billing_if_needed($task, $task['message']);
            $this->refund_ocr_billing_if_needed($task, $refundMessage);
            $this->json_out(1, 'invalid_task_skipped', array('task' => null));
        }
        $now = $_ENV['_time'];
        $claimMessage = $queue_stage === 'ocr'
            ? '格式转换 worker 已领取任务，开始转换 '.$this->format_label($output_format).'。'
            : ($output_format !== 'pdf' ? '远程服务器已领取任务，开始生成 PDF；生成后进入格式转换队列。' : '远程服务器已领取任务，开始处理 PDF。');
        $claimed = $this->document_delivery_task->lease_update($task, $worker_id, $lease_id, array(
            'task_token' => $task['task_token'],
            'book_title' => $task['book_title'],
            'ssno' => $task['ssno'],
            'keyword' => $task['keyword'],
            'output_format' => $output_format,
            'output_formats' => $output_formats_csv,
            'message' => $claimMessage,
            'updated_at' => $now,
        ));
        if($claimed !== 1) $this->json_out(0, '任务租约已失效');
        $task = $this->get_task_by_token($task['task_token']);
        if(empty($task)) $task = $this->document_delivery_task->get((int)$task['id']);
        $workerTask = array(
            'id' => (int)$task['id'],
            'token' => $task['task_token'],
            'book_title' => $task['book_title'],
            'ssno' => $task['ssno'],
            'keyword' => $task['keyword'] ? $task['keyword'] : $task['book_title'],
            'email' => $task['email'],
            'queue_stage' => $queue_stage,
            'output_format' => !empty($task['output_format']) && in_array($task['output_format'], array('txt', 'docx', 'md', 'json', 'textpdf', 'zip')) ? $task['output_format'] : 'pdf',
            'output_formats' => !empty($task['output_formats']) ? $task['output_formats'] : (!empty($task['output_format']) ? $task['output_format'] : 'pdf'),
            'lease_id' => $lease_id,
            'lease_seconds' => $this->lease_seconds(),
        );
        if($queue_stage === 'ocr') {
            $workerTask['pdf_file'] = isset($task['result_file']) ? $task['result_file'] : '';
            $workerTask['pdf_raw_output'] = isset($task['raw_output']) ? $task['raw_output'] : '';
        }
        if($workerTask['output_format'] !== 'pdf') {
            $file = PLUGIN_PATH.'le_ocr_txt/le_ocr_txt_client.class.php';
            if(is_file($file)) require_once $file;
            $workerTask['ocr'] = class_exists('le_ocr_txt_client') ? le_ocr_txt_client::worker_payload($this->ocr_setting()) : array();
        }
        $this->json_out(1, 'claimed', array('task' => $workerTask));
    }

    public function progress() {
        if(!$this->auth_worker()) $this->json_out(0, 'worker token 不正确');
        $this->ensure_lease_columns();
        $token = trim((string)R('task_token', 'P'));
        $message = $this->clean_text(R('message', 'P'), 600);
        if($token === '') $this->json_out(0, '缺少任务令牌');
        $task = $this->get_task_by_token($token);
        if(empty($task)) $this->json_out(0, '任务不存在');
        $lease = $this->worker_lease_input();
        if(empty($lease)) $this->json_out(0, '缺少或无效的任务租约');
        if(!in_array((int)$task['status'], array(2, 7))) $this->json_out(0, '任务不在处理中');
        if($this->document_delivery_task->renew_lease($task['id'], $task['status'], $lease[0], $lease[1], $_ENV['_time'], $this->lease_seconds(), $message) !== 1) {
            $this->json_out(0, '任务租约已失效');
        }
        $this->json_out(1, 'updated');
    }

    public function heartbeat() {
        if(!$this->auth_worker()) $this->json_out(0, 'worker token 不正确');
        $this->ensure_lease_columns();
        $token = trim((string)R('task_token', 'P'));
        if($token === '') $this->json_out(0, '缺少任务令牌');
        $task = $this->get_task_by_token($token);
        if(empty($task)) $this->json_out(0, '任务不存在');
        $lease = $this->worker_lease_input();
        if(empty($lease)) $this->json_out(0, '缺少或无效的任务租约');
        if(!in_array((int)$task['status'], array(2, 7))) $this->json_out(0, '任务不在处理中');
        if($this->document_delivery_task->renew_lease($task['id'], $task['status'], $lease[0], $lease[1], $_ENV['_time'], $this->lease_seconds()) !== 1) {
            $this->json_out(0, '任务租约已失效');
        }
        $this->json_out(1, 'renewed', array('lease_seconds' => $this->lease_seconds()));
    }

    public function complete() {
        if(!$this->auth_worker()) $this->json_out(0, 'worker token 不正确');
        $setting = $this->setting();
        $this->ensure_billing_columns();
        $this->ensure_ocr_columns();
        $this->ensure_lease_columns();
        $token = trim((string)R('task_token', 'P'));
        if($token === '') $this->json_out(0, '缺少任务令牌');
        $task = $this->get_task_by_token($token);
        if(empty($task)) $this->json_out(0, '任务不存在');
        $lease = $this->worker_lease_input();
        if(empty($lease)) $this->json_out(0, '缺少或无效的任务租约');
        if(!in_array((int)$task['status'], array(2, 7))) $this->json_out(0, '任务不在处理中或已被回收');

        $worker_status = trim((string)R('worker_status', 'P'));
        $result_url = trim((string)R('result_url', 'P'));
        $result_file = $this->clean_text(R('result_file', 'P'), 600);
        $posted_message = (string)R('message', 'P');
        $raw_output = trim((string)R('raw_output', 'P'));
        if(strlen($raw_output) > 60000) $raw_output = substr($raw_output, -60000);
        $message = $this->sanitize_worker_message($posted_message, $raw_output);

        if((int)$task['status'] === 4 && $worker_status === 'completed' && ((isset($task['billing_type']) && $task['billing_type'] === 'credits_refunded') || (isset($task['ocr_billing_type']) && $task['ocr_billing_type'] === 'credits_refunded'))) {
            $this->document_delivery_task->update(array(
                'id' => $task['id'],
                'raw_output' => $raw_output,
                'updated_at' => $_ENV['_time'],
            ));
            $this->json_out(1, '任务已失败并退款，忽略迟到的完成回调。');
        }

        if($worker_status === 'pdf_ready' && $result_file !== '') {
            if($message === '') $message = 'PDF 已生成，正在等待格式转换队列。';
            $saved = $this->document_delivery_task->lease_update($task, $lease[0], $lease[1], array(
                'status' => 6,
                'result_file' => $result_file,
                'message' => $message,
                'raw_output' => $raw_output,
                'updated_at' => $_ENV['_time'],
                'lease_id' => '',
                'lease_until' => 0,
                'heartbeat_at' => 0,
            ));
            if($saved !== 1) $this->json_out(0, '任务租约已失效，拒绝迟到结果');
            $task = $this->get_task_by_token($token);
            $this->build_task_response($task, 'pdf_ready');
        }

        $status = ($worker_status === 'completed' && $result_url !== '') ? 3 : 4;
        if($message === '') $message = $status == 3 ? '文献传递完成。' : '文献传递失败，请稍后重试。';

        $update = array(
            'id' => $task['id'],
            'status' => $status,
            'result_url' => $result_url,
            'result_file' => $result_file,
            'message' => $message,
            'raw_output' => $raw_output,
            'updated_at' => $_ENV['_time'],
            'finished_at' => $_ENV['_time'],
            'lease_id' => '',
            'lease_until' => 0,
            'heartbeat_at' => 0,
        );
        if($this->document_delivery_task->lease_update($task, $lease[0], $lease[1], $update) !== 1) {
            $this->json_out(0, '任务租约已失效，拒绝迟到结果');
        }
        if($status == 4) {
            $task['result_url'] = $result_url;
            $task['result_file'] = $result_file;
            $task['message'] = $message;
            $message = $this->refund_billing_if_needed($task, $message);
            $task['message'] = $message;
            $message = $this->refund_ocr_billing_if_needed($task, $message);
        }

        $recipientEmail = $this->task_email($task);
        if($status == 3 && $recipientEmail !== '') {
            $task['result_url'] = $result_url;
            $task['result_file'] = $result_file;
            $sent = $this->send_link_email($task, $recipientEmail);
            $mailMessage = $sent
                ? '网盘链接已自动发送到邮箱：'.$recipientEmail.'。'
                : '自动邮件发送失败，请手动点击发送到邮箱。';
            if(!$sent) $this->mail_log('自动发送失败，收件邮箱：'.$recipientEmail, $task);
            $message = $this->append_task_message($message, $mailMessage);
            $this->document_delivery_task->update(array(
                'id' => $task['id'],
                'email' => $recipientEmail,
                'message' => $message,
                'updated_at' => $_ENV['_time'],
            ));
        }elseif($status == 3) {
            $message = $this->append_task_message($message, '未找到接收邮箱，未自动发送邮件。');
            $this->document_delivery_task->update(array(
                'id' => $task['id'],
                'message' => $message,
                'updated_at' => $_ENV['_time'],
            ));
        }
        $task = $this->get_task_by_token($token);
        $this->build_task_response($task, 'saved');
    }

    private function get_task_by_public_id($id) {
        $id = (int)$id;
        if(!$id) return array();
        $task = $this->document_delivery_task->get($id);
        if(empty($task)) return array();
        $uid = (int)$this->_uid;
        if($uid && (int)$task['uid'] === $uid) return $task;
        if((int)$task['ip'] === (int)$this->client_ip_long()) return $task;
        return array();
    }

    private function auth_worker() {
        $setting = $this->setting();
        $token = trim((string)R('worker_token', 'P'));
        if($token === '' && isset($_SERVER['HTTP_X_WORKER_TOKEN'])) $token = trim($_SERVER['HTTP_X_WORKER_TOKEN']);
        if($token === '' || empty($setting['worker_token'])) return false;
        if(function_exists('hash_equals')) return hash_equals($setting['worker_token'], $token);
        return $setting['worker_token'] === $token;
    }

    private function send_link_email($task, $to) {
        if(!email::available($this->_cfg, 'doc_delivery')) {
            return false;
        }
        $label = !empty($task['output_formats'])
            ? $this->output_formats_label(preg_split('/[,\s]+/', $task['output_formats']))
            : $this->format_label(isset($task['output_format']) ? $task['output_format'] : 'pdf');
        $title = '《'.$task['book_title'].'》'.$label.' 文献传递完成';
        $url = htmlspecialchars($task['result_url'], ENT_QUOTES, 'UTF-8');
        $book = htmlspecialchars($task['book_title'], ENT_QUOTES, 'UTF-8');
        $body = '<div><h2>'.$title.'</h2><p>你申请的《'.$book.'》'.$label.' 已经处理完成。</p><p>网盘链接：<a href="'.$url.'" target="_blank">'.$url.'</a></p><p>链接有效期7天，请及时保存。</p></div>';
        $config = array(
            'debug' => 0,
            'smtp' => isset($this->_cfg['email_smtp']) ? $this->_cfg['email_smtp'] : '',
            'port' => isset($this->_cfg['email_port']) ? $this->_cfg['email_port'] : '',
            'account' => isset($this->_cfg['email_account']) ? $this->_cfg['email_account'] : '',
            'account_name' => isset($this->_cfg['email_account_name']) ? $this->_cfg['email_account_name'] : (isset($this->_cfg['email_account']) ? $this->_cfg['email_account'] : ''),
            'password' => isset($this->_cfg['email_password']) ? $this->_cfg['email_password'] : '',
            'to' => $to,
            'title' => $title,
            'body' => $body,
            'mail_scene' => 'doc_delivery',
        );
        $emailObj = new email();
        return $emailObj->sendemail($config) ? true : false;
    }
}

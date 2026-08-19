<?php
defined('ROOT_PATH') or exit;

class document_delivery_task extends model {
    public $status_map = array(
        1 => '排队中',
        2 => 'PDF处理中',
        3 => '已完成',
        4 => '失败',
        5 => '已取消',
        6 => '等待格式转换',
        7 => '格式转换中',
    );

    function __construct() {
        $this->table = 'document_delivery_task';
        $this->pri = array('id');
        $this->maxid = 'id';
    }

    public function list_arr($where, $orderby, $orderway, $start, $limit, $total) {
        if($start > 1000 && $total > 2000 && $start > $total/2) {
            $orderway = -$orderway;
            $newstart = $total-$start-$limit;
            if($newstart < 0) {
                $limit += $newstart;
                $newstart = 0;
            }
            $list_arr = $this->find_fetch($where, array($orderby => $orderway), $newstart, $limit);
            return array_reverse($list_arr, TRUE);
        }
        return $this->find_fetch($where, array($orderby => $orderway), $start, $limit);
    }

    public function status_label($status) {
        $status = (int)$status;
        return isset($this->status_map[$status]) ? $this->status_map[$status] : '未知';
    }

    public function format_task($task, $avg_minutes = 18) {
        if(empty($task)) return array();
        $task['status'] = (int)$task['status'];
        if(empty($task['output_format']) || !in_array($task['output_format'], array('pdf', 'txt', 'docx', 'md', 'json', 'textpdf', 'zip'))) $task['output_format'] = 'pdf';
        if(empty($task['output_formats'])) $task['output_formats'] = $task['output_format'];
        if($task['output_format'] === 'txt') {
            $task['output_label'] = 'TXT 纯文本';
        }elseif($task['output_format'] === 'docx') {
            $task['output_label'] = 'Word 文档';
        }elseif($task['output_format'] === 'md') {
            $task['output_label'] = 'Markdown';
        }elseif($task['output_format'] === 'json') {
            $task['output_label'] = 'JSON 数据';
        }elseif($task['output_format'] === 'textpdf') {
            $task['output_label'] = '文本 PDF';
        }elseif($task['output_format'] === 'zip') {
            $task['output_label'] = '打包文件';
        }else{
            $task['output_label'] = 'PDF';
        }
        $task['status_label'] = $this->status_label($task['status']);
        $task['created_text'] = empty($task['dateline']) ? '' : date('Y-m-d H:i:s', $task['dateline']);
        $task['updated_text'] = empty($task['updated_at']) ? '' : date('Y-m-d H:i:s', $task['updated_at']);
        $task['finished_text'] = empty($task['finished_at']) ? '' : date('Y-m-d H:i:s', $task['finished_at']);

        if($task['status'] == 1 || $task['status'] == 6) {
            $position = $this->queue_position($task['id'], $task['status']);
            $ahead = max(0, $position - 1);
            $task['queue_position'] = $position;
            $task['ahead_count'] = $ahead;
            $task['estimated_wait_minutes'] = $position * max(1, (int)$avg_minutes);
        }elseif($task['status'] == 2 || $task['status'] == 7) {
            $task['queue_position'] = 0;
            $task['ahead_count'] = 0;
            $task['estimated_wait_minutes'] = max(1, (int)$avg_minutes);
        }else{
            $task['queue_position'] = 0;
            $task['ahead_count'] = 0;
            $task['estimated_wait_minutes'] = 0;
        }
        if($task['status'] == 6 || $task['status'] == 7) {
            $task['queue_stage'] = 'ocr';
            $task['queue_label'] = '格式转换队列';
        }else{
            $task['queue_stage'] = 'pdf';
            $task['queue_label'] = 'PDF 处理队列';
        }
        return $task;
    }

    public function queue_position($id, $status = 1) {
        $id = (int)$id;
        $status = (int)$status;
        if($status == 6 || $status == 7) {
            $processing = $this->find_count(array('status' => 7));
            $pending = $this->find_count(array('status' => 6, 'id' => array('<=' => $id)));
        }else{
            $processing = $this->find_count(array('status' => 2));
            $pending = $this->find_count(array('status' => 1, 'id' => array('<=' => $id)));
        }
        return max(1, (int)$processing + (int)$pending);
    }

    public function active_count_by_user($uid, $ip) {
        if($uid) {
            return $this->find_count(array('uid' => (int)$uid, 'status' => array('IN' => array(1, 2, 6, 7))));
        }
        return $this->find_count(array('ip' => (int)$ip, 'status' => array('IN' => array(1, 2, 6, 7))));
    }

    public function today_count_by_user($uid, $start_time) {
        $uid = (int)$uid;
        if(!$uid) return 0;
        return $this->find_count(array(
            'uid' => $uid,
            'dateline' => array('>=' => (int)$start_time),
            'status' => array('IN' => array(1, 2, 3, 6, 7)),
        ));
    }

    public function today_vip_count_by_user($uid, $start_time) {
        $uid = (int)$uid;
        if(!$uid) return 0;
        return $this->find_count(array(
            'uid' => $uid,
            'dateline' => array('>=' => (int)$start_time),
            'billing_type' => 'vip',
            'status' => array('IN' => array(1, 2, 3, 6, 7)),
        ));
    }

    public function find_active_duplicate($uid, $ip, $cid, $book_id, $keyword, $output_format = '') {
        $where = array('status' => array('IN' => array(1, 2, 6, 7)));
        if($output_format !== '') {
            if(strpos($output_format, ',') !== false) {
                $where['output_formats'] = $output_format;
            }else{
                $where['output_format'] = $output_format;
            }
        }
        if($cid && $book_id) {
            $where['book_cid'] = (int)$cid;
            $where['book_id'] = (int)$book_id;
        }else{
            $where['keyword'] = $keyword;
        }
        if($uid) {
            $where['uid'] = (int)$uid;
        }else{
            $where['ip'] = (int)$ip;
        }
        $list = $this->find_fetch($where, array('id' => -1), 0, 1);
        return empty($list) ? array() : current($list);
    }

    public function next_pending($queue = 'all') {
        $queue = strtolower((string)$queue);
        if($queue === 'ocr') {
            $list = $this->find_fetch(array('status' => 6), array('id' => 1), 0, 1);
            return empty($list) ? array() : current($list);
        }
        if($queue === 'pdf') {
            $list = $this->find_fetch(array('status' => 1), array('id' => 1), 0, 1);
            return empty($list) ? array() : current($list);
        }
        $list = $this->find_fetch(array('status' => 1), array('id' => 1), 0, 1);
        if(empty($list)) $list = $this->find_fetch(array('status' => 6), array('id' => 1), 0, 1);
        return empty($list) ? array() : current($list);
    }

    public function atomic_claim($queue, $worker_id, $lease_id, $now, $lease_seconds) {
        $queue = strtolower((string)$queue);
        $worker_id = preg_replace('/[^A-Za-z0-9._:-]/', '_', (string)$worker_id);
        $lease_id = preg_replace('/[^a-f0-9]/', '', strtolower((string)$lease_id));
        if($worker_id === '' || strlen($lease_id) !== 32) return array();
        $old_status = $queue === 'ocr' ? 6 : 1;
        $new_status = $queue === 'ocr' ? 7 : 2;
        $table = $_ENV['_config']['db']['master']['tablepre'].$this->table;
        $now = (int)$now;
        $lease_until = $now + max(60, (int)$lease_seconds);
        $sql = "UPDATE ".$table." SET status=".$new_status.", worker_id='".$worker_id."', lease_id='".$lease_id."', lease_until=".$lease_until.", heartbeat_at=".$now.", started_at=".$now.", updated_at=".$now." WHERE status=".$old_status." ORDER BY id ASC LIMIT 1";
        if((int)$this->db->exec($sql) !== 1) return array();
        $task = $this->db->fetch_first("SELECT * FROM ".$table." WHERE lease_id='".$lease_id."' LIMIT 1");
        return empty($task) ? array() : $task;
    }

    public function claim_any_atomic($queue, $worker_id, $lease_id, $now, $lease_seconds) {
        if($queue === 'ocr' || $queue === 'pdf') return $this->atomic_claim($queue, $worker_id, $lease_id, $now, $lease_seconds);
        $task = $this->atomic_claim('pdf', $worker_id, $lease_id, $now, $lease_seconds);
        if(!empty($task)) return $task;
        return $this->atomic_claim('ocr', $worker_id, $lease_id, $now, $lease_seconds);
    }

    public function renew_lease($id, $status, $worker_id, $lease_id, $now, $lease_seconds, $message = null) {
        $data = array(
            'lease_until' => (int)$now + max(60, (int)$lease_seconds),
            'heartbeat_at' => (int)$now,
            'updated_at' => (int)$now,
        );
        if($message !== null) $data['message'] = $message;
        $where = array(
            'id' => (int)$id,
            'status' => (int)$status,
            'worker_id' => (string)$worker_id,
            'lease_id' => (string)$lease_id,
        );
        $changed = (int)$this->db->find_update($this->table, $where, $data);
        if($changed === 1) return 1;
        // MySQL reports 0 affected rows when an immediate heartbeat lands in
        // the same second as claim. Force a monotonic one-second extension so
        // a still-valid lease is never mistaken for a stale one.
        $worker_id = preg_replace('/[^A-Za-z0-9._:-]/', '_', (string)$worker_id);
        $lease_id = preg_replace('/[^a-f0-9]/', '', strtolower((string)$lease_id));
        if($worker_id === '' || strlen($lease_id) !== 32) return 0;
        $table = $_ENV['_config']['db']['master']['tablepre'].$this->table;
        $sql = "UPDATE ".$table." SET lease_until=lease_until+1 WHERE id=".(int)$id." AND status=".(int)$status." AND worker_id='".$worker_id."' AND lease_id='".$lease_id."' LIMIT 1";
        return (int)$this->db->exec($sql);
    }

    public function lease_update($task, $worker_id, $lease_id, $data) {
        return (int)$this->db->find_update($this->table, array(
            'id' => (int)$task['id'],
            'status' => (int)$task['status'],
            'worker_id' => (string)$worker_id,
            'lease_id' => (string)$lease_id,
        ), $data);
    }

    public function recover_expired_leases($now, $max_retries = 3) {
        $now = (int)$now;
        $max_retries = max(1, (int)$max_retries);
        $table = $_ENV['_config']['db']['master']['tablepre'].$this->table;
        $rows = $this->db->fetch_all("SELECT * FROM ".$table." WHERE status IN (2,7) AND lease_id<>'' AND lease_until>0 AND lease_until<".$now." ORDER BY lease_until ASC LIMIT 20");
        $failed = array();
        foreach($rows as $task) {
            $id = (int)$task['id'];
            $status = (int)$task['status'];
            $lease_id = preg_replace('/[^a-f0-9]/', '', strtolower((string)$task['lease_id']));
            if(strlen($lease_id) !== 32) continue;
            if((int)$task['retry_count'] < $max_retries) {
                $pending = $status === 7 ? 6 : 1;
                $message = $status === 7 ? '格式转换 Worker 租约过期，任务已自动重新排队。' : '远程 Worker 租约过期，任务已自动重新排队。';
                $sql = "UPDATE ".$table." SET status=".$pending.", worker_id='', lease_id='', lease_until=0, heartbeat_at=0, retry_count=retry_count+1, message='".$message."', updated_at=".$now." WHERE id=".$id." AND status=".$status." AND lease_id='".$lease_id."' LIMIT 1";
                $this->db->exec($sql);
            }else{
                $message = $status === 7 ? '格式转换多次失去 Worker 租约，任务失败。' : '任务多次失去远程 Worker 租约，任务失败。';
                $sql = "UPDATE ".$table." SET status=4, worker_id='', lease_id='', lease_until=0, heartbeat_at=0, message='".$message."', updated_at=".$now.", finished_at=".$now." WHERE id=".$id." AND status=".$status." AND lease_id='".$lease_id."' LIMIT 1";
                if((int)$this->db->exec($sql) === 1) {
                    $task['status'] = 4;
                    $task['message'] = $message;
                    $failed[] = $task;
                }
            }
        }
        return $failed;
    }

    public function timeout_processing($deadline, $ocr_deadline = 0) {
        $deadline = (int)$deadline;
        $ocr_deadline = (int)$ocr_deadline;
        $list = $this->find_fetch(array('status' => 2, 'lease_id' => '', 'updated_at' => array('<' => $deadline)), array('id' => 1), 0, 20);
        if($ocr_deadline > 0) {
            $ocr_list = $this->find_fetch(array('status' => 7, 'lease_id' => '', 'updated_at' => array('<' => $ocr_deadline)), array('id' => 1), 0, 20);
            foreach($ocr_list as $ocr_task) $list[] = $ocr_task;
        }
        $expired = array();
        foreach($list as $task) {
            $old_status = (int)$task['status'];
            $task['status'] = 4;
            $task['message'] = ($old_status == 7 ? '格式转换超时，请稍后重新提交。' : '任务处理超时，请稍后重新提交。');
            $this->update(array(
                'id' => $task['id'],
                'status' => 4,
                'message' => $task['message'],
                'updated_at' => $_ENV['_time'],
                'finished_at' => $_ENV['_time'],
            ));
            $expired[] = $task;
        }
        return $expired;
    }
}

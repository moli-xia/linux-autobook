/* linux-autobook administration panel — single-page front-end. */
(function () {
  "use strict";

  var state = {
    session: null,
    overview: null,
    config: null,
    view: "overview",
    checks: {},
    checkBusy: {},
    logService: "worker",
    logLines: 200,
    logFilter: "",
    logFollow: true,
    activity: [],
    passwords: null,
    jobId: null,
    jobOffsetSeen: 0,
    dirty: {},
    showAdvanced: false,
    timer: null,
  };

  var VIEWS = [
    { id: "overview", icon: "◎", label: "概览", title: "概览", sub: "服务状态、待办事项与主机资源" },
    { id: "setup", icon: "✦", label: "配置向导", title: "配置向导", sub: "按步骤填写必需信息，几分钟完成部署" },
    { id: "config", icon: "⚙", label: "配置", title: "配置", sub: "全部参数，含逐项说明" },
    { id: "checks", icon: "✓", label: "连通性检测", title: "连通性检测", sub: "实测网站、网关、网盘与百度账号" },
    { id: "baidu", icon: "▤", label: "百度登录", title: "百度登录", sub: "扫码获取百度网盘登录态", role: "gateway" },
    { id: "logs", icon: "≡", label: "运行日志", title: "运行日志", sub: "实时查看各服务日志" },
    { id: "activity", icon: "☰", label: "任务记录", title: "任务记录", sub: "最近领取与交付的任务", role: "worker" },
    { id: "maintenance", icon: "⛭", label: "维护", title: "维护", sub: "依赖修复、证书、更新与备份" },
    { id: "account", icon: "⚿", label: "管理账号", title: "管理账号", sub: "修改面板登录用户名与密码" },
  ];

  // ------------------------------------------------------------- helpers
  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: { "Accept": "application/json" } };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (init.method !== "GET" && state.session && state.session.csrf) {
      init.headers["X-CSRF-Token"] = state.session.csrf;
    }
    return fetch(path, init).then(function (response) {
      if (response.status === 401 && path !== "/api/login") {
        showLogin();
        throw new Error("登录已过期，请重新登录");
      }
      var type = response.headers.get("Content-Type") || "";
      if (type.indexOf("application/json") === -1) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      }
      return response.json().then(function (payload) {
        if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
        return payload;
      });
    });
  }

  function toast(message, kind) {
    var node = document.createElement("div");
    node.className = "toast " + (kind || "");
    node.textContent = message;
    $("toasts").appendChild(node);
    setTimeout(function () {
      node.style.opacity = "0";
      setTimeout(function () { node.remove(); }, 250);
    }, kind === "bad" ? 7000 : 3800);
  }

  function confirmDialog(title, body, okLabel) {
    return new Promise(function (resolve) {
      var modal = $("modal");
      $("modal-title").textContent = title;
      $("modal-body").innerHTML = body;
      $("modal-ok").textContent = okLabel || "确定";
      modal.classList.remove("hidden");
      function close(result) {
        modal.classList.add("hidden");
        $("modal-ok").onclick = null;
        $("modal-cancel").onclick = null;
        resolve(result);
      }
      $("modal-ok").onclick = function () { close(true); };
      $("modal-cancel").onclick = function () { close(false); };
    });
  }

  function bytes(value) {
    value = Number(value) || 0;
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return (index === 0 ? value : value.toFixed(value >= 100 ? 0 : 1)) + " " + units[index];
  }

  function duration(seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    if (days) return days + " 天 " + hours + " 小时";
    if (hours) return hours + " 小时 " + minutes + " 分";
    if (minutes) return minutes + " 分 " + (seconds % 60) + " 秒";
    return seconds + " 秒";
  }

  function meter(percent, label) {
    var klass = percent >= 90 ? "bad" : (percent >= 75 ? "warn" : "");
    return '<div class="meter ' + klass + '" data-pct="' + percent + '"><span></span></div>'
      + (label ? '<div class="stat-sub">' + esc(label) + "</div>" : "");
  }

  function applyMeters(root) {
    var nodes = (root || document).querySelectorAll(".meter[data-pct]");
    for (var i = 0; i < nodes.length; i += 1) {
      var pct = Math.max(0, Math.min(100, Number(nodes[i].getAttribute("data-pct")) || 0));
      nodes[i].firstElementChild.style.width = pct + "%";
    }
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return Promise.reject(new Error("clipboard unavailable"));
  }

  function hasRole(role) {
    return state.session && state.session.roles && state.session.roles.indexOf(role) !== -1;
  }

  // --------------------------------------------------------------- login
  function showLogin() {
    stopTimer();
    state.session = null;
    $("boot").classList.add("hidden");
    $("app").classList.add("hidden");
    $("login-view").classList.remove("hidden");
    $("login-user").focus();
  }

  function showApp() {
    $("boot").classList.add("hidden");
    $("login-view").classList.add("hidden");
    $("app").classList.remove("hidden");
  }

  $("login-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var error = $("login-error");
    error.classList.add("hidden");
    api("/api/login", {
      method: "POST",
      body: { username: $("login-user").value, password: $("login-pass").value },
    }).then(function () {
      $("login-pass").value = "";
      return boot();
    }).catch(function (exc) {
      error.textContent = exc.message;
      error.classList.remove("hidden");
    });
  });

  $("logout-btn").addEventListener("click", function () {
    api("/api/logout", { method: "POST" }).finally(showLogin);
  });

  $("refresh-btn").addEventListener("click", function () { refresh(true); });
  $("menu-toggle").addEventListener("click", function () {
    document.querySelector(".sidebar").classList.toggle("open");
  });

  // ---------------------------------------------------------------- boot
  function boot() {
    return api("/api/session").then(function (session) {
      if (!session.authenticated) { showLogin(); return; }
      state.session = session;
      $("panel-version").textContent = session.version;
      $("who-user").textContent = "当前用户 " + session.username;
      $("role-badge").textContent = { all: "网关 + Worker", gateway: "仅网关", worker: "仅 Worker" }[session.role] || session.role;
      showApp();
      setView(session.must_change_password ? "account" : state.view);
      return Promise.all([loadConfig(), refresh(true)]);
    });
  }

  function renderNav() {
    var nav = $("nav");
    nav.innerHTML = VIEWS.filter(function (view) {
      return !view.role || hasRole(view.role);
    }).map(function (view) {
      var badge = "";
      if (view.id === "config" || view.id === "overview") {
        var pending = pendingIssueCount();
        if (pending > 0) badge = '<span class="nav-dot"></span>';
      }
      return '<button type="button" data-view="' + view.id + '"'
        + (state.view === view.id ? ' class="active"' : "")
        + '><span class="nav-icon">' + view.icon + "</span>" + esc(view.label) + badge + "</button>";
    }).join("");
  }

  function pendingIssueCount() {
    if (!state.overview || !state.overview.issues) return 0;
    var total = 0;
    Object.keys(state.overview.issues).forEach(function (role) {
      total += state.overview.issues[role].length;
    });
    return total;
  }

  document.addEventListener("click", function (event) {
    var navButton = event.target.closest("[data-view]");
    if (navButton) {
      setView(navButton.getAttribute("data-view"));
      document.querySelector(".sidebar").classList.remove("open");
      return;
    }
    var action = event.target.closest("[data-action]");
    if (action) handleAction(action, event);
  });

  function setView(id) {
    state.view = id;
    var meta = VIEWS.filter(function (view) { return view.id === id; })[0] || VIEWS[0];
    $("page-title").textContent = meta.title;
    $("page-sub").textContent = meta.sub;
    renderNav();
    render();
    if (id === "logs") loadLogs();
    if (id === "activity") loadActivity();
    if (id === "maintenance" && !state.passwords) loadPasswords();
  }

  // ------------------------------------------------------------- polling
  function startTimer() {
    stopTimer();
    state.timer = setInterval(function () {
      if (document.hidden) return;
      refresh(false);
      if (state.view === "logs" && state.logFollow) loadLogs();
      if (state.view === "baidu") loadBaidu();
      if (state.jobId) pollJob();
    }, 5000);
  }

  function stopTimer() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  function refresh(showToast) {
    return api("/api/overview").then(function (overview) {
      state.overview = overview;
      renderNav();
      if (["overview", "setup", "checks", "maintenance", "baidu"].indexOf(state.view) !== -1) render();
      renderBanners();
      if (showToast) toast("状态已刷新", "ok");
      startTimer();
    }).catch(function (exc) {
      if (showToast) toast(exc.message, "bad");
    });
  }

  function loadConfig() {
    return api("/api/config").then(function (config) {
      state.config = config;
      if (state.view === "config" || state.view === "setup") render();
    });
  }

  // -------------------------------------------------------------- render
  function render() {
    var container = $("views");
    var html = "";
    switch (state.view) {
      case "overview": html = viewOverview(); break;
      case "setup": html = viewSetup(); break;
      case "config": html = viewConfig(); break;
      case "checks": html = viewChecks(); break;
      case "baidu": html = viewBaidu(); break;
      case "logs": html = viewLogs(); break;
      case "activity": html = viewActivity(); break;
      case "maintenance": html = viewMaintenance(); break;
      case "account": html = viewAccount(); break;
      default: html = viewOverview();
    }
    container.innerHTML = html;
    applyMeters(container);
    if (state.view === "logs") {
      var console_ = $("log-console");
      if (console_ && state.logFollow) console_.scrollTop = console_.scrollHeight;
    }
  }

  function renderBanners() {
    var area = $("banner-area");
    var items = [];
    if (state.overview && state.overview.must_change_password) {
      items.push('<div class="banner warn"><span>⚠</span><div>当前仍在使用默认密码 <code>admin</code>，'
        + '任何人都能接管这台服务器。请立即修改。</div>'
        + '<div class="banner-actions"><button class="btn small primary" data-view="account">去修改</button></div></div>');
    }
    var pending = pendingIssueCount();
    if (pending > 0 && state.view !== "setup") {
      items.push('<div class="banner info"><span>ℹ</span><div>还有 <strong>' + pending
        + '</strong> 项必填配置未完成，服务无法启动。</div>'
        + '<div class="banner-actions"><button class="btn small primary" data-view="setup">打开配置向导</button></div></div>');
    }
    area.innerHTML = items.join("");
  }

  // ------------------------------------------------------------ overview
  function viewOverview() {
    var overview = state.overview;
    if (!overview) return '<div class="card">正在载入…</div>';
    var system = overview.system || {};
    var memory = system.memory || {};
    var disk = system.disk || {};
    var memoryPct = memory.total ? Math.round(memory.used / memory.total * 100) : 0;
    var diskPct = disk.total ? Math.round(disk.used / disk.total * 100) : 0;
    var loadPct = system.cpu_count ? Math.round((system.load || [0])[0] / system.cpu_count * 100) : 0;

    var services = (overview.services || []).map(serviceCard).join("");

    var todo = "";
    Object.keys(overview.issues || {}).forEach(function (role) {
      overview.issues[role].forEach(function (issue) {
        todo += '<li class="error"><span class="pill bad"><span class="dot"></span>'
          + (role === "gateway" ? "网关" : "Worker") + "</span>"
          + '<div class="todo-body"><div class="todo-title">' + esc(issue.message) + "</div>"
          + (issue.hint ? '<div class="todo-hint">' + esc(issue.hint) + "</div>" : "")
          + "</div>"
          + '<button class="btn small ghost" data-action="jump-config" data-key="' + esc(issue.key) + '">去填写</button></li>';
      });
    });

    return ""
      + '<div class="grid cols-4">'
      + stat("主机", esc(system.hostname || "-"), esc(system.os || ""))
      + stat("CPU 负载", (system.load || [0])[0] + " / " + (system.cpu_count || 1) + " 核", "", meter(Math.min(100, loadPct)))
      + stat("内存", bytes(memory.used) + " / " + bytes(memory.total), "", meter(memoryPct, "剩余 " + bytes(memory.available)))
      + stat("磁盘", bytes(disk.used) + " / " + bytes(disk.total), "", meter(diskPct, "剩余 " + bytes(disk.free)))
      + "</div>"
      + '<div class="grid cols-2">' + services + "</div>"
      + '<div class="card"><div class="card-head"><div><h3>待办事项</h3>'
      + "<p>下面每一项都会阻止服务正常启动，全部处理完即可开始跑任务。</p></div>"
      + '<div class="card-actions"><button class="btn small primary" data-view="setup">配置向导</button>'
      + '<button class="btn small ghost" data-view="checks">连通性检测</button></div></div>'
      + (todo ? '<ul class="todo">' + todo + "</ul>"
              : '<div class="empty-state">✅ 所有必填配置均已完成，可以直接启动服务。</div>')
      + "</div>"
      + '<div class="card"><div class="card-head"><div><h3>系统信息</h3><p>运行环境与外部依赖。</p></div></div>'
      + '<dl class="kv">'
      + "<dt>面板版本</dt><dd>" + esc(overview.version) + "</dd>"
      + "<dt>本机角色</dt><dd>" + esc({ all: "网关 + Worker（单机全功能）", gateway: "仅百度下载网关", worker: "仅任务 Worker" }[overview.role] || overview.role) + "</dd>"
      + "<dt>访问地址</dt><dd>" + esc(overview.public_host || location.hostname) + "</dd>"
      + "<dt>系统</dt><dd>" + esc(system.os || "-") + " · 内核 " + esc(system.kernel || "-") + "</dd>"
      + "<dt>Python</dt><dd>" + esc(system.python || "-") + "</dd>"
      + "<dt>已运行</dt><dd>" + duration(system.uptime_seconds) + "</dd>"
      + "<dt>外部命令</dt><dd>" + (overview.binaries || []).map(function (item) {
          return '<span class="pill ' + (item.found ? "ok" : "bad") + '">' + esc(item.name) + (item.found ? " 已安装" : " 缺失") + "</span>";
        }).join(" ") + "</dd>"
      + ((overview.runtime_dirs || []).length ? "<dt>工作目录</dt><dd>" + overview.runtime_dirs.map(function (item) {
          return esc(item.path) + " — " + bytes(item.bytes) + "（" + item.entries + " 个文件）";
        }).join("<br>") + "</dd>" : "")
      + "</dl></div>";
  }

  function stat(label, value, sub, extra) {
    return '<div class="stat"><div class="stat-label">' + esc(label) + "</div>"
      + '<div class="stat-value">' + value + "</div>"
      + (sub ? '<div class="stat-sub">' + esc(sub) + "</div>" : "")
      + (extra || "") + "</div>";
  }

  function serviceCard(service) {
    var running = service.running;
    var pill = service.installed
      ? '<span class="pill ' + (running ? "ok" : "bad") + '"><span class="dot ' + (running ? "live" : "") + '"></span>'
        + (running ? "运行中" : (service.active === "failed" ? "启动失败" : "已停止")) + "</span>"
      : '<span class="pill">未安装</span>';
    var issues = (state.overview.issues || {})[service.name] || [];
    var blocked = issues.length > 0 && service.name !== "admin";
    var actions = "";
    if (service.installed) {
      if (service.name === "admin") {
        actions = '<button class="btn small ghost" data-action="service" data-service="admin" data-op="restart">重启面板</button>';
      } else if (running) {
        actions = '<button class="btn small ghost" data-action="service" data-service="' + service.name + '" data-op="restart">重启</button>'
          + '<button class="btn small danger" data-action="service" data-service="' + service.name + '" data-op="stop">停止</button>';
      } else {
        actions = '<button class="btn small ok" data-action="service" data-service="' + service.name + '" data-op="start"'
          + (blocked ? " disabled" : "") + ">启动</button>";
      }
      actions += '<button class="btn small ghost" data-action="open-log" data-service="' + service.name + '">日志</button>';
    }
    return '<div class="card service-card"><div class="service-top"><div>'
      + "<h3>" + esc(service.label) + "</h3><p>" + esc(service.summary) + "</p></div>"
      + '<div class="card-actions">' + pill + "</div></div>"
      + '<div class="service-meta">'
      + "<span>开机自启：" + esc(service.enabled === "enabled" ? "已开启" : (service.enabled === "not-installed" ? "—" : "未开启")) + "</span>"
      + (running ? "<span>已运行 " + duration(service.uptime_seconds) + "</span>" : "")
      + (running && service.memory_bytes ? "<span>内存 " + bytes(service.memory_bytes) + "</span>" : "")
      + (service.restarts ? "<span>重启次数 " + service.restarts + "</span>" : "")
      + "</div>"
      + (blocked ? '<div class="banner warn"><span>⚠</span><div>还有 ' + issues.length
          + " 项配置未完成，启动按钮已锁定。</div></div>" : "")
      + '<div class="service-actions">' + actions + "</div></div>";
  }

  // --------------------------------------------------------------- setup
  function viewSetup() {
    if (!state.config) return '<div class="card">正在载入配置…</div>';
    var roles = state.session.roles || [];
    var steps = [];
    var index = 0;

    function essentialGroups(role) {
      return (state.config.schema || []).filter(function (group) {
        return group.target === role && group.essential;
      });
    }

    if (roles.indexOf("gateway") !== -1) {
      essentialGroups("gateway").forEach(function (group) {
        index += 1;
        steps.push(stepCard(index, group));
      });
      index += 1;
      steps.push('<div class="step done"><div class="step-index">✓</div>'
        + '<div class="step-body"><h4>网关共享令牌</h4>'
        + "<p>安装时已自动生成一个随机令牌。每台 Worker 都要填写<strong>完全相同</strong>的令牌才能连上网关，"
        + "点下面的按钮显示并复制它。</p>"
        + '<div class="copyable"><code id="token-display">••••••••••••••••••••••••••••••••</code>'
        + '<button class="btn small ghost" data-action="reveal-token">显示</button>'
        + '<button class="btn small ghost" data-action="copy-token">复制</button>'
        + '<button class="btn small ghost" data-action="gen-token">重新生成</button></div>'
        + '<p class="stat-sub">重新生成后，必须把新令牌同步到所有 Worker，否则它们会连不上网关。</p>'
        + "</div></div>");
      index += 1;
      var baidu = (state.overview && state.overview.baidu) || {};
      steps.push('<div class="step ' + (baidu.logged_in ? "done" : "") + '">'
        + '<div class="step-index">' + (baidu.logged_in ? "✓" : index) + "</div>"
        + '<div class="step-body"><h4>百度网盘扫码登录</h4>'
        + "<p>网关需要一个已加入目标群的百度账号。点下面的按钮生成二维码，用百度网盘 App 扫一下即可，无需手工复制 Cookie。</p>"
        + '<div class="step-actions"><button class="btn ' + (baidu.logged_in ? "ghost" : "primary") + '" data-view="baidu">'
        + (baidu.logged_in ? "已登录，重新扫码" : "去扫码登录") + "</button></div></div></div>");
    }
    if (roles.indexOf("worker") !== -1) {
      essentialGroups("worker").forEach(function (group) {
        index += 1;
        steps.push(stepCard(index, group));
      });
    }
    index += 1;
    var ready = pendingIssueCount() === 0;
    steps.push('<div class="step ' + (ready ? "done" : "") + '">'
      + '<div class="step-index">' + (ready ? "✓" : index) + "</div>"
      + '<div class="step-body"><h4>检测并启动</h4>'
      + "<p>先做一次连通性检测确认令牌和网络都正常，再启动服务。配置不完整时面板会拦住启动，避免服务反复崩溃重启。</p>"
      + '<div class="step-actions">'
      + '<button class="btn ghost" data-view="checks">运行连通性检测</button>'
      + roles.map(function (role) {
          return '<button class="btn primary" data-action="service" data-service="' + role + '" data-op="start"'
            + (((state.overview.issues || {})[role] || []).length ? " disabled" : "") + ">启动"
            + (role === "gateway" ? "网关" : "Worker") + "</button>";
        }).join("")
      + "</div></div></div>");

    return '<div class="card"><div class="card-head"><div><h3>按步骤完成部署</h3>'
      + "<p>每一步只需要填几个字段，填完点保存即可。所有参数都能在「配置」页面随时修改。</p></div>"
      + '<div class="card-actions"><button class="btn small ghost" data-action="save-config">保存本页全部修改</button></div></div>'
      + '<div class="steps">' + steps.join("") + "</div></div>";
  }

  function stepCard(index, group) {
    var fields = group.fields.filter(function (field) { return field.essential; });
    var blocking = 0;
    var issues = (state.overview && state.overview.issues) || {};
    Object.keys(issues).forEach(function (role) {
      issues[role].forEach(function (issue) { if (issue.group === group.id) blocking += 1; });
    });
    var done = blocking === 0 && fields.every(function (field) {
      return field.secret ? state.config.secrets_set[field.key] : (!field.required || !!state.config.values[field.key]);
    });
    return '<div class="step ' + (done ? "done" : "") + '">'
      + '<div class="step-index">' + (done ? "✓" : index) + "</div>"
      + '<div class="step-body"><h4>' + esc(group.title) + "</h4><p>" + esc(group.summary) + "</p>"
      + '<div class="grid cols-2">' + fields.map(fieldControl).join("") + "</div>"
      + '<div class="step-actions"><button class="btn primary small" data-action="save-config">保存</button></div>'
      + "</div></div>";
  }

  // -------------------------------------------------------------- config
  function viewConfig() {
    if (!state.config) return '<div class="card">正在载入配置…</div>';
    var groups = (state.config.schema || []).filter(function (group) {
      return state.showAdvanced || group.essential;
    });
    var body = groups.map(function (group) {
      var fields = group.fields.filter(function (field) {
        return state.showAdvanced || field.essential || field.required;
      });
      if (!fields.length) return "";
      var roleLabel = group.target === "gateway" ? "网关" : "Worker";
      return '<details class="group" open><summary><span class="pill info">' + roleLabel + "</span>"
        + esc(group.title) + '<span class="group-sum">' + esc(group.summary) + "</span></summary>"
        + '<div class="group-body"><div class="grid cols-2">' + fields.map(fieldControl).join("") + "</div></div></details>";
    }).join("");

    return '<div class="card"><div class="card-head"><div><h3>参数设置</h3>'
      + "<p>标有必填的项目必须填写。敏感项保存后不再显示，留空提交表示保持原值不变。</p></div>"
      + '<div class="card-actions"><div class="seg">'
      + '<button data-action="mode" data-mode="basic"' + (state.showAdvanced ? "" : ' class="active"') + ">常用</button>"
      + '<button data-action="mode" data-mode="advanced"' + (state.showAdvanced ? ' class="active"' : "") + ">全部参数</button>"
      + "</div></div></div>" + body + "</div>"
      + '<div class="sticky-save"><span class="save-note">修改后记得点保存；影响运行中服务的改动需要重启服务才会生效。</span>'
      + '<button class="btn ghost" data-action="reload-config">放弃修改</button>'
      + '<button class="btn primary" data-action="save-config">保存配置</button></div>';
  }

  function fieldControl(field) {
    var value = state.config.values[field.key];
    if (value === undefined || value === null) value = field.default || "";
    if (state.dirty[field.key] !== undefined) value = state.dirty[field.key];
    var badge = "";
    if (field.required) {
      var filled = field.secret ? state.config.secrets_set[field.key] : !!value;
      badge = '<span class="pill ' + (filled ? "ok" : "bad") + '">' + (filled ? "已填写" : "必填") + "</span>";
    }
    var control;
    if (field.kind === "select") {
      control = '<select data-field="' + esc(field.key) + '">' + field.options.map(function (option) {
        return '<option value="' + esc(option.value) + '"' + (String(value) === option.value ? " selected" : "") + ">"
          + esc(option.label) + "</option>";
      }).join("") + "</select>";
    } else if (field.secret) {
      var placeholder = state.config.secrets_set[field.key] ? "已保存，留空则保持不变" : (field.placeholder || "尚未填写");
      control = '<div class="input-row"><input type="password" autocomplete="new-password" data-field="' + esc(field.key)
        + '" value="" placeholder="' + esc(placeholder) + '">'
        + (field.key === "BAIDU_GATEWAY_TOKEN"
            ? '<button class="btn ghost small" type="button" data-action="fill-token">显示当前值</button>'
              + '<button class="btn ghost small" type="button" data-action="gen-token">重新生成</button>' : "")
        + "</div>";
    } else {
      var inputType = field.kind === "number" ? "number" : "text";
      var attrs = "";
      if (field.min !== null && field.min !== undefined) attrs += ' min="' + field.min + '"';
      if (field.max !== null && field.max !== undefined) attrs += ' max="' + field.max + '"';
      control = '<div class="input-row"><input type="' + inputType + '" data-field="' + esc(field.key)
        + '" value="' + esc(value) + '" placeholder="' + esc(field.placeholder || field.default || "") + '"'
        + attrs + " spellcheck=\"false\" autocomplete=\"off\">"
        + (field.unit ? '<span class="suffix">' + esc(field.unit) + "</span>" : "") + "</div>";
    }
    return '<div class="field" data-field-wrap="' + esc(field.key) + '">'
      + "<label>" + esc(field.label) + badge + "</label>" + control
      + (field.help ? '<small class="help">' + esc(field.help) + "</small>" : "")
      + (field.example ? '<div class="example">示例：<code>' + esc(field.example) + "</code></div>" : "")
      + "</div>";
  }

  document.addEventListener("input", function (event) {
    var input = event.target.closest("[data-field]");
    if (!input) return;
    state.dirty[input.getAttribute("data-field")] = input.value;
  });
  document.addEventListener("change", function (event) {
    var input = event.target.closest("[data-field]");
    if (!input) return;
    state.dirty[input.getAttribute("data-field")] = input.value;
  });

  function collectValues() {
    var values = {};
    var inputs = document.querySelectorAll("[data-field]");
    for (var i = 0; i < inputs.length; i += 1) {
      values[inputs[i].getAttribute("data-field")] = inputs[i].value;
    }
    return values;
  }

  function saveConfig() {
    var values = collectValues();
    return api("/api/config", { method: "POST", body: { values: values } }).then(function (result) {
      state.dirty = {};
      toast(result.message || "配置已保存", "ok");
      if ((result.restart_needed || []).length) {
        toast("配置已变更，请重启 " + result.restart_needed.join("、") + " 服务使其生效", "warn");
      }
      return Promise.all([loadConfig(), refresh(false)]);
    }).catch(function (exc) { toast(exc.message, "bad"); });
  }

  // -------------------------------------------------------------- checks
  function viewChecks() {
    var roles = (state.session && state.session.roles) || [];
    return roles.map(function (role) {
      var label = role === "gateway" ? "百度下载网关" : "任务 Worker";
      var results = state.checks[role];
      var busy = state.checkBusy[role];
      var body;
      if (busy) {
        body = '<div class="empty-state">正在检测，请稍候（最长约 1 分钟）…</div>';
      } else if (!results) {
        body = '<div class="empty-state">尚未运行检测。检测只读取配置并发起只读请求，不会领取任务或修改任何数据。</div>';
      } else {
        body = results.map(function (item) {
          var icon = { ok: "✓", fail: "✕", warn: "!", skip: "–" }[item.status] || "?";
          return '<div class="check-item"><div class="check-icon ' + item.status + '">' + icon + "</div>"
            + '<div class="check-body"><div class="check-title">' + esc(item.title) + "</div>"
            + (item.detail ? '<div class="check-detail">' + esc(item.detail) + "</div>" : "")
            + (item.hint ? '<div class="check-hint">建议：' + esc(item.hint) + "</div>" : "")
            + "</div></div>";
        }).join("");
      }
      var summary = "";
      if (results) {
        var failed = results.filter(function (item) { return item.status === "fail"; }).length;
        var warned = results.filter(function (item) { return item.status === "warn"; }).length;
        summary = failed
          ? '<span class="pill bad">' + failed + " 项失败</span>"
          : (warned ? '<span class="pill warn">' + warned + " 项警告</span>" : '<span class="pill ok">全部通过</span>');
      }
      return '<div class="card"><div class="card-head"><div><h3>' + esc(label) + " 连通性检测</h3>"
        + "<p>逐项验证令牌、证书、网络与账号，直接告诉你是哪一项配错了。</p></div>"
        + '<div class="card-actions">' + summary
        + '<button class="btn primary small" data-action="run-check" data-role="' + role + '"'
        + (busy ? " disabled" : "") + ">" + (busy ? "检测中…" : "开始检测") + "</button></div></div>"
        + body + "</div>";
    }).join("");
  }

  function runCheck(role) {
    state.checkBusy[role] = true;
    render();
    api("/api/check", { method: "POST", body: { role: role } }).then(function (result) {
      state.checks[role] = result.results;
      var failed = result.results.filter(function (item) { return item.status === "fail"; }).length;
      toast(failed ? role + " 检测发现 " + failed + " 个问题" : role + " 检测全部通过", failed ? "bad" : "ok");
    }).catch(function (exc) {
      toast(exc.message, "bad");
    }).finally(function () {
      state.checkBusy[role] = false;
      render();
    });
  }

  // --------------------------------------------------------------- baidu
  function viewBaidu() {
    var baidu = (state.overview && state.overview.baidu) || {};
    var phaseLabel = {
      idle: "未开始", starting: "正在生成二维码", waiting: "等待扫码",
      scanned: "等待手机确认", confirming: "正在建立会话", success: "登录成功", failed: "登录失败",
    }[baidu.phase] || baidu.phase || "未开始";
    var pillClass = baidu.phase === "success" ? "ok" : (baidu.phase === "failed" ? "bad" : (baidu.running ? "info" : ""));
    return '<div class="card"><div class="card-head"><div><h3>百度网盘登录</h3>'
      + "<p>网关需要一个已加入目标群的百度账号。扫码得到的登录态会以 0600 权限保存在服务器本地，不会显示在页面上。</p></div>"
      + '<div class="card-actions"><span class="pill ' + pillClass + '">' + esc(phaseLabel) + "</span></div></div>"
      + '<div class="banner ' + (baidu.logged_in ? "" : "warn") + '"><span>' + (baidu.logged_in ? "✓" : "⚠") + "</span><div>"
      + (baidu.logged_in
          ? "已保存登录凭据（" + duration(baidu.credentials_age) + "前更新）。登录态失效时重新扫码即可。"
          : "尚未登录。网关在登录之前无法启动。")
      + "</div></div>"
      + '<div class="qr-box">'
      + (baidu.has_qr
          ? '<img class="qr-img" alt="百度登录二维码" src="/api/baidu/qr.png?t=' + (baidu.qr_mtime || 0) + '">'
          : '<div class="qr-img"></div>')
      + '<div class="qr-side">'
      + "<h4>扫码步骤</h4>"
      + '<div class="steps">'
      + '<div class="step"><div class="step-index">1</div><div class="step-body"><h4>生成二维码</h4><p>点击下方按钮，几秒后左侧会出现二维码。</p></div></div>'
      + '<div class="step"><div class="step-index">2</div><div class="step-body"><h4>手机扫码</h4><p>打开百度网盘 App → 右上角「+」→ 扫一扫，对准左侧二维码。</p></div></div>'
      + '<div class="step"><div class="step-index">3</div><div class="step-body"><h4>确认登录</h4><p>在手机上点「确认登录」。面板会自动检测结果并重启网关服务。</p></div></div>'
      + "</div>"
      + '<div class="service-actions"><button class="btn primary" data-action="baidu-start"'
      + (baidu.running ? " disabled" : "") + ">" + (baidu.running ? "进行中…" : "生成二维码") + "</button>"
      + (baidu.running ? '<button class="btn ghost" data-action="baidu-cancel">取消</button>' : "")
      + "</div>"
      + "<p class=\"stat-sub\">" + esc(baidu.message || "") + "</p>"
      + "</div></div>"
      + (baidu.log ? '<h4 class="stat-label">登录输出</h4><pre class="console short">' + esc(baidu.log) + "</pre>" : "")
      + "</div>";
  }

  function loadBaidu() {
    return api("/api/baidu/status").then(function (status) {
      if (state.overview) state.overview.baidu = status;
      if (state.view === "baidu") render();
    }).catch(function () {});
  }

  // ---------------------------------------------------------------- logs
  function viewLogs() {
    var options = (state.overview ? state.overview.services : []).map(function (service) {
      return '<option value="' + service.name + '"' + (state.logService === service.name ? " selected" : "") + ">"
        + esc(service.label) + "</option>";
    }).join("");
    return '<div class="card"><div class="card-head"><div><h3>运行日志</h3>'
      + "<p>直接读取 systemd 日志，下载链接与令牌等敏感内容会自动隐藏。</p></div></div>"
      + '<div class="log-toolbar">'
      + '<select data-action="log-service">' + options + "</select>"
      + '<select data-action="log-lines">'
      + [100, 200, 500, 1000].map(function (count) {
          return '<option value="' + count + '"' + (state.logLines === count ? " selected" : "") + ">最近 " + count + " 行</option>";
        }).join("") + "</select>"
      + '<input type="search" data-action="log-filter" placeholder="按关键字过滤" value="' + esc(state.logFilter) + '">'
      + '<label class="checkline"><input type="checkbox" data-action="log-follow"' + (state.logFollow ? " checked" : "") + "> 自动刷新</label>"
      + '<button class="btn small ghost" data-action="log-reload">立即刷新</button>'
      + "</div>"
      + '<pre class="console" id="log-console">' + esc(state.logText || "正在载入日志…") + "</pre></div>";
  }

  function loadLogs() {
    var query = "?service=" + encodeURIComponent(state.logService)
      + "&lines=" + state.logLines
      + "&grep=" + encodeURIComponent(state.logFilter);
    return api("/api/logs" + query).then(function (result) {
      state.logText = result.text || "（暂无日志）";
      if (state.view === "logs") {
        var node = $("log-console");
        if (node) {
          var atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
          node.textContent = state.logText;
          if (state.logFollow || atBottom) node.scrollTop = node.scrollHeight;
        }
      }
    }).catch(function (exc) { toast(exc.message, "bad"); });
  }

  // ------------------------------------------------------------ activity
  function viewActivity() {
    var rows = (state.activity || []).map(function (task) {
      var badge = { completed: '<span class="pill ok">已完成</span>', failed: '<span class="pill bad">失败</span>' }[task.status]
        || '<span class="pill info"><span class="dot live"></span>处理中</span>';
      return "<tr><td>#" + esc(task.id) + "</td><td>" + esc(task.title || "-") + "</td><td>" + badge + "</td>"
        + "<td>" + esc(task.message || "") + (task.url ? '<br><a href="' + esc(task.url) + '" target="_blank" rel="noreferrer noopener">交付链接</a>' : "")
        + "</td><td>" + esc(task.updated || "") + "</td></tr>";
    }).join("");
    return '<div class="card"><div class="card-head"><div><h3>最近任务</h3>'
      + "<p>从 Worker 日志还原的任务记录，最多显示最近 25 条。</p></div>"
      + '<div class="card-actions"><button class="btn small ghost" data-action="reload-activity">刷新</button></div></div>'
      + (rows
          ? '<div class="table-wrap"><table class="table"><thead><tr><th>任务</th><th>书名</th><th>状态</th><th>最新进度</th><th>时间</th></tr></thead><tbody>'
            + rows + "</tbody></table></div>"
          : '<div class="empty-state">暂无任务记录。Worker 启动并领到任务后这里会自动出现。</div>')
      + "</div>";
  }

  function loadActivity() {
    return api("/api/activity").then(function (result) {
      state.activity = result.tasks || [];
      if (state.view === "activity") render();
    }).catch(function (exc) { toast(exc.message, "bad"); });
  }

  // --------------------------------------------------------- maintenance
  function viewMaintenance() {
    var job = state.jobSnapshot;
    var passwordCard = "";
    if (hasRole("worker")) {
      var dict = state.passwords || { count: 0, content: "", path: "" };
      passwordCard = '<div class="card"><div class="card-head"><div><h3>解压密码字典</h3>'
        + "<p>加密压缩包会按这里的顺序逐个尝试密码，每行一个。当前共 " + dict.count + " 条，文件位置 <code>"
        + esc(dict.path) + "</code>。</p></div>"
        + '<div class="card-actions"><button class="btn small primary" data-action="save-passwords">保存字典</button></div></div>'
        + '<div class="field"><textarea id="password-dict" spellcheck="false">' + esc(dict.content) + "</textarea></div></div>";
    }

    var roleCard = '<div class="card"><div class="card-head"><div><h3>本机角色</h3>'
      + "<p>当前为 <strong>" + esc({ all: "网关 + Worker", gateway: "仅网关", worker: "仅 Worker" }[state.session.role]) + "</strong>。"
      + "切换角色会重新执行安装脚本，安装或移除对应的 systemd 服务，已有配置会保留。</p></div></div>"
      + '<div class="service-actions">'
      + ["all", "gateway", "worker"].map(function (role) {
          var label = { all: "网关 + Worker", gateway: "仅网关", worker: "仅 Worker" }[role];
          return '<button class="btn ' + (state.session.role === role ? "ghost" : "ghost") + ' small" data-action="switch-role" data-role="'
            + role + '"' + (state.session.role === role ? " disabled" : "") + ">切换为" + label + "</button>";
        }).join("") + "</div></div>";

    return '<div class="card"><div class="card-head"><div><h3>一键维护</h3>'
      + "<p>常见问题都可以在这里一键处理，执行过程会实时显示在下方控制台。</p></div></div>"
      + '<div class="grid cols-2">'
      + maintenanceItem("dependencies", "修复系统依赖", "重新安装 7z、aria2、openssl 等外部命令。缺少命令导致解压或下载失败时使用。")
      + maintenanceItem("permissions", "修复目录权限", "把 runtime 目录和密码字典的属主改回服务账号。服务报「无权限」时使用。")
      + (hasRole("gateway") ? maintenanceItem("gateway_cert", "重新生成网关证书", "证书损坏或更换 IP/域名后使用。生成后需要把新的 gateway.crt 分发给所有 Worker。") : "")
      + maintenanceItem("backup", "备份配置", "把 /etc/linux-autobook 打包到 /var/backups/linux-autobook，升级前建议先备份。")
      + maintenanceItem("update", "更新程序", "从 GitHub 拉取最新代码并重新安装，保留全部配置与数据。")
      + maintenanceItem("restart_panel", "重启管理面板", "面板本身异常时使用，重启期间页面会短暂无法访问。")
      + "</div></div>"
      + (job ? '<div class="card"><div class="card-head"><div><h3>' + esc(job.title) + "</h3>"
          + "<p>状态：" + esc({ running: "执行中", success: "已完成", failed: "失败" }[job.status] || job.status)
          + " · 用时 " + job.elapsed + " 秒</p></div>"
          + '<div class="card-actions"><button class="btn small ghost" data-action="close-job">关闭</button></div></div>'
          + '<pre class="console" id="job-console">' + esc(job.log || "") + "</pre></div>" : "")
      + roleCard
      + passwordCard;
  }

  function maintenanceItem(action, title, description) {
    return '<div class="stat"><div class="stat-value" data-mini>' + esc(title) + "</div>"
      + '<div class="stat-sub">' + esc(description) + "</div>"
      + '<div class="service-actions"><button class="btn small ghost" data-action="maintain" data-op="'
      + action + '">执行</button></div></div>';
  }

  function loadPasswords() {
    return api("/api/passwords").then(function (result) {
      state.passwords = result;
      if (state.view === "maintenance") render();
    }).catch(function () {});
  }

  function pollJob() {
    if (!state.jobId) return;
    api("/api/jobs/" + state.jobId).then(function (job) {
      state.jobSnapshot = job;
      if (state.view === "maintenance") {
        var node = $("job-console");
        if (node) {
          var atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 60;
          node.textContent = job.log || "";
          if (atBottom) node.scrollTop = node.scrollHeight;
          var head = node.parentElement.querySelector(".card-head p");
          if (head) {
            head.textContent = "状态：" + ({ running: "执行中", success: "已完成", failed: "失败" }[job.status] || job.status)
              + " · 用时 " + job.elapsed + " 秒";
          }
        } else {
          render();
        }
      }
      if (job.status !== "running") {
        state.jobId = null;
        toast(job.title + (job.status === "success" ? " 已完成" : " 失败，请查看输出"), job.status === "success" ? "ok" : "bad");
        refresh(false);
      }
    }).catch(function () { state.jobId = null; });
  }

  // ------------------------------------------------------------- account
  function viewAccount() {
    return '<div class="card"><div class="card-head"><div><h3>修改登录账号</h3>'
      + "<p>面板运行在公网端口上，请务必使用强密码。修改成功后所有已登录的会话都会失效。</p></div></div>"
      + '<div class="grid cols-2">'
      + '<div class="field"><label>用户名</label><input id="acc-user" value="' + esc(state.session.username) + '" autocomplete="username"></div>'
      + '<div class="field"><label>当前密码</label><input id="acc-current" type="password" autocomplete="current-password"></div>'
      + '<div class="field"><label>新密码</label><input id="acc-new" type="password" autocomplete="new-password">'
      + '<small class="help">至少 8 位，建议混合大小写字母、数字与符号。</small></div>'
      + '<div class="field"><label>重复新密码</label><input id="acc-new2" type="password" autocomplete="new-password"></div>'
      + "</div>"
      + '<div class="service-actions"><button class="btn primary" data-action="save-account">保存并重新登录</button></div></div>'
      + '<div class="card"><div class="card-head"><div><h3>会话</h3><p>当前有 '
      + ((state.overview && state.overview.sessions) || 1) + " 个活动会话，登录状态默认保持 8 小时。</p></div></div></div>";
  }

  // -------------------------------------------------------------- actions
  function handleAction(node, event) {
    var action = node.getAttribute("data-action");
    if (action === "mode") {
      state.showAdvanced = node.getAttribute("data-mode") === "advanced";
      render();
    } else if (action === "save-config") {
      saveConfig();
    } else if (action === "reload-config") {
      state.dirty = {};
      loadConfig().then(function () { toast("已放弃未保存的修改"); render(); });
    } else if (action === "gen-token") {
      api("/api/config/token", { method: "POST" }).then(function (result) {
        toast(result.message, "ok");
        return loadConfig();
      }).then(function () { render(); }).catch(function (exc) { toast(exc.message, "bad"); });
    } else if (action === "reveal-token") {
      api("/api/config/token").then(function (result) {
        var node = $("token-display");
        if (node) node.textContent = result.token || "（尚未生成）";
      }).catch(function (exc) { toast(exc.message, "bad"); });
    } else if (action === "copy-token") {
      api("/api/config/token").then(function (result) {
        var node = $("token-display");
        if (node) node.textContent = result.token || "（尚未生成）";
        return copyText(result.token || "");
      }).then(function () { toast("令牌已复制到剪贴板", "ok"); })
        .catch(function () { toast("已显示令牌，请手动选中复制", "warn"); });
    } else if (action === "fill-token") {
      api("/api/config/token").then(function (result) {
        var input = document.querySelector('[data-field="BAIDU_GATEWAY_TOKEN"]');
        if (input) {
          input.type = "text";
          input.value = result.token || "";
          state.dirty.BAIDU_GATEWAY_TOKEN = input.value;
        }
        toast("已显示当前令牌", "ok");
      }).catch(function (exc) { toast(exc.message, "bad"); });
    } else if (action === "jump-config") {
      state.showAdvanced = true;
      setView("config");
      var key = node.getAttribute("data-key");
      setTimeout(function () {
        var target = document.querySelector('[data-field-wrap="' + key + '"]');
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          var input = target.querySelector("[data-field]");
          if (input) input.focus();
        }
      }, 80);
    } else if (action === "service") {
      doService(node.getAttribute("data-service"), node.getAttribute("data-op"));
    } else if (action === "open-log") {
      state.logService = node.getAttribute("data-service");
      setView("logs");
    } else if (action === "run-check") {
      runCheck(node.getAttribute("data-role"));
    } else if (action === "baidu-start") {
      api("/api/baidu/start", { method: "POST" }).then(function () {
        toast("正在生成二维码，请稍候", "ok");
        setTimeout(loadBaidu, 1500);
      }).catch(function (exc) { toast(exc.message, "bad"); });
    } else if (action === "baidu-cancel") {
      api("/api/baidu/cancel", { method: "POST" }).then(loadBaidu);
    } else if (action === "log-reload") {
      loadLogs();
    } else if (action === "reload-activity") {
      loadActivity();
    } else if (action === "save-passwords") {
      var content = $("password-dict").value;
      api("/api/passwords", { method: "POST", body: { content: content } }).then(function (result) {
        toast(result.message, "ok");
        state.passwords = null;
        loadPasswords();
      }).catch(function (exc) { toast(exc.message, "bad"); });
    } else if (action === "maintain") {
      doMaintenance(node.getAttribute("data-op"));
    } else if (action === "switch-role") {
      var role = node.getAttribute("data-role");
      confirmDialog("切换本机角色",
        "将重新运行安装脚本把本机切换为 <strong>" + esc(role) + "</strong>。过程约 1–3 分钟，期间服务会重启，现有配置会保留。",
        "开始切换").then(function (confirmed) {
        if (!confirmed) return;
        api("/api/maintenance", { method: "POST", body: { action: "role", role: role } }).then(function (result) {
          state.jobId = result.job_id;
          state.jobSnapshot = { title: result.title, status: "running", elapsed: 0, log: "" };
          render();
          pollJob();
        }).catch(function (exc) { toast(exc.message, "bad"); });
      });
    } else if (action === "close-job") {
      state.jobId = null;
      state.jobSnapshot = null;
      render();
    } else if (action === "save-account") {
      saveAccount();
    }
    if (event) event.preventDefault();
  }

  document.addEventListener("change", function (event) {
    var node = event.target.closest("[data-action]");
    if (!node) return;
    var action = node.getAttribute("data-action");
    if (action === "log-service") { state.logService = node.value; loadLogs(); }
    else if (action === "log-lines") { state.logLines = Number(node.value); loadLogs(); }
    else if (action === "log-follow") { state.logFollow = node.checked; }
  });

  var filterTimer = null;
  document.addEventListener("input", function (event) {
    var node = event.target.closest('[data-action="log-filter"]');
    if (!node) return;
    state.logFilter = node.value;
    clearTimeout(filterTimer);
    filterTimer = setTimeout(loadLogs, 350);
  });

  function doService(service, op) {
    var labels = { start: "启动", stop: "停止", restart: "重启" };
    var run = function (force) {
      return api("/api/service", { method: "POST", body: { service: service, action: op, force: !!force } })
        .then(function (result) {
          toast(result.message || "操作成功", "ok");
          setTimeout(function () { refresh(false); }, 1200);
        });
    };
    if (op === "stop") {
      confirmDialog("确认停止服务", "停止后本机将不再处理新任务，正在进行的任务会等待完成。", "停止")
        .then(function (confirmed) { if (confirmed) run(false).catch(function (exc) { toast(exc.message, "bad"); }); });
      return;
    }
    run(false).catch(function (exc) {
      toast(labels[op] + "失败：" + exc.message, "bad");
    });
  }

  function doMaintenance(op) {
    var confirmations = {
      update: ["更新程序", "将从 GitHub 拉取最新代码并重新安装，配置与数据会保留。过程约 2–5 分钟，服务会重启。"],
      restart_panel: ["重启管理面板", "面板将在数秒后重启，期间页面无法访问，稍后刷新即可。"],
      gateway_cert: ["重新生成网关证书", "旧证书会失效。生成后必须把新的 gateway.crt 复制到所有 Worker，否则它们将无法连接网关。"],
    };
    var proceed = function () {
      api("/api/maintenance", { method: "POST", body: { action: op } }).then(function (result) {
        if (!result.job_id) { toast(result.message || "操作已执行", "ok"); return; }
        state.jobId = result.job_id;
        state.jobSnapshot = { title: result.title, status: "running", elapsed: 0, log: "" };
        render();
        pollJob();
      }).catch(function (exc) { toast(exc.message, "bad"); });
    };
    if (confirmations[op]) {
      confirmDialog(confirmations[op][0], confirmations[op][1], "继续").then(function (confirmed) {
        if (confirmed) proceed();
      });
    } else {
      proceed();
    }
  }

  function saveAccount() {
    var newPassword = $("acc-new").value;
    if (newPassword !== $("acc-new2").value) { toast("两次输入的新密码不一致", "bad"); return; }
    if (newPassword.length < 8) { toast("新密码至少需要 8 个字符", "bad"); return; }
    api("/api/account", {
      method: "POST",
      body: {
        username: $("acc-user").value,
        current_password: $("acc-current").value,
        new_password: newPassword,
      },
    }).then(function (result) {
      toast(result.message, "ok");
      setTimeout(showLogin, 900);
    }).catch(function (exc) { toast(exc.message, "bad"); });
  }

  // ---------------------------------------------------------------- init
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && state.session) refresh(false);
  });

  boot().catch(function (exc) {
    $("boot").innerHTML = "<p>面板载入失败：" + esc(exc.message) + "</p>";
  });
})();

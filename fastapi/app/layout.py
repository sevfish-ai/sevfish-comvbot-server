"""
Shared layout for admin pages — sidebar + content area

- 组织选择页（/admin/orgs）：bare 模式，无侧边栏
- 组织内页面：侧边栏显示该组织的导航（首页/设备/智能体/知识库/表格/工作流），
  标题区提供「返回组织选择」链接
"""

import html as html_mod
from typing import Optional

from fastapi.responses import HTMLResponse

# 顶部角落 icon 入口（文档 / 测试等辅助工具）
TOPBAR_ICONS = [
    ("/test", "功能测试", "🧪"),
    ("/docs/zh", "中文文档", "📖"),
    ("/docs", "API 文档", "🌐"),
]

# 组织内导航（active key, 标签, 图标, 基础 URL）
# home 的 URL 需要拼上 org_id（/admin/org/{org_id}），其余拼 ?org_id=
ORG_NAV = [
    ("home", "首页", "🏠", "/admin/org"),
    ("members", "成员", "👥", "/admin/org"),
    ("devices", "设备", "📱", "/admin"),
    ("strategies", "回复策略", "🔄", "/admin/strategies"),
    ("agents", "智能体", "🤖", "/admin/agents"),
    ("knowledge", "知识库", "📚", "/admin/knowledge"),
    ("memory", "长期记忆", "🧠", "/admin/memory"),
    ("tables", "表格", "🗄️", "/admin/tables"),
    ("workflows", "工作流", "🔄", "/admin/workflows"),
    ("tasks", "任务", "📋", "/admin/tasks"),
    ("skills", "技能", "🧩", "/admin/skills"),
    ("audit", "安全日志", "🛡️", "/admin/audit"),
]

CSS = """\
/* ═══════════════════════════════════════════
   设计变量（Design Tokens）
   ═══════════════════════════════════════════ */
:root{
  --bg:#f3f4f6;
  --surface:#ffffff;
  --border:#e4e7ec;
  --border-strong:#d5dae1;
  --text:#17202c;
  --text-2:#5b6572;
  --text-3:#8b95a3;
  --primary:#4f46e5;
  --primary-h:#4338ca;
  --primary-bg:#eef2ff;
  --green:#16a34a;  --green-bg:#dcfce7;
  --red:#dc2626;    --red-bg:#fee2e2;
  --amber:#b45309;  --amber-bg:#fef3c7;
  --blue:#0369a1;   --blue-bg:#e0f2fe;
  --sidebar-bg:#0f172a;
  --sidebar-text:#94a3b8;
  --sidebar-active:#f1f5f9;
  --radius:12px;
  --radius-sm:8px;
  --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.07);
  --shadow-lg:0 4px 16px rgba(16,24,40,.10);
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei','Helvetica Neue',Arial,sans-serif;
  --mono:'Cascadia Code','JetBrains Mono','SFMono-Regular',Consolas,'Courier New',monospace;
}

/* ── 基础 ── */
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{
  font-family:var(--font);
  background:var(--bg);
  color:var(--text);
  font-size:14px;
  line-height:1.6;
  -webkit-font-smoothing:antialiased;
  display:flex;
  min-height:100vh;
}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:none;color:var(--primary-h)}

/* ── 侧边栏 ── */
.sidebar{
  width:236px;
  background:var(--sidebar-bg);
  color:#cbd5e1;
  display:flex;
  flex-direction:column;
  flex-shrink:0;
  position:sticky;
  top:0;
  height:100vh;
  border-right:1px solid #1e293b;
}
.sidebar-brand{
  padding:20px 18px 16px;
  border-bottom:1px solid rgba(148,163,184,.14);
  display:flex;
  flex-direction:column;
  gap:2px;
}
.sidebar-brand .brand-title{
  font-size:15px;
  font-weight:700;
  color:#f8fafc;
  letter-spacing:.2px;
  display:flex;
  align-items:center;
  gap:8px;
}
.sidebar-brand .brand-title .brand-ico{
  width:22px;height:22px;border-radius:7px;flex-shrink:0;display:block;
  box-shadow:0 4px 12px -4px rgba(99,102,241,.8);
}
.bare-logo-mark img{width:28px;height:28px;border-radius:9px;display:block}
.sidebar-brand .brand-title .brand-dot{
  width:8px;height:8px;border-radius:50%;
  background:linear-gradient(135deg,#818cf8,#4f46e5);
  box-shadow:0 0 0 3px rgba(99,102,241,.25);
  flex-shrink:0;
}
.sidebar-brand .brand-back{
  display:block;
  font-size:12.5px;
  color:#7c8aa0;
  text-decoration:none;
  transition:color .15s;
  line-height:1.6;
  margin-top:6px;
}
.sidebar-brand .brand-back:hover{color:#e2e8f0}
.sidebar-brand .brand-back b{font-weight:600;color:#cbd5e1}
.sidebar-nav{flex:1;padding:14px 10px;overflow-y:auto}
.sidebar-nav .nav-label{
  font-size:11px;
  color:#64748b;
  letter-spacing:1px;
  text-transform:uppercase;
  padding:0 12px 8px;
  font-weight:600;
}
.sidebar-nav a{
  display:flex;
  align-items:center;
  gap:10px;
  padding:9px 12px;
  margin:2px 0;
  color:#aab6c8;
  text-decoration:none;
  font-size:13.5px;
  border-radius:8px;
  transition:background .15s,color .15s;
  border-left:3px solid transparent;
}
.sidebar-nav a:hover{background:rgba(148,163,184,.12);color:#f1f5f9}
.sidebar-nav a.active{
  background:rgba(99,102,241,.16);
  color:#e0e7ff;
  font-weight:600;
  border-left-color:#818cf8;
}
.sidebar-nav .nav-icon{font-size:16px;width:20px;text-align:center;flex-shrink:0;opacity:.92}
.sidebar-footer{
  padding:14px 18px;
  border-top:1px solid rgba(148,163,184,.14);
  font-size:11px;
  color:#5b6b82;
  text-align:center;
}

/* ── 主区域 ── */
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{
  background:var(--surface);
  border-bottom:1px solid var(--border);
  padding:16px 32px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  flex-shrink:0;
  gap:16px;
}
.topbar h2{font-size:19px;font-weight:700;color:var(--text);letter-spacing:.2px;display:flex;align-items:center;gap:10px}
.topbar .sub{font-size:12.5px;color:var(--text-3);margin-top:2px}
.topbar .topbar-icons{display:flex;gap:4px;align-items:center}
.icon-btn{
  display:inline-flex;align-items:center;justify-content:center;
  width:34px;height:34px;border-radius:9px;
  font-size:16px;text-decoration:none;
  color:var(--text-2);
  transition:background .15s,transform .1s;
}
.icon-btn:hover{background:#f1f2f6;color:var(--text);transform:translateY(-1px)}
.content{padding:28px 32px 40px;flex:1;overflow-y:auto}

/* ── 卡片 ── */
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:20px 24px;
  box-shadow:var(--shadow);
  margin-bottom:20px;
}
.card h3{
  font-size:15px;font-weight:600;color:var(--text);
  margin-bottom:14px;
  display:flex;align-items:center;gap:8px;
}
.card h3:not(:first-child){
  margin-top:22px;
  font-size:13.5px;
  font-weight:600;
  color:var(--text);
  padding-top:14px;
  border-top:1px dashed var(--border);
}
.card h3:not(:first-child) .badge{font-size:11px}
.card-sub{font-size:13px;color:var(--text-2);margin-bottom:14px;line-height:1.7}
.card-hd{
  display:flex;justify-content:space-between;align-items:center;gap:12px;
  margin-bottom:14px;
}
.card-hd h3{margin-bottom:0}

/* ── 表单 ── */
.form-row{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap}
.form-row > .form-group{flex:1;min-width:170px}
.form-group{display:flex;flex-direction:column;gap:6px;min-width:0}
.form-group label{font-size:12.5px;color:var(--text-2);font-weight:500;line-height:1.5}
input[type="text"],input[type="number"],input[type="password"],input[type="search"],input[type="email"],select,textarea{
  font-family:inherit;
  font-size:13.5px;
  color:var(--text);
  background:#fff;
  border:1px solid var(--border-strong);
  border-radius:8px;
  padding:7px 12px;
  outline:none;
  transition:border-color .15s,box-shadow .15s;
  width:100%;
}
input[type="text"]:focus,input[type="number"]:focus,input[type="password"]:focus,input[type="search"]:focus,input[type="email"]:focus,select:focus,textarea:focus{
  border-color:var(--primary);
  box-shadow:0 0 0 3px rgba(79,70,229,.12);
}
input::placeholder,textarea::placeholder{color:#a2abb8}
select{
  cursor:pointer;
  appearance:none;
  -webkit-appearance:none;
  background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b95a3' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;
  background-position:right 10px center;
  padding-right:30px;
}
select::-ms-expand{display:none}
textarea{resize:vertical;line-height:1.6}
.form-hint{font-size:12px;color:var(--text-3);margin-top:2px;line-height:1.6}
.form-tip{font-size:12px;color:var(--text-3)}

/* 表单两列栅格 */
.form-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px 16px;width:100%}
.form-grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 16px;width:100%}
.form-grid .full,.form-grid-3 .full{grid-column:1/-1}
.form-check{
  display:inline-flex;align-items:center;gap:6px;
  font-size:13px;color:var(--text-2);cursor:pointer;
}
.form-check input[type="checkbox"]{width:15px;height:15px;accent-color:var(--primary)}
.form-stack{display:flex;flex-direction:column;gap:14px;align-items:stretch}
.form-switch-row{display:flex;flex-direction:row;align-items:center;gap:10px;justify-content:flex-start}
.chip-check{
  display:inline-flex;align-items:center;gap:5px;
  background:#f6f7f9;
  border:1px solid var(--border);
  border-radius:8px;
  padding:5px 11px;
  font-size:13px;
  color:var(--text-2);
  cursor:pointer;
  transition:background .15s,border-color .15s;
  user-select:none;
}
.chip-check:hover{border-color:#c7cbe3;background:#f1f2f7}
.chip-check input[type="checkbox"]{width:14px;height:14px;accent-color:var(--primary);margin:0}
.dropzone{
  background:#f8f9fb;
  border:1.5px dashed var(--border-strong);
  border-radius:var(--radius);
  padding:18px;
  margin-bottom:16px;
}

/* ── 表格 ── */
.table-wrap{overflow-x:auto;margin:0 -4px;padding:0 4px}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eef0f4;font-size:13px;vertical-align:top}
th{
  background:#f8f9fb;
  font-weight:600;
  color:var(--text-2);
  font-size:12px;
  letter-spacing:.4px;
  white-space:nowrap;
  border-bottom:1px solid var(--border);
}
tbody tr{transition:background .12s}
tbody tr:hover{background:#f8f9fc}
td .muted,td .cell-sub{color:var(--text-3);font-size:12px}
.cell-main{font-weight:600;color:var(--text)}
.cell-sub{display:block;color:var(--text-3);font-size:12px;margin-top:1px}

/* ── 按钮 ── */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:5px;
  padding:7px 15px;
  border:none;
  border-radius:8px;
  font-size:13px;font-weight:500;
  font-family:inherit;
  cursor:pointer;
  text-decoration:none;
  white-space:nowrap;
  transition:background .15s,box-shadow .15s,transform .08s,color .15s;
  line-height:1.5;
}
.btn:hover{text-decoration:none}
.btn:active{transform:translateY(0)}
.btn:focus-visible{outline:2px solid rgba(79,70,229,.5);outline-offset:1px}
.btn-primary{background:var(--primary);color:#fff}
.btn-primary:hover{background:var(--primary-h);color:#fff;box-shadow:0 2px 8px rgba(79,70,229,.3)}
.btn-success{background:var(--green);color:#fff}
.btn-success:hover{background:#15803d;color:#fff;box-shadow:0 2px 8px rgba(22,163,74,.3)}
.btn-danger{background:var(--red);color:#fff}
.btn-danger:hover{background:#b91c1c;color:#fff;box-shadow:0 2px 8px rgba(220,38,38,.28)}
.btn-warning{background:var(--amber);color:#fff}
.btn-warning:hover{background:#92400e;color:#fff}
.btn-ghost{background:#f2f4f7;color:#3f4a58}
.btn-ghost:hover{background:#e6e9ee;color:var(--text)}
.btn-outline{background:#fff;color:var(--text-2);border:1px solid var(--border-strong)}
.btn-outline:hover{background:#f8f9fb;color:var(--text);border-color:#b9c0ca}
.btn-sm{padding:4px 10px;font-size:12px;border-radius:7px}
.btn-lg{padding:9px 20px;font-size:14px}
td .btn{margin:2px 4px 2px 0}
.sel-sm{width:auto;min-width:0;padding:4px 26px 4px 9px;font-size:12px;border-radius:7px}
.inp-sm{width:auto;padding:4px 9px;font-size:12px;border-radius:7px}
.field-label{font-size:12.5px;color:var(--text-2);font-weight:500;margin-bottom:6px;display:block}
.check{width:16px;height:16px;accent-color:var(--primary);vertical-align:-3px}
.row-check{width:16px;height:16px;accent-color:var(--primary);vertical-align:middle;cursor:pointer}

/* ── 徽章 / 标签 ── */
.badge{
  display:inline-flex;align-items:center;gap:4px;
  padding:2px 10px;
  border-radius:999px;
  font-size:11.5px;font-weight:600;
  line-height:1.7;
  white-space:nowrap;
}
.badge-online{background:var(--green-bg);color:#15803d}
.badge-offline{background:#f1f2f6;color:var(--text-2)}
.badge-banned{background:var(--red-bg);color:#b91c1c}
.badge-blue{background:var(--blue-bg);color:var(--blue)}
.badge-warn{background:var(--amber-bg);color:#92400e}
.badge-purple{background:#ede9fe;color:#6d28d9}

/* ── 消息条 ── */
.msg{
  padding:12px 16px;
  border-radius:10px;
  font-size:13px;
  margin-bottom:18px;
  line-height:1.7;
  display:flex;
  align-items:flex-start;
  gap:8px;
  border:1px solid transparent;
}
.msg-success{background:var(--green-bg);color:#14532d;border-color:#bbf7d0}
.msg-error{background:var(--red-bg);color:#7f1d1d;border-color:#fecaca}
.msg-info{background:var(--blue-bg);color:#0c4a6e;border-color:#bae6fd}
.msg-warn{background:var(--amber-bg);color:#78350f;border-color:#fde68a}

/* ── 说明卡（提示框） ── */
.note{
  border-radius:10px;
  padding:13px 16px;
  font-size:13px;
  line-height:1.9;
  margin-bottom:16px;
  border:1px solid transparent;
  color:var(--text-2);
}
.note b{color:var(--text)}
.note code{background:rgba(255,255,255,.7)}
.note-info{background:var(--primary-bg);border-color:#c7d2fe;color:#3730a3}
.note-info b{color:#312e81}
.note-blue{background:var(--blue-bg);border-color:#bae6fd;color:#0c4a6e}
.note-blue b{color:#075985}
.note-warn{background:var(--amber-bg);border-color:#fde68a;color:#78350f}
.note-warn b{color:#78350f}
.note-green{background:var(--green-bg);border-color:#bbf7d0;color:#14532d}
.note-green b{color:#14532d}
.note-dark{
  background:#f8fafc;
  border:1px solid var(--border);
  color:#334155;
}
.note .note-title{display:flex;align-items:center;gap:6px;font-weight:600;margin-bottom:4px}

/* ── 空状态 ── */
.empty{
  text-align:center;
  color:var(--text-3);
  padding:44px 16px;
  font-size:13.5px;
}
.empty-card{
  background:var(--surface);
  border:1px dashed var(--border-strong);
  border-radius:var(--radius);
  text-align:center;
  color:var(--text-3);
  padding:44px 16px;
  font-size:13.5px;
}

/* ── 代码 ── */
code{
  font-family:var(--mono);
  font-size:12px;
  background:#eef1f5;
  padding:1.5px 6px;
  border-radius:5px;
  color:#334155;
}
pre{
  background:#f8fafc;
  border:1px solid var(--border);
  padding:12px 14px;
  border-radius:10px;
  font-size:12.5px;
  line-height:1.7;
  overflow:auto;
  white-space:pre-wrap;
  word-break:break-all;
  max-height:380px;
  font-family:var(--mono);
}
pre code{background:none;padding:0}

/* ── 统计卡片 ── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.stat-card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:18px 20px;
  box-shadow:var(--shadow);
  transition:transform .15s,box-shadow .15s;
}
.stat-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.stat-num{font-size:28px;font-weight:700;line-height:1.2;letter-spacing:-.5px}
.stat-label{font-size:12.5px;color:var(--text-3);margin-top:4px;font-weight:500}
.stat-icon{float:right;font-size:22px;opacity:.85}

/* ── 组织 / 模块卡片 ── */
.org-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.org-card{
  display:flex;flex-direction:column;gap:14px;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:20px;
  box-shadow:var(--shadow);
  text-decoration:none;color:inherit;
  transition:transform .15s,box-shadow .15s,border-color .15s;
}
a.org-card:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg);border-color:#c7cbe3;color:inherit}
.org-card-hd{display:flex;align-items:center;gap:12px}
.org-card-icon{font-size:26px;flex-shrink:0}
.org-card-name{font-size:15.5px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.org-card-desc{font-size:12px;color:var(--text-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.org-card-arrow{font-size:16px;color:#c3cad4;flex-shrink:0;transition:transform .15s,color .15s}
a.org-card:hover .org-card-arrow{transform:translateX(3px);color:var(--primary)}
.stat-chip{
  display:inline-flex;align-items:center;gap:4px;
  background:#f6f7f9;
  border:1px solid var(--border);
  border-radius:999px;
  padding:3px 11px;
  font-size:12px;
  color:var(--text-2);
}
.stat-chip b{color:var(--text)}

.module-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.module-card{
  display:flex;flex-direction:column;gap:8px;
  text-decoration:none;color:inherit;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:18px 20px;
  transition:transform .15s,box-shadow .15s,border-color .15s;
}
a.module-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg);border-color:#c7cbe3;color:inherit}
.module-card-hd{display:flex;align-items:center;justify-content:space-between;gap:10px}
.module-card-title{font-size:14.5px;font-weight:600;color:var(--text)}
.module-card-count{color:var(--text-3);font-size:12px}
.module-card-desc{font-size:12.5px;color:var(--text-2);line-height:1.7;flex:1}
.module-card .btn{align-self:flex-start;margin-top:6px}

/* ── bare 组织选择页 ── */
.bare-wrap{
  min-height:100vh;
  width:100%;
  padding:48px 24px;
  background:
    radial-gradient(1000px 500px at 15% -10%, rgba(99,102,241,.10), transparent 60%),
    radial-gradient(900px 460px at 90% 0%, rgba(14,165,233,.08), transparent 55%),
    linear-gradient(180deg,#f8f9ff 0%,#f3f4f6 46%);
}
body:has(.bare-wrap){display:block}
.bare-header{
  display:flex;align-items:center;justify-content:space-between;
  max-width:960px;margin:0 auto 30px;
}
.bare-logo{display:flex;align-items:center;gap:14px}
.bare-logo-mark{
  width:46px;height:46px;border-radius:13px;
  background:linear-gradient(135deg,#6366f1,#4338ca);
  display:flex;align-items:center;justify-content:center;
  font-size:24px;
  box-shadow:0 6px 16px rgba(79,70,229,.35);
}
.bare-logo h1{font-size:21px;color:var(--text);font-weight:700}
.bare-logo p{font-size:13px;color:var(--text-3)}
.bare-main{max-width:960px;margin:0 auto}

/* ── 其他工具类 ── */
.mono{font-family:var(--mono)}
.muted{color:var(--text-3)}
.inline-form{display:inline}
.inline-flex{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.mt-0{margin-top:0}
.mb-0{margin-bottom:0}
.w-100{width:100%}
.hr{border:none;border-top:1px solid var(--border);margin:14px 0}

/* ── 文档页 ── */
.ep-block{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:10px;
  padding:14px 16px;
  margin-bottom:10px;
  transition:box-shadow .15s,border-color .15s;
}
.ep-block:hover{box-shadow:var(--shadow);border-color:var(--border-strong)}
.ep-method{
  display:inline-block;
  color:#fff;
  padding:2px 10px;
  border-radius:6px;
  font-size:12px;
  font-weight:600;
  font-family:var(--mono);
  line-height:1.6;
}
.ep-path{font-family:var(--mono);font-size:13px;font-weight:600;margin-left:8px;color:var(--text)}
.ep-desc{font-size:13px;color:var(--text);margin:8px 0 4px}
.ep-detail{font-size:12.5px;color:var(--text-2);line-height:1.8}
.ep-label{font-size:11px;color:var(--text-3);font-weight:500}
.doc-lead{font-size:13px;color:var(--text);margin-bottom:10px;line-height:1.8}

/* ── 功能测试页左右分栏 ── */
.test-split{
  display:grid;
  grid-template-columns:minmax(0,1fr) 400px;
  gap:20px;
  align-items:start;
}
.test-left{min-width:0}
.test-right{
  position:sticky;
  top:24px;
  max-height:calc(100vh - 120px);
  overflow-y:auto;
  min-width:0;
  padding-right:2px;
}
.test-right .card{margin-bottom:0}
.test-right::-webkit-scrollbar{width:6px}
.test-right::-webkit-scrollbar-thumb{background:#d5dae1;border-radius:3px}
@media(max-width:1200px){
  .test-split{grid-template-columns:1fr}
  .test-right{position:static;max-height:none;overflow:visible}
  .test-right .card{margin-bottom:20px}
}

/* ── 工作流节点参数区 ── */
.param-label{font-size:11.5px;color:var(--text-2);font-weight:600;display:block;margin-bottom:4px}
.param-note{
  flex:1;min-width:100%;
  margin-top:4px;margin-bottom:0;
  padding:10px 13px;
  font-size:12px;
  line-height:1.85;
  border-radius:9px;
  border:1px solid transparent;
  color:var(--text-2);
}
.param-note b{color:var(--text)}
.param-note code{background:rgba(255,255,255,.75)}
.param-note pre{margin:6px 0 0;background:#0f172a;color:#e2e8f0;border:none;border-radius:8px;padding:9px 11px;font-size:11.5px;max-height:none}
.param-note table{margin-top:6px}
.param-note tr{border-top:1px solid rgba(100,116,139,.25)}
.param-note td{padding:4px 6px;border-bottom:none}
.param-hint-bar{
  flex:1;min-width:100%;
  margin-top:2px;margin-bottom:0;
  background:var(--amber-bg);
  border:1px solid #fde68a;
  border-radius:8px;
  padding:8px 12px;
  font-size:12px;
  color:#78350f;
  line-height:1.75;
}
.param-hint-bar code{background:rgba(255,255,255,.75)}

/* ── 响应式 ── */
@media(max-width:1100px){
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .form-grid,.form-grid-3{grid-template-columns:1fr}
}
@media(max-width:768px){
  .sidebar{width:58px}
  .sidebar-brand{padding:16px 10px;align-items:center}
  .sidebar-brand .brand-title{font-size:0}
  .sidebar-brand .brand-title .brand-dot{width:9px;height:9px}
  .sidebar-brand .brand-back{display:none}
  .sidebar-nav a span{display:none}
  .sidebar-nav a{padding:11px;justify-content:center}
  .sidebar-nav a .nav-icon{margin:0}
  .sidebar-footer{display:none}
  .topbar{padding:12px 16px}
  .topbar h2{font-size:16px}
  .content{padding:16px}
  .stat-grid{grid-template-columns:repeat(2,1fr);gap:10px}
}
/* 表格横向滚动容器：窄屏下宽表格（含内嵌表单）不撑破布局 */
.tbl-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tbl-scroll table{min-width:max-content}
"""


def esc(s) -> str:
    return html_mod.escape(str(s))


def _status_text(s: str) -> str:
    return {"online": "在线", "offline": "离线", "banned": "已封禁"}.get(s, s)


def _status_badge(s: str) -> str:
    cls = {
        "online": "badge-online",
        "offline": "badge-offline",
        "banned": "badge-banned",
    }.get(s, "badge-offline")
    return f'<span class="badge {cls}">{_status_text(s)}</span>'


def _fmt_dt(dt_str) -> str:
    if not dt_str:
        return "-"
    try:
        return dt_str.replace("T", " ")[:19]
    except Exception:
        return dt_str


def page(
    title: str,
    heading: str,
    content_html: str,
    msg: str = "",
    subtitle: str = "",
    active: str = "",
    org_id: int = 0,
    org_name: str = "",
    org_active: str = "",
    bare: bool = False,
) -> HTMLResponse:
    """Render a full page

    bare=True：组织选择页，无侧边栏，内容居中；
    org_id>0：组织内页面，侧边栏显示组织导航（首页/设备/智能体/知识库/表格/工作流/任务），
              brand 区提供「返回组织选择」链接。
    """
    icons_html = "".join(
        f'<a href="{url}" title="{label}" class="icon-btn">{icon}</a>'
        for url, label, icon in TOPBAR_ICONS
    )

    # Message banner（按前缀选样式）
    msg_html = ""
    if msg:
        if msg.startswith("✅"):
            msg_html = f'<div class="msg msg-success">{esc(msg)}</div>'
        elif msg.startswith("❌") or msg.startswith("err:") or msg.startswith("⚠"):
            msg_html = f'<div class="msg msg-error">{esc(msg)}</div>'
        elif msg.startswith("ℹ"):
            msg_html = f'<div class="msg msg-info">{esc(msg)}</div>'
        else:
            msg_html = f'<div class="msg msg-info">{esc(msg)}</div>'

    sub_html = f'<span class="sub">{esc(subtitle)}</span>' if subtitle else ""

    # ── bare 模式：组织选择页，无侧边栏 ──
    if bare:
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>{esc(title)} · SevFish</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{CSS}</style>
</head>
<body>
<div class="bare-wrap">
    <div class="bare-header">
        <div class="bare-logo">
            <div class="bare-logo-mark"><img src="/favicon.svg" width="28" height="28" alt="SevFish"></div>
            <div>
                <h1>SevFish</h1>
                <p>组织控制台 · 选择组织进入</p>
            </div>
        </div>
        <div class="topbar-icons">{icons_html}</div>
    </div>
    {msg_html}
    <div class="bare-main">{content_html}</div>
</div>
</body>
</html>"""
        response = HTMLResponse(content=html)
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # ── 组织内导航 ──
    if org_id:
        brand_title = (
            '<div class="brand-title"><img class="brand-ico" src="/favicon.svg" width="22" height="22" alt="">SevFish</div>'
        )
        brand_back = (
            f'<a href="/admin/orgs" class="brand-back" title="返回组织选择">'
            f"⬅ {esc(org_name)}</a>"
        )
        nav_parts = []
        for key, label, icon, base in ORG_NAV:
            # home/members 是路径式（/admin/org/{id}...），其余走 ?org_id=
            if key == "members":
                url = f"/admin/org/{org_id}/members"
            elif key == "home":
                url = f"{base}/{org_id}"
            else:
                url = f"{base}?org_id={org_id}"
            cls = " active" if key == org_active else ""
            nav_parts.append(
                f'<a class="{cls}" href="{url}"><span class="nav-icon">{icon}</span><span>{label}</span></a>'
            )
    else:
        # 无组织上下文（如测试/文档页）：只有「返回组织选择」
        brand_title = (
            '<div class="brand-title"><img class="brand-ico" src="/favicon.svg" width="22" height="22" alt="">SevFish</div>'
        )
        brand_back = (
            '<a href="/admin/orgs" class="brand-back" title="返回组织选择">⬅ 组织选择</a>'
        )
        nav_parts = [
            f'<a class="{("active" if active == "/admin/orgs" else "")}" href="/admin/orgs">'
            f'<span class="nav-icon">🏢</span><span>组织选择</span></a>'
        ]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>{esc(title)} · SevFish</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{CSS}</style>
</head>
<body>
<div class="sidebar">
    <div class="sidebar-brand">
        {brand_title}
        {brand_back}
    </div>
    <div class="sidebar-nav">
        <div class="nav-label">导航</div>
        {"".join(nav_parts)}
    </div>
    <div class="sidebar-footer">v2.8.1 · SevFish 控制台</div>
</div>
<div class="main">
    <div class="topbar">
        <div>
            <h2>{esc(heading)}</h2>
            {sub_html}
        </div>
        <div class="topbar-icons">{icons_html}</div>
    </div>
    <div class="content">
        {msg_html}
        {content_html}
    </div>
</div>
</body>
</html>"""

    # 返回带有禁止缓存 HTTP 头的响应
    response = HTMLResponse(content=html)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

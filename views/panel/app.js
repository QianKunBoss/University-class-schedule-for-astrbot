// 教务课表面板前端。无第三方依赖，通过宿主注入的 window.AstrBotPluginView 与后端通信。
const bridge = window.AstrBotPluginView;

const state = {
  status: null,
  config: null,
  week: null,
  currentWeek: null,
  maxWeek: 20,
  grid: null,
  terms: [],
  busy: false,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/** 转义，防止课程名里的尖括号破坏结构 */
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, isError ? 5200 : 2600);
}

function setBusy(busy) {
  state.busy = busy;
  ["#btnRefresh", "#btnLogin"].forEach((sel) => {
    const el = $(sel);
    if (el) el.disabled = busy;
  });
}

// ---------------- 接口封装 ----------------
async function get(path, params) {
  try {
    return await bridge.apiGet(path, params);
  } catch (err) {
    // 带上接口名，方便定位是哪条路由出的问题
    throw new Error(`${path}: ${(err && err.message) || "请求失败"}`);
  }
}

async function post(path, body) {
  try {
    return await bridge.apiPost(path, body || {});
  } catch (err) {
    throw new Error(`${path}: ${(err && err.message) || "请求失败"}`);
  }
}

// ---------------- 顶部与状态 ----------------
function renderTopbar() {
  const s = state.status;
  if (!s) return;
  const bits = [];
  if (s.account) bits.push(s.account);
  if (s.week) bits.push(`第 ${s.week} 周`);
  if (s.course_count) bits.push(`${s.course_count} 个课程安排`);
  if (!s.has_password) bits.push("未设置密码");
  $("#brandSub").textContent = bits.join(" · ") || "尚未配置";
  $("#notifyToggle").checked = !!s.notify_enabled;
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  const restoreBtn = $("#btnRestoreHidden");
  if (restoreBtn) restoreBtn.hidden = !(s.hidden_count > 0);
  const editInfo = $("#editInfo");
  if (editInfo)
    editInfo.textContent =
      (s.manual_count > 0 ? `有 ${s.manual_count} 门自定义课程。` : "可以在课表页添加自定义课程。") +
      (s.hidden_count > 0 ? ` 教务课程已隐藏 ${s.hidden_count} 条。` : "");
  const rows = [
    ["认证方式", s.auth_mode || "-"],
    ["账号", s.account || "未配置"],
    ["教务地址", s.base_url || "未配置"],
    ["统一认证", s.cas_url || "-"],
    ["SSO 回调", s.service_url || "-"],
    ["学期", s.term_name || s.term || "-"],
    ["当前周", s.week ? `第 ${s.week} 周` : "未开学 / 未知"],
    ["自定义课程", `${s.manual_count ?? 0} 条（已隐藏教务课程 ${s.hidden_count ?? 0} 条）`],
    ["第 1 周周一", s.week1 ? `${s.week1}（${s.week1_source}）` : "-"],
    ["课表缓存", s.fetched_at || "无"],
    ["课程数", String(s.course_count || 0)],
    ["提醒", `${s.notify_enabled ? "开" : "关"}，提前 ${s.ahead} 分钟`],
    ["早报", s.brief || "关闭"],
    ["推送目标", `${s.targets} 个`],
    ["验证码识别", s.ocr ? "可用" : "不可用"],
    ["AI 补全识别", s.llm_enhance ? "已开启" : "关闭"],
    ["配置文件", s.config_path || "-"],
  ];
  $("#statusList").innerHTML = rows
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join("");

  const st = s.stats || {};
  const chips = [
    { text: `共 ${st.total || 0} 条`, warn: false },
    { text: `缺教师 ${st.no_teacher || 0}`, warn: (st.no_teacher || 0) > 0 },
    { text: `缺教室 ${st.no_location || 0}`, warn: (st.no_location || 0) > 0 },
    { text: `周次靠猜 ${st.weeks_guessed || 0}`, warn: (st.weeks_guessed || 0) > 0 },
  ];
  let html = chips.map((c) => `<span class="chip${c.warn ? " warn" : ""}">${esc(c.text)}</span>`).join("");
  if (st.samples && st.samples.length) {
    html += `<ul class="recog-list">${st.samples
      .map((x) => `<li>${esc(x.name)} — 原始：${esc(x.raw || "-")}</li>`)
      .join("")}</ul>`;
  }
  if (s.last_error) {
    html += `<p class="hint" style="width:100%;color:var(--error)">最近错误：${esc(s.last_error)}</p>`;
  }
  $("#recogBox").innerHTML = html;
}

async function loadStatus() {
  const s = await get("status");
  state.status = s;
  renderTopbar();
  renderStatus();
  return s;
}

// ---------------- 课表 ----------------
// ---------------- 自定义课程（面板编辑） ----------------
let editingEntryId = "";

function weeksToText(weeks) {
  const list = (weeks || []).slice().sort((a, b) => a - b);
  if (!list.length) return "";
  const segs = [];
  let s = list[0], prev = list[0];
  for (let i = 1; i <= list.length; i++) {
    const v = list[i];
    if (v !== prev + 1) {
      segs.push(s === prev ? String(s) : `${s}-${prev}`);
      s = v;
    }
    prev = v;
  }
  return segs.join(",");
}

async function openCourseDialog(entry) {
  editingEntryId = entry ? entry.entry_id : "";
  $("#courseDialogTitle").textContent = entry ? "编辑自定义课程" : "添加自定义课程";
  const f = $("#courseForm");
  f.name.value = entry ? entry.name : "";
  f.weekday.value = entry
    ? String(entry.weekday)
    : String((state.grid && state.grid.days || []).find((d) => d.today)?.weekday || 1);
  f.start_period.value = entry ? entry.start_period : 1;
  f.end_period.value = entry ? entry.end_period : 2;
  f.weeks_text.value = entry ? weeksToText(entry.weeks) : "";
  f.teacher.value = entry ? entry.teacher : "";
  f.location.value = entry ? entry.location : "";
  $("#courseDialog").hidden = false;
  setTimeout(() => f.name.focus(), 50);
}

function closeCourseDialog() {
  $("#courseDialog").hidden = true;
}

async function saveCourse(ev) {
  ev.preventDefault();
  const f = $("#courseForm");
  const payload = {
    action: "save",
    entry_id: editingEntryId || "",
    name: f.name.value.trim(),
    weekday: Number(f.weekday.value),
    start_period: Number(f.start_period.value),
    end_period: Number(f.end_period.value),
    weeks_text: f.weeks_text.value.trim(),
    teacher: f.teacher.value.trim(),
    location: f.location.value.trim(),
  };
  if (!payload.name) return toast("课程名不能为空", true);
  if (payload.end_period < payload.start_period)
    return toast("结束节次不能小于开始节次", true);
  try {
    const r = await post("manual", payload);
    if (r && r.ok === false) return toast(r.message || "保存失败", true);
    closeCourseDialog();
    toast(editingEntryId ? "课程已更新" : "课程已添加");
    await loadTimetable(state.week);
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteCourse(entryId) {
  if (!confirm("确定删除这门自定义课程？")) return;
  try {
    await post("manual", { action: "delete", entry_id: entryId });
    toast("课程已删除");
    await loadTimetable(state.week);
  } catch (err) {
    toast(err.message, true);
  }
}

async function hideCourse(fp, restore) {
  try {
    await post("hide", { fingerprint: fp, hidden: !restore });
    toast(restore ? "已恢复" : "已隐藏");
    await loadTimetable(state.week);
    await loadStatus();
  } catch (err) {
    toast(err.message, true);
  }
}

async function restoreAllHidden() {
  if (!confirm("恢复全部被隐藏的教务课程？")) return;
  await hideCourse("__all__", true);
}

function cardHtml(c) {
  const parts = [];
  if (c.location) parts.push(`<span>📍 ${esc(c.location)}</span>`);
  if (c.teacher) parts.push(`<span>👤 ${esc(c.teacher)}</span>`);
  if (!parts.length) parts.push("<span>未识别</span>");
  let cls = c.incomplete ? "course incomplete" : "course";
  if (c.manual) cls += " manual";
  // 面板编辑按钮：自定义课 → 编辑/删除；教务课 → 隐藏
  let actions = "";
  if (c.manual) {
    actions = `<div class="course-actions"><button class="mini-btn" data-act="edit" data-entry="${esc(c.entry_id)}" title="编辑">✎</button><button class="mini-btn" data-act="del" data-entry="${esc(c.entry_id)}" title="删除">🗑</button></div>`;
  } else {
    actions = `<div class="course-actions"><button class="mini-btn" data-act="hide" data-fp="${esc(c.fingerprint)}" title="从课表中隐藏这一条">🚫</button></div>`;
  }
  return `<div class="${cls}" title="${esc(c.time)}"><div class="name">${esc(c.name)}${c.manual ? ' <span class="tag">自定义</span>' : ""}</div><div class="meta">${parts.join("")}</div>${actions}</div>`;
}

function renderGrid() {
  const g = state.grid;
  const table = $("#grid");
  const empty = $("#gridEmpty");

  if (!g || !g.rows || !g.rows.length) {
    table.innerHTML = "";
    table.hidden = true;
    empty.hidden = false;
    $("#weekLabel").textContent = g ? `第 ${g.week} 周` : "暂无数据";
    $("#weekBounds").textContent = g ? g.bounds || "" : "";
    $("#courseCount").textContent = "";
    $("#todayBox").hidden = true;
    return;
  }

  table.hidden = false;
  empty.hidden = true;

  const head =
    "<thead><tr><th>节次</th>" +
    g.days
      .map(
        (d) =>
          `<th class="${d.today ? "today" : ""}">${esc(d.label)}<span class="d">${esc(d.short)}</span></th>`,
      )
      .join("") +
    "</tr></thead>";

  const body =
    "<tbody>" +
    g.rows
      .map((row) => {
        const cells = row.cells
          .map((cell, i) => {
            const today = g.days[i] && g.days[i].today ? " today" : "";
            const inner = cell.length ? cell.map(cardHtml).join("") : "";
            return `<td class="${today.trim()}">${inner}</td>`;
          })
          .join("");
        const time = row.time ? `<span class="t">${esc(row.time)}</span>` : "";
        return `<tr><th><span class="n">${esc(row.label)}节</span>${time}</th>${cells}</tr>`;
      })
      .join("") +
    "</tbody>";

  table.innerHTML = head + body;

  $("#weekLabel").textContent = `第 ${g.week} 周`;
  $("#weekBounds").textContent = g.bounds || "";
  $("#courseCount").textContent = `本周 ${g.total} 节`;
  $("#prevWeek").disabled = g.week <= 1;
  $("#nextWeek").disabled = g.week >= (g.max_week || 20);
  $("#thisWeek").disabled = !g.current_week || g.week === g.current_week;

  // 今日剩余课程
  const box = $("#todayBox");
  if (g.today && g.today.length && g.week === g.current_week) {
    box.hidden = false;
    box.innerHTML =
      "<h3>今天接下来的课</h3><div class='today-list'>" +
      g.today
        .map(
          (c) =>
            `<div class="today-item"><b>${esc(c.name)}</b><i>${esc(c.time)} ${esc(c.location || "")}</i></div>`,
        )
        .join("") +
      "</div>";
  } else {
    box.hidden = true;
  }
}

async function loadTimetable(week) {
  try {
    const params = week ? { week } : {};
    const g = await get("timetable", params);
    if (g && g.ok === false) {
      state.grid = null;
      renderGrid();
      $("#weekLabel").textContent = "暂无数据";
      $("#weekBounds").textContent = "";
      return;
    }
    state.grid = g;
    state.week = g.week;
    state.currentWeek = g.current_week;
    state.maxWeek = g.max_week || 20;
    renderGrid();
  } catch (err) {
    state.grid = null;
    renderGrid();
  }
}

// ---------------- 设置 ----------------
async function loadConfig() {
  const c = await get("config");
  state.config = c;
  const f = $("#settingsForm");
  const jw = c.jw || {};
  const nt = c.notify || {};
  const sc = c.schedule || {};

  f.base_url.value = jw.base_url || "";
  f.auth_mode.value = jw.auth_mode || "auto";
  f.cas_url.value = jw.cas_url || "";
  f.service_url.value = jw.service_url || "";
  f.username.value = jw.username || "";
  f.password.value = "";
  $("#pwdHint").textContent = jw.has_password ? "已保存密码（留空则不修改）" : "尚未保存密码";

  f.ahead_minutes.value = nt.ahead_minutes ?? 20;
  f.second_ahead_minutes.value = nt.second_ahead_minutes ?? 0;
  f.daily_brief_time.value = nt.daily_brief_time || "";
  f.notify_enabled.checked = !!nt.enabled;
  f.brief_skip_empty.checked = nt.brief_skip_empty !== false;

  renderTermOptions(state.terms, sc.term || "auto");
  f.week1_monday.value = sc.week1_monday || "";
  f.refresh_hours.value = sc.refresh_hours ?? 6;
  f.llm_enhance.checked = !!sc.llm_enhance;
}

// ---------------- 学期选择 ----------------
function renderTermOptions(terms, current) {
  const sel = $("#termSelect");
  if (!sel) return;
  const list = [{ v: "auto", t: "auto（跟随教务默认）" }];
  (terms || []).forEach((t) => {
    if (t && t.id) list.push({ v: t.id, t: `${t.id} ${t.name || ""}`.trim() });
  });
  const cur = (current || "auto").trim();
  if (cur && cur !== "auto" && !list.some((o) => o.v === cur)) {
    list.push({ v: cur, t: `${cur}（当前配置，教务列表中未出现）` });
  }
  list.push({ v: "__manual__", t: "手动输入…" });

  sel.innerHTML = list.map((o) => `<option value="${esc(o.v)}">${esc(o.t)}</option>`).join("");
  sel.value = list.some((o) => o.v === cur) ? cur : "auto";
  toggleManualInput();
}

function toggleManualInput() {
  const sel = $("#termSelect");
  const input = document.querySelector('input[name="term"]');
  if (!sel || !input) return;
  const manual = sel.value === "__manual__";
  input.style.display = manual ? "" : "none";
  if (manual) {
    const saved = (state.config && state.config.schedule && state.config.schedule.term) || "";
    input.value = saved && saved !== "auto" ? saved : "";
    input.focus();
  }
}

async function loadTerms() {
  const hint = $("#termHint");
  if (hint) hint.textContent = "正在登录并获取学期列表…";
  try {
    const r = await get("terms");
    if (r && r.ok === false) {
      if (hint) hint.textContent = r.message || "获取失败";
      return;
    }
    state.terms = r.terms || [];
    renderTermOptions(state.terms, (state.config && state.config.schedule.term) || "auto");
    if (hint) {
      hint.textContent = `共 ${state.terms.length} 个学期` + (r.default ? `，教务默认 ${r.default}` : "");
    }
  } catch (err) {
    if (hint) hint.textContent = err.message;
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const f = $("#settingsForm");
  const payload = {
    jw: {
      base_url: f.base_url.value.trim(),
      auth_mode: f.auth_mode.value,
      cas_url: f.cas_url.value.trim(),
      service_url: f.service_url.value.trim(),
      username: f.username.value.trim(),
    },
    notify: {
      enabled: f.notify_enabled.checked,
      ahead_minutes: Number(f.ahead_minutes.value || 0),
      second_ahead_minutes: Number(f.second_ahead_minutes.value || 0),
      daily_brief_time: f.daily_brief_time.value.trim(),
      brief_skip_empty: f.brief_skip_empty.checked,
    },
    schedule: {
      term:
        f.term_select.value === "__manual__"
          ? f.term.value.trim() || "auto"
          : f.term_select.value,
      week1_monday: f.week1_monday.value.trim(),
      refresh_hours: Number(f.refresh_hours.value || 6),
      llm_enhance: f.llm_enhance.checked,
    },
  };
  const pwd = f.password.value;
  if (pwd) payload.jw.password = pwd;

  try {
    const r = await post("config", payload);
    if (r && r.ok === false) {
      toast(r.message || "保存失败", true);
      return;
    }
    const changed = (r && r.changed) || [];
    $("#saveHint").textContent = changed.length ? `已保存 ${changed.length} 项` : "没有改动";
    toast(changed.length ? "配置已保存" : "没有改动");
    f.password.value = "";
    await loadConfig();
    await loadStatus();
    if (r && r.warning) toast(r.warning, true);
    if (r && r.service_url) {
      $("#saveHint").textContent += ` · 回调地址：${r.service_url}`;
    }
    // 切了学期就立刻按新学期重抓课表，不用等下一个刷新周期
    if ((r.changed || []).includes("schedule.term")) {
      toast("学期已切换，正在重新抓取课表…");
      try {
        await post("refresh", {});
        state.week = null;
        await loadTimetable(null);
        toast("课表已按新学期刷新");
      } catch (e2) {
        toast(e2.message, true);
      }
    }
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------- 操作 ----------------
async function doRefresh() {
  if (state.busy) return;
  setBusy(true);
  toast("正在登录并抓取课表…");
  try {
    const r = await post("refresh", {});
    if (r && r.ok === false) {
      toast(r.message || "刷新失败", true);
      if (r.hint) $("#brandSub").textContent = r.hint;
      return;
    }
    const st = r.stats || {};
    let msg = `已刷新：${r.count} 个课程安排`;
    if (st.no_teacher || st.no_location) {
      msg += `（缺教师 ${st.no_teacher || 0}、缺教室 ${st.no_location || 0}，可在设置里开启 AI 补全）`;
    }
    toast(msg);
    await loadStatus();
    await loadTimetable(state.week);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
  }
}

async function doLogin() {
  if (state.busy) return;
  setBusy(true);
  toast("正在尝试登录…");
  try {
    const r = await post("login", {});
    if (r && r.ok === false) {
      toast(r.message || "登录失败", true);
      return;
    }
    toast(`登录成功（${r.mode || "未知方式"}）`);
    await loadStatus();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
  }
}

async function toggleNotify(enabled) {
  try {
    await post("toggle", { notify_enabled: enabled });
    await loadStatus();
    toast(enabled ? "提醒已开启" : "提醒已关闭");
  } catch (err) {
    toast(err.message, true);
  }
}

async function exportSnapshot() {
  try {
    const r = await bridge.download("debug/snapshot", undefined, "课表原始页面.html");
    toast(`已开始下载：${(r && r.filename) || "课表原始页面.html"}`);
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------- 事件绑定 ----------------
// 0.3.3 起面板不再有「启用/关闭」开关：面板始终可用。
function bindEvents() {
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
      const name = btn.dataset.tab;
      ["schedule", "settings", "status"].forEach((key) => {
        $(`#tab-${key}`).hidden = key !== name;
      });
      if (name === "settings") {
        loadConfig()
          .then(() => {
            // 已保存过密码才自动拉学期列表，避免没配置时反复弹登录错误
            if (state.status && state.status.has_password) return loadTerms();
          })
          .catch((e) => toast(e.message, true));
      }
      if (name === "status") loadStatus().catch((e) => toast(e.message, true));
    });
  });

  $("#prevWeek").addEventListener("click", () => {
    if (state.week > 1) loadTimetable(state.week - 1);
  });
  $("#nextWeek").addEventListener("click", () => {
    if (state.week < state.maxWeek) loadTimetable(state.week + 1);
  });
  $("#thisWeek").addEventListener("click", () => {
    if (state.currentWeek) loadTimetable(state.currentWeek);
  });
  $("#btnRefresh").addEventListener("click", doRefresh);
  $("#btnLogin").addEventListener("click", doLogin);
  $("#notifyToggle").addEventListener("change", (e) => toggleNotify(e.target.checked));
  $("#settingsForm").addEventListener("submit", saveConfig);
  const bannerBtn = $("#bannerBtn");
  if (bannerBtn) bannerBtn.remove(); // 兼容旧页面缓存里残留的提示条元素
  const fetchTermsBtn = $("#btnFetchTerms");
  if (fetchTermsBtn) fetchTermsBtn.addEventListener("click", loadTerms);
  const termSelect = $("#termSelect");
  if (termSelect) termSelect.addEventListener("change", toggleManualInput);
  const exportBtn = $("#btnExportSnapshot");
  if (exportBtn) exportBtn.addEventListener("click", exportSnapshot);
  const addCourseBtn = $("#btnAddCourse");
  if (addCourseBtn)
    addCourseBtn.addEventListener("click", () => openCourseDialog(null));
  const courseForm = $("#courseForm");
  if (courseForm) courseForm.addEventListener("submit", saveCourse);
  const courseCancel = $("#courseCancel");
  if (courseCancel) courseCancel.addEventListener("click", closeCourseDialog);
  const restoreBtn = $("#btnRestoreHidden");
  if (restoreBtn) restoreBtn.addEventListener("click", restoreAllHidden);
  // 课表卡片上的操作按钮（事件委托）
  const gridEl = $("#grid");
  if (gridEl) {
    gridEl.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".mini-btn");
      if (!btn) return;
      ev.preventDefault();
      const act = btn.dataset.act;
      if (act === "hide") return hideCourse(btn.dataset.fp, false);
      if (act === "del") return deleteCourse(btn.dataset.entry);
      if (act === "edit") {
        try {
          const r = await get("manual");
          const entry = (r.entries || []).find((x) => x.entry_id === btn.dataset.entry);
          if (entry) await openCourseDialog(entry);
        } catch (err) {
          toast(err.message, true);
        }
      }
    });
  }
}

// ---------------- 启动 ----------------
async function main() {
  if (!bridge) {
    document.body.innerHTML =
      '<div style="padding:40px;font-family:sans-serif">' +
      "<h2>请从 AstrBot WebUI 打开本页面</h2>" +
      "<p>直接访问 HTML 文件时拿不到宿主注入的 bridge，页面无法读取数据。</p></div>";
    return;
  }

  try {
    await bridge.ready();
  } catch (err) {
    // 宿主上下文还没到也继续尝试，接口调用会给出具体错误
  }

  bindEvents();

  try {
    // 无论开关状态如何都加载课表 —— 面板是查看工具，开关只影响顶部提示条，
    // 不再把整个界面挡掉（之前那套「关闭就挡住」的设计排查起来太痛苦）。
    await loadStatus();
    await loadTimetable(null);
  } catch (err) {
    toast(err.message, true);
  }
}

main();

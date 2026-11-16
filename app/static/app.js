/* ShiftKey front-end v2 – roles, corrections, admin pages. No framework, no build step. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    me: null, config: {}, reports: [], current: null, view: "reports",
    filter: { q: "", dept: "", status: "all", segment: "" }, grouped: true,
    pendingFile: null, fixRecord: null, users: [], departments: [], shifts: [], editUser: null,
  };

  const STATUS_FILTERS = [
    { key: "all", label: "All" },
    { key: "present", label: "Present", test: r => r.is_present },
    { key: "absent", label: "Absent", test: r => r.is_absent },
    { key: "halfday", label: "Half day", test: r => r.is_halfday },
    { key: "mispunch", label: "Mispunch", test: r => r.flag_mispunch },
    { key: "review", label: "Needs review", test: r => r.flag_warning },
    { key: "pending", label: "Pending", test: r => r.needs_action },
    { key: "fixed", label: "Resolved", test: r => r.is_corrected },
    { key: "night", label: "Night", test: r => r.is_night },
  ];
  const ACTION_LABELS = { shift_time: "Shift time", set_time: "Actual time", halfday: "Half day", absent: "Absent", revert: "Reverted" };
  const ROLE_LABELS = { admin: "Admin", hod: "HOD", hr: "HR" };

  /* ------------------------------------------------------------ helpers */
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const el = html => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const isAdmin = () => state.me && state.me.role === "admin";

  function toast(msg, kind = "", ms = 4200) {
    const node = el(`<div class="toast ${kind}">${esc(msg)}</div>`);
    $("#toasts").appendChild(node);
    setTimeout(() => { node.style.opacity = "0"; node.style.transition = "opacity .3s"; setTimeout(() => node.remove(), 320); }, ms);
  }
  function timeAgo(iso) {
    if (!iso) return "never";
    const d = new Date(iso); const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }
  const fmtDT = iso => iso ? new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  function longDate(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" }); }
  function shortDate(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }); }

  async function api(path, opts = {}) {
    opts.headers = Object.assign({ "X-Requested-With": "ShiftKey" }, opts.headers || {});
    if (opts.json !== undefined) { opts.body = JSON.stringify(opts.json); opts.headers["Content-Type"] = "application/json"; delete opts.json; }
    const res = await fetch(path, opts);
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    const body = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (body && body.detail) ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `HTTP ${res.status}`;
      const err = new Error(msg); err.status = res.status;
      if (res.status === 401 && !path.startsWith("/api/auth/")) showLogin();
      if (res.status === 428) openPwModal(true);
      throw err;
    }
    return body;
  }

  /* ------------------------------------------------------------ boot / auth */
  async function boot() {
    try { state.config = await api("/api/config"); } catch (e) { /* ignore */ }
    try { state.me = await api("/api/me"); } catch (e) { state.me = null; }
    if (!state.me) { showLogin(); return; }
    enterApp();
  }

  function showLogin() {
    state.me = null; state.current = null; state.reports = [];
    $("#appShell").hidden = true; $("#loginView").hidden = false;
    $("#loginError").hidden = true; $("#loginPass").value = ""; $("#loginUser").value = "";
    setTimeout(() => $("#loginUser").focus(), 50);
  }

  $("#loginForm").addEventListener("submit", async e => {
    e.preventDefault();
    const btn = $("#loginBtn"); btn.disabled = true; $("#loginError").hidden = true;
    try {
      state.me = await api("/api/auth/login", { method: "POST", json: { username: $("#loginUser").value.trim(), password: $("#loginPass").value } });
      $("#loginPass").value = "";
      enterApp();
    } catch (err) { $("#loginError").textContent = err.message; $("#loginError").hidden = false; }
    finally { btn.disabled = false; }
  });

  $("#logoutBtn").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ } showLogin(); });

  async function enterApp() {
    $("#loginView").hidden = true; $("#appShell").hidden = false;
    renderUserChrome();
    if (state.me.must_change_password) openPwModal(true);
    await loadReports();
    switchView("reports");
    const hash = Number(location.hash.replace("#", ""));
    if (hash && state.reports.some(r => r.id === hash)) openReport(hash);
    else if (state.reports.length) openReport(state.reports[0].id);
    else showDropView();
  }

  function renderUserChrome() {
    const me = state.me;
    $$(".admin-only").forEach(n => n.classList.toggle("hidden-role", !isAdmin()));
    $$(".admin-only").forEach(n => { n.hidden = !isAdmin(); });
    $("#userName").textContent = me.full_name || me.username;
    $("#userAvatar").textContent = (me.full_name || me.username).trim().slice(0, 1).toUpperCase();
    const rb = $("#userRole"); rb.textContent = ROLE_LABELS[me.role] || me.role; rb.className = `role-badge ${me.role}`;
    let scope = "";
    if (me.role === "admin") scope = "<b>Administrator</b> – all departments, all staff.";
    else if (me.role === "hod") scope = `<b>Head of department</b><br>${me.departments.map(esc).join(", ") || "no departments assigned"}`;
    else scope = `<b>HR – ${esc(me.segment_label)} staff</b><br>All departments, paycodes ${me.segment === "indian" ? "starting with 1" : me.segment === "local" ? "starting with 2" : "of both segments"}.`;
    $("#userScope").innerHTML = `${scope}<br><span style="color:var(--faint)">signed in as ${esc(me.username)}</span>`;
    // HR with a fixed segment never sees the segment chips
    $("#segChips").hidden = !!(me.scope.segment);
  }

  $("#userBtn").addEventListener("click", e => { e.stopPropagation(); const m = $("#userMenu"); m.hidden = !m.hidden; });
  $("#userMenu").addEventListener("click", e => e.stopPropagation());

  /* ------------------------------------------------------------ password modal */
  function openPwModal(forced) {
    $("#pwModal").hidden = false; $("#pwError").hidden = true;
    $("#pwCurrent").value = $("#pwNew").value = $("#pwNew2").value = "";
    $("#pwTitle").textContent = forced ? "Set a new password" : "Change password";
    $("#pwIntro").textContent = forced ? "Your password was set by the administrator. Choose your own before continuing (8+ characters, upper & lower case, a digit)." : "Use at least 8 characters with upper and lower case letters and a digit.";
    $("#pwCancel").hidden = !!forced;
    $("#userMenu").hidden = true;
    setTimeout(() => $("#pwCurrent").focus(), 50);
  }
  $("#pwBtn").addEventListener("click", () => openPwModal(false));
  $("#pwCancel").addEventListener("click", () => { $("#pwModal").hidden = true; });
  $("#pwForm").addEventListener("submit", async e => {
    e.preventDefault();
    if ($("#pwNew").value !== $("#pwNew2").value) { $("#pwError").textContent = "The new passwords do not match."; $("#pwError").hidden = false; return; }
    try {
      state.me = await api("/api/me/password", { method: "POST", json: { current_password: $("#pwCurrent").value, new_password: $("#pwNew").value } });
      $("#pwModal").hidden = true; toast("Password updated", "ok"); renderUserChrome();
      if (!state.reports.length) await loadReports();
    } catch (err) { $("#pwError").textContent = err.message; $("#pwError").hidden = false; }
  });

  /* ------------------------------------------------------------ navigation */
  $("#navTabs").addEventListener("click", e => { const t = e.target.closest(".tab"); if (t) switchView(t.dataset.view); });
  function switchView(view) {
    if ((view === "users" || view === "shifts" || view === "integrations") && !isAdmin()) view = "reports";
    state.view = view;
    $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === view));
    $("#reportsLayout").hidden = view !== "reports";
    $("#correctionsPage").hidden = view !== "corrections";
    $("#usersPage").hidden = view !== "users";
    $("#shiftsPage").hidden = view !== "shifts";
    $("#auditPage").hidden = view !== "audit";
    $("#integrationsPage").hidden = view !== "integrations";
    if (view === "integrations") loadIntegrations();
    if (view !== "audit") stopAutoRefresh();
    if (view === "audit") loadAudit(true);
    if (view === "corrections") loadCorrections();
    if (view === "users") loadUsers();
    if (view === "shifts") loadShifts();
  }

  /* ------------------------------------------------------------ reports sidebar */
  async function loadReports() {
    try { state.reports = await api("/api/reports"); } catch (e) { if (e.status !== 401 && e.status !== 428) toast(`Could not load reports: ${e.message}`, "err"); state.reports = []; }
    renderSidebar();
  }

  function renderSidebar() {
    const list = $("#reportList"); list.innerHTML = "";
    $("#reportCount").textContent = state.reports.length ? `${state.reports.length}` : "";
    $("#reportEmpty").hidden = state.reports.length > 0;
    for (const r of state.reports) {
      const li = document.createElement("li");
      li.appendChild(el(`
        <button class="report-item ${state.current && state.current.id === r.id ? "active" : ""}" data-id="${r.id}">
          <div class="d"><b>${esc(shortDate(r.report_date))}</b><span>${esc(r.report_date.slice(0, 4))}</span></div>
          <div class="mini">
            <i title="Employees">${r.total}</i><i title="Present">P ${r.present}</i><i title="Absent">A ${r.absent}</i>
            ${r.halfday ? `<i class="hlf" title="Half days">HLF ${r.halfday}</i>` : ""}
            ${r.pending ? `<i class="mis" title="Pending resolution">⚑ ${r.pending}</i>` : ""}
            ${r.corrected ? `<i class="warn" title="Resolved" style="color:var(--ok);background:var(--ok-soft)">✓ ${r.corrected}</i>` : ""}
            ${!r.pending && !r.halfday && !r.corrected ? `<i title="No flags">✓ clean</i>` : ""}
          </div>
        </button>`));
      list.appendChild(li);
    }
  }
  $("#reportList").addEventListener("click", e => { const b = e.target.closest(".report-item"); if (b) openReport(Number(b.dataset.id)); });

  function showDropView() {
    state.current = null;
    $("#dropView").hidden = false; $("#reportView").hidden = true;
    $("#nonAdminEmpty").hidden = isAdmin();
    history.replaceState(null, "", location.pathname);
    renderSidebar();
  }

  async function openReport(id) {
    try {
      const detail = await api(`/api/reports/${id}`);
      state.current = detail;
      state.filter = { q: "", dept: "", status: "all", segment: "" };
      $("#search").value = "";
      location.hash = String(id);
      renderReport();
    } catch (e) { if (e.status !== 401) toast(e.message, "err"); }
  }

  function renderReport() {
    const r = state.current; if (!r) return;
    $("#dropView").hidden = true; $("#reportView").hidden = false;
    $("#rEyebrow").textContent = `${r.title || "Daily Performance Report"}${r.company ? " · " + r.company : ""}${state.me.scope.segment ? " · " + state.me.scope.segment_label + " staff" : ""}`;
    $("#rTitle").textContent = longDate(r.report_date);
    const meta = [];
    if (r.source_kind === "sql") meta.push(`Source <span class="tag seg">SQL</span> ${esc(r.source_filename || "")}`);
    else if (r.source_filename) meta.push(isAdmin() ? `Source <a href="/api/reports/${r.id}/source" title="Download original">${esc(r.source_filename)}</a>` : `Source ${esc(r.source_filename)}`);
    if (r.run_at) meta.push(`Machine run ${esc(r.run_at)}`);
    meta.push(`Uploaded ${esc(timeAgo(r.uploaded_at))}`);
    if (state.me.role === "hod") meta.push(`Your departments: ${state.me.departments.map(esc).join(", ")}`);
    $("#rMeta").innerHTML = meta.map(m => `<span>${m}</span>`).join("");
    const p = $("#rProblems"); p.hidden = !(r.problems && r.problems.length); p.textContent = (r.problems || []).join(" ");
    renderStats(); renderAttention(); renderDeptFilter(); renderChips(); renderTable(); renderSidebar();
    $("#deleteBtn").textContent = "Delete"; $("#deleteBtn").classList.remove("confirm");
  }

  function renderStats() {
    const s = state.current.summary; const t = s.total || 1;
    const pct = n => `${Math.round(n / t * 100)}%`;
    const tiles = [
      ["all", "", "Employees", s.total, ""],
      ["present", "ok", "Present", s.present, pct(s.present)],
      ["absent", "abs", "Absent", s.absent, pct(s.absent)],
      ["halfday", "hlf", "Half days", s.halfday, pct(s.halfday)],
      ["mispunch", "mis", "Mispunches", s.mispunch, pct(s.mispunch)],
      ["review", "warn", "Needs review", s.warnings, pct(s.warnings)],
      ["pending", "pend", "Pending", s.pending, ""],
      ["fixed", "fixed", "Resolved", s.corrected, ""],
    ];
    $("#stats").innerHTML = tiles.map(([key, cls, label, v, pc]) => `
      <button class="stat ${cls} ${state.filter.status === key ? "active" : ""}" data-filter="${key}" title="Filter table">
        <span class="pct">${pc}</span><span class="v">${v}</span><span class="l">${esc(label)}</span></button>`).join("");
  }
  $("#stats").addEventListener("click", e => { const b = e.target.closest(".stat"); if (!b) return; setStatusFilter(state.filter.status === b.dataset.filter && b.dataset.filter !== "all" ? "all" : b.dataset.filter); });

  const allRecords = () => state.current.departments.flatMap(d => d.records);

  function renderAttention() {
    const recs = allRecords();
    const open = recs.filter(r => r.needs_action);
    const fixed = recs.filter(r => r.is_corrected);
    const hlf = recs.filter(r => r.is_halfday && !r.is_corrected);
    const item = (r, state_) => `
      <li data-seq="${r.seq}" class="grid3">
        <span class="n">${esc(r.name)}</span>
        <span class="t">${r.time_in ? esc(r.time_in) : "<em>—</em>"} → ${r.time_out ? esc(r.time_out) : "<em>—</em>"}${r.hrs ? ` · ${esc(r.hrs)} h` : ""}</span>
        ${state_ === "open" ? (r.can_correct ? `<button class="btn sm fix" data-fix="${r.id}">Resolve</button>` : `<span class="state open">OPEN</span>`) : state_ === "done" ? `<span class="state done">${esc(ACTION_LABELS[r.correction.action] || "FIXED")}</span>` : `<span></span>`}
        <span class="m">${esc(r.dept_name || "")} · ${esc(r.paycode)}${state_ === "done" ? ` · by ${esc(r.correction.by || "?")}${r.correction.remark ? " – " + esc(r.correction.remark) : ""}` : (r.notes && r.notes.length ? " · " + esc(r.notes[0]) : "")}</span>
      </li>`;
    const card = (cls, title, rows, fkey, state_, empty) => `
      <section class="att-card ${cls}">
        <header><h3>${esc(title)} <span class="pill">${rows.length}</span></h3>${rows.length ? `<button data-filter="${fkey}">Show in table</button>` : ""}</header>
        ${rows.length ? `<ul class="att-list">${rows.map(r => item(r, state_)).join("")}</ul>` : `<div class="att-empty">${empty}</div>`}
      </section>`;
    $("#attention").innerHTML =
      card("pend", "Pending resolution", open, "pending", "open", "No open mispunches 🎉") +
      card("hlf", "Half days", hlf, "halfday", "", "None today") +
      card("warn", "Resolved", fixed, "fixed", "done", "Nothing resolved yet");
  }
  $("#attention").addEventListener("click", e => {
    const fix = e.target.closest("button[data-fix]"); if (fix) { openFix(Number(fix.dataset.fix)); return; }
    const b = e.target.closest("button[data-filter]"); if (b) { setStatusFilter(b.dataset.filter); $("#table").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
    const li = e.target.closest("li[data-seq]"); if (li) jumpToRow(Number(li.dataset.seq));
  });

  function jumpToRow(seq) {
    let row = $(`#tbody tr[data-seq="${seq}"]`);
    if (!row) { state.filter = { q: "", dept: "", status: "all", segment: "" }; $("#search").value = ""; $("#deptFilter").value = ""; renderStats(); renderChips(); renderTable(); row = $(`#tbody tr[data-seq="${seq}"]`); }
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
  }

  function renderDeptFilter() {
    const sel = $("#deptFilter");
    sel.innerHTML = `<option value="">All departments</option>` + state.current.summary.departments.map(d => `<option value="${esc(d.code)}|${esc(d.name)}">${esc(d.name)} (${d.total})</option>`).join("");
    sel.value = state.filter.dept;
  }

  function renderChips() {
    const recs = allRecords();
    $("#statusChips").innerHTML = STATUS_FILTERS.map(f => {
      const n = f.test ? recs.filter(f.test).length : recs.length;
      if (n === 0 && !["all", "pending", "mispunch", "halfday"].includes(f.key)) return "";
      return `<button class="chip ${state.filter.status === f.key ? "active" : ""}" data-filter="${f.key}" role="tab">${esc(f.label)}<span class="k">${n}</span></button>`;
    }).join("");
    const segs = state.current.summary.by_segment || {};
    const segList = [["", "All staff", recs.length], ["indian", "Indian", segs.indian || 0], ["local", "Local", segs.local || 0]];
    if (segs.other) segList.push(["other", "Other", segs.other]);
    $("#segChips").innerHTML = segList.map(([k, l, n]) => `<button class="chip ${state.filter.segment === k ? "active" : ""}" data-seg="${k}">${l}<span class="k">${n}</span></button>`).join("");
  }
  $("#statusChips").addEventListener("click", e => { const b = e.target.closest(".chip"); if (b) setStatusFilter(b.dataset.filter); });
  $("#segChips").addEventListener("click", e => { const b = e.target.closest(".chip"); if (b) { state.filter.segment = b.dataset.seg; renderChips(); renderTable(); } });
  function setStatusFilter(key) { state.filter.status = key; renderStats(); renderChips(); renderTable(); }

  function filtered() {
    const f = STATUS_FILTERS.find(x => x.key === state.filter.status) || STATUS_FILTERS[0];
    const q = state.filter.q.trim().toLowerCase(); const seg = state.filter.segment;
    return state.current.departments
      .filter(d => !state.filter.dept || `${d.code}|${d.name}` === state.filter.dept)
      .map(d => ({ ...d, records: d.records.filter(r => (!f.test || f.test(r)) && (!seg || r.segment === seg) && (!q || r.name.toLowerCase().includes(q) || String(r.paycode).toLowerCase().includes(q))) }))
      .filter(d => d.records.length);
  }

  function statusBadge(r) {
    const code = (r.status || "").trim();
    const cls = ["P", "A", "HLF", "MIS"].includes(code) ? code : "other";
    return `<span class="status ${cls}" title="${esc(r.status_label)}">${esc(code || "?")}</span>`;
  }

  function rowHtml(r) {
    const cls = ["emp", r.is_corrected ? "is-fixed" : "", r.is_mispunch ? "is-mis" : "", r.is_halfday && !r.is_corrected ? "is-hlf" : "", r.has_warning && !r.is_mispunch ? "is-warn" : "", r.is_absent ? "is-abs" : ""].join(" ");
    const tcell = (v, orig, needed) => `<td class="time ${(!v && needed) ? "missing" : ""}">${v ? esc(v) : (needed ? "—" : "")}${r.is_corrected && orig !== v ? `<span class="was">${orig ? esc(orig) : "—"}</span>` : ""}</td>`;
    const tags = [
      r.is_corrected ? `<span class="tag fixed">${esc((ACTION_LABELS[r.correction.action] || "FIXED").toUpperCase())}</span>` : "",
      r.is_night ? `<span class="tag night">NIGHT</span>` : "",
      r.has_warning ? `<span class="tag warn">REVIEW</span>` : "",
      r.manual ? `<span class="tag manual">MANUAL</span>` : "",
      r.segment === "local" ? `<span class="tag seg">LOCAL</span>` : "",
    ].join("");
    const notes = r.is_corrected ? [`Resolved by ${r.correction.by || "?"}${r.correction.remark ? ": " + r.correction.remark : ""}`, ...(r.notes || [])] : (r.notes || []);
    let act = "";
    if (r.can_correct) {
      if (r.is_corrected) act = `<button class="btn sm undo" data-undo="${r.id}" title="Revert to the machine values">Undo</button>`;
      else if (r.needs_action) act = `<button class="btn sm fix" data-fix="${r.id}">Resolve</button>`;
      else if (isAdmin()) act = `<button class="btn sm" data-fix="${r.id}" title="Edit punches">Edit</button>`;
    }
    return `<tr class="${cls}" data-seq="${r.seq}" data-id="${r.id}">
      <td class="num">${r.seq}</td>
      <td class="mono">${esc(r.paycode)}</td>
      <td class="name">${esc(r.name)}${!state.grouped ? `<small>${esc(r.dept_name || "")}</small>` : ""}</td>
      <td class="mono">${esc(r.shift || "")}${r.shift_start ? `<span style="color:var(--faint)"> ${esc(r.shift_start)}</span>` : ""}</td>
      ${tcell(r.time_in, r.original.time_in, !r.is_absent)}${tcell(r.time_out, r.original.time_out, !r.is_absent)}
      <td class="hrs">${esc(r.hrs || "")}</td>
      <td class="c">${statusBadge(r)}</td>
      <td class="remarks">${tags}${esc(notes.join(" · "))}</td>
      <td class="act-col">${act}</td>
      <td class="act-col"><button class="btn icon" data-history="${r.id}" title="History of this record"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></button></td>
    </tr>`;
  }

  function renderTable() {
    const groups = filtered(); const tb = $("#tbody"); let html = ""; let n = 0;
    for (const d of groups) {
      n += d.records.length;
      if (state.grouped) {
        const pend = d.records.filter(r => r.needs_action).length, hlf = d.records.filter(r => r.is_halfday).length;
        html += `<tr class="dept"><td colspan="11">${esc(d.code || "")} ${esc(d.name || "")}<span>${d.records.length} shown${hlf ? `<i class="hlf"> · HLF ${hlf}</i>` : ""}${pend ? `<i class="mis"> · pending ${pend}</i>` : ""}</span></td></tr>`;
      }
      html += d.records.map(rowHtml).join("");
    }
    tb.innerHTML = html;
    $("#tableEmpty").hidden = n > 0;
    const total = state.current.summary.total;
    $("#tableFoot").textContent = n === total ? `${total} employees across ${state.current.summary.departments.length} departments` : `${n} of ${total} employees shown`;
  }
  $("#tbody").addEventListener("click", e => {
    const fix = e.target.closest("button[data-fix]"); if (fix) { openFix(Number(fix.dataset.fix)); return; }
    const undo = e.target.closest("button[data-undo]"); if (undo) { revert(Number(undo.dataset.undo), undo); return; }
    const h = e.target.closest("button[data-history]"); if (h) openHistory(Number(h.dataset.history));
  });
  $("#historyBtn").addEventListener("click", () => { if (state.current) { state.auditFilter = { report_id: state.current.id, label: `Report ${longDate(state.current.report_date)}` }; switchView("audit"); } });

  $("#search").addEventListener("input", e => { state.filter.q = e.target.value; renderTable(); });
  $("#deptFilter").addEventListener("change", e => { state.filter.dept = e.target.value; renderTable(); });
  $("#groupToggle").addEventListener("change", e => { state.grouped = e.target.checked; renderTable(); });
  document.addEventListener("keydown", e => {
    if (e.key === "/" && !/input|select|textarea/i.test(document.activeElement.tagName) && state.current && state.view === "reports") { e.preventDefault(); $("#search").focus(); }
    if (e.key === "Escape") { closeMenus(); if (document.activeElement === $("#search")) { $("#search").value = ""; state.filter.q = ""; renderTable(); } }
  });

  /* ------------------------------------------------------------ corrections */
  function findRecord(id) { return allRecords().find(r => r.id === id); }
  function replaceRecord(updated) {
    for (const d of state.current.departments) { const i = d.records.findIndex(r => r.id === updated.id); if (i >= 0) { d.records[i] = updated; break; } }
    recomputeSummary();
  }
  function recomputeSummary() {
    const recs = allRecords(); const s = state.current.summary;
    s.present = recs.filter(r => r.is_present).length; s.absent = recs.filter(r => r.is_absent).length;
    s.halfday = recs.filter(r => r.is_halfday).length; s.pending = recs.filter(r => r.needs_action).length;
    s.corrected = recs.filter(r => r.is_corrected).length;
    const rep = state.reports.find(x => x.id === state.current.id);
    if (rep) Object.assign(rep, { present: s.present, absent: s.absent, halfday: s.halfday, pending: s.pending, corrected: s.corrected });
  }

  function openFix(id) {
    const r = findRecord(id); if (!r) return;
    state.fixRecord = r;
    const missing = r.time_in && !r.time_out ? "out" : (!r.time_in && r.time_out ? "in" : "out");
    $("#fixEmp").innerHTML = `<b>${esc(r.name)}</b><span class="t">${r.time_in ? esc(r.time_in) : "<em>—</em>"} → ${r.time_out ? esc(r.time_out) : "<em>—</em>"}${r.hrs ? " · " + esc(r.hrs) + " h" : ""}</span>
      <span class="m">${esc(r.dept_name || "")} · ${esc(r.paycode)} · shift ${esc(r.shift || "?")}${r.shift_start ? " " + esc(r.shift_start) : ""}${r.shift_end ? "–" + esc(r.shift_end) : ""} · machine status ${esc(r.original.status)}${r.notes && r.notes.length ? "<br>" + esc(r.notes[0]) : ""}</span>`;
    const shiftTime = missing === "out" ? r.shift_end : r.shift_start;
    const shiftRadio = $('#fixOptions input[value="shift_time"]');
    $("#fixShiftLabel").textContent = missing === "out" ? `Use shift end time${shiftTime ? " (" + shiftTime + ")" : ""}` : `Use shift start time${shiftTime ? " (" + shiftTime + ")" : ""}`;
    $("#fixShiftHint").textContent = shiftTime ? `Fills the ${missing.toUpperCase()} punch with the scheduled time for shift ${r.shift}` : (r.shift ? `Shift ${r.shift} has no ${missing === "out" ? "end" : "start"} time in the shift master – ask the admin, or enter the time.` : "No shift code on this record – enter the time manually.");
    shiftRadio.disabled = !shiftTime;
    shiftRadio.checked = !!shiftTime;
    if (!shiftTime) $('#fixOptions input[value="set_time"]').checked = true;
    $("#fixField").value = missing; $("#fixTime").value = ""; $("#fixRemark").value = ""; $("#fixError").hidden = true;
    $("#fixModal").hidden = false;
    setTimeout(() => (shiftTime ? $("#fixSubmit") : $("#fixTime")).focus(), 50);
  }
  $("#fixCancel").addEventListener("click", () => { $("#fixModal").hidden = true; state.fixRecord = null; });
  $("#fixTime").addEventListener("focus", () => { $('#fixOptions input[value="set_time"]').checked = true; });
  $("#fixField").addEventListener("focus", () => { $('#fixOptions input[value="set_time"]').checked = true; });
  $("#fixForm").addEventListener("submit", async e => {
    e.preventDefault();
    const r = state.fixRecord; if (!r) return;
    const action = $('#fixOptions input[name="fixAction"]:checked').value;
    const body = { action, remark: $("#fixRemark").value };
    if (action === "set_time") { body.field = $("#fixField").value; body.time = $("#fixTime").value; if (!body.time) { $("#fixError").textContent = "Enter the time (HH:MM)."; $("#fixError").hidden = false; return; } }
    if (action === "shift_time") body.field = $("#fixField").value;
    $("#fixSubmit").disabled = true;
    try {
      const updated = await api(`/api/records/${r.id}/correction`, { method: "POST", json: body });
      replaceRecord(updated); $("#fixModal").hidden = true; state.fixRecord = null;
      renderStats(); renderAttention(); renderChips(); renderTable(); renderSidebar();
      toast(`${updated.name}: ${updated.time_in || "—"} → ${updated.time_out || "—"} · ${updated.status_label}`, "ok");
    } catch (err) { $("#fixError").textContent = err.message; $("#fixError").hidden = false; }
    finally { $("#fixSubmit").disabled = false; }
  });

  async function revert(id, btn) {
    if (btn && !btn.classList.contains("confirm")) { btn.classList.add("confirm"); btn.textContent = "Undo?"; setTimeout(() => { btn.classList.remove("confirm"); btn.textContent = "Undo"; }, 3000); return; }
    try {
      const updated = await api(`/api/records/${id}/correction`, { method: "DELETE" });
      replaceRecord(updated); renderStats(); renderAttention(); renderChips(); renderTable(); renderSidebar();
      toast(`Reverted ${updated.name} to the machine values`);
    } catch (err) { toast(err.message, "err"); }
  }

  async function loadCorrections() {
    try {
      const rows = await api("/api/corrections");
      $("#corrEmpty").hidden = rows.length > 0;
      const t = c => c ? esc(c) : "—";
      $("#corrBody").innerHTML = rows.map(c => `
        <tr>
          <td class="mono">${esc(fmtDT(c.at))}</td>
          <td>${esc(shortDate(c.report_date))} ${esc(c.report_date.slice(0, 4))}</td>
          <td class="name">${esc(c.name)}<small>${esc(c.paycode)} · ${c.segment === "local" ? "Local" : c.segment === "indian" ? "Indian" : ""}</small></td>
          <td>${esc(c.dept_name || c.dept_code || "")}</td>
          <td><span class="tag ${c.action === "revert" ? "manual" : "fixed"}">${esc((ACTION_LABELS[c.action] || c.action).toUpperCase())}</span></td>
          <td class="time before">${t(c.original.time_in)}<span class="arrow">→</span>${t(c.original.time_out)} · ${esc(c.original.status || "")}</td>
          <td class="time">${t(c.corrected.time_in)}<span class="arrow">→</span>${t(c.corrected.time_out)} · ${esc(c.corrected.status || "")}${c.corrected.hrs ? ` · ${esc(c.corrected.hrs)} h` : ""}</td>
          <td>${esc(c.by || "")}</td>
          <td class="remarks">${esc(c.remark || "")}</td>
        </tr>`).join("");
    } catch (e) { if (e.status !== 401) toast(e.message, "err"); }
  }
  $("#corrRefresh").addEventListener("click", loadCorrections);

  /* ------------------------------------------------------------ users (admin) */
  async function loadUsers() {
    try { [state.users, state.departments] = await Promise.all([api("/api/users"), api("/api/departments")]); } catch (e) { toast(e.message, "err"); return; }
    const deptName = code => { const d = state.departments.find(x => x.code === code); return d ? d.name : code; };
    $("#usersBody").innerHTML = state.users.map(u => `
      <tr class="${u.is_active ? "" : "inactive"}" data-id="${u.id}">
        <td class="mono">${esc(u.username)}${u.id === state.me.id ? ' <span class="tag seg">YOU</span>' : ""}</td>
        <td class="name">${esc(u.full_name || "")}</td>
        <td><span class="role-badge ${u.role}">${esc(ROLE_LABELS[u.role] || u.role)}</span></td>
        <td>${u.role === "hod" ? `<div class="pill-list">${u.departments.map(c => `<i title="${esc(c)}">${esc(deptName(c))}</i>`).join("") || "<i>none</i>"}</div>` : u.role === "hr" ? `${esc(u.segment_label)} staff` : "Everything"}</td>
        <td>${u.is_active ? (u.must_change_password ? '<span class="tag warn">PASSWORD RESET PENDING</span>' : '<span class="tag fixed">ACTIVE</span>') : '<span class="tag manual">DISABLED</span>'}</td>
        <td class="mono">${esc(timeAgo(u.last_login_at))}</td>
        <td class="act-col"><button class="btn sm" data-edit="${u.id}">Edit</button> ${u.id !== state.me.id ? `<button class="btn sm ghost-danger" data-del="${u.id}">Delete</button>` : ""}</td>
      </tr>`).join("");
  }
  $("#usersBody").addEventListener("click", async e => {
    const ed = e.target.closest("button[data-edit]"); if (ed) { openUserModal(state.users.find(u => u.id === Number(ed.dataset.edit))); return; }
    const del = e.target.closest("button[data-del]");
    if (del) {
      if (!del.classList.contains("confirm")) { del.classList.add("confirm"); del.textContent = "Confirm?"; setTimeout(() => { del.classList.remove("confirm"); del.textContent = "Delete"; }, 3000); return; }
      try { await api(`/api/users/${del.dataset.del}`, { method: "DELETE" }); toast("User deleted"); loadUsers(); } catch (err) { toast(err.message, "err"); }
    }
  });
  $("#newUserBtn").addEventListener("click", () => openUserModal(null));

  function openUserModal(u) {
    state.editUser = u;
    $("#userModalTitle").textContent = u ? `Edit ${u.username}` : "New user";
    $("#uUsername").value = u ? u.username : ""; $("#uUsername").disabled = !!u;
    $("#uName").value = u ? u.full_name : "";
    $("#uRole").value = u ? u.role : "hod";
    $("#uSegment").value = u && u.role === "hr" ? u.segment : "indian";
    $("#uPassword").value = ""; $("#uPassword").required = !u;
    $("#uPasswordLabel").textContent = u ? "Reset password (leave blank to keep)" : "Initial password";
    $("#uMustChange").checked = true;
    $("#uActive").checked = u ? u.is_active : true; $("#uActiveField").hidden = !u || u.id === state.me.id;
    $("#uDepts").innerHTML = state.departments.length ? state.departments.map(d => `<label><input type="checkbox" value="${esc(d.code)}" ${u && u.departments.includes(d.code) ? "checked" : ""}><span>${esc(d.name)} <small>${esc(d.code)} · ${d.headcount}</small></span></label>`).join("") : `<span class="empty-note">Import a report first – departments are learnt from the Starlink export.</span>`;
    $("#userError").hidden = true;
    syncUserForm(); $("#userModal").hidden = false;
    setTimeout(() => (u ? $("#uName") : $("#uUsername")).focus(), 50);
  }
  function syncUserForm() { const role = $("#uRole").value; $("#uSegmentField").hidden = role !== "hr"; $("#uDeptsField").hidden = role !== "hod"; }
  $("#uRole").addEventListener("change", syncUserForm);
  $("#userCancel").addEventListener("click", () => { $("#userModal").hidden = true; });
  $("#userForm").addEventListener("submit", async e => {
    e.preventDefault();
    const u = state.editUser; const role = $("#uRole").value;
    const depts = $$("#uDepts input:checked").map(i => i.value);
    const body = { full_name: $("#uName").value.trim(), role, segment: $("#uSegment").value, departments: depts, must_change_password: $("#uMustChange").checked };
    try {
      if (u) {
        if ($("#uPassword").value) body.password = $("#uPassword").value;
        if (u.id !== state.me.id) body.is_active = $("#uActive").checked;
        await api(`/api/users/${u.id}`, { method: "PATCH", json: body });
        toast(`Saved ${u.username}`, "ok");
      } else {
        body.username = $("#uUsername").value.trim(); body.password = $("#uPassword").value;
        await api("/api/users", { method: "POST", json: body });
        toast(`Created ${body.username}`, "ok");
      }
      $("#userModal").hidden = true; loadUsers();
    } catch (err) { $("#userError").textContent = err.message; $("#userError").hidden = false; }
  });

  /* ------------------------------------------------------------ shifts (admin) */
  async function loadShifts() {
    try { state.shifts = await api("/api/shifts"); } catch (e) { toast(e.message, "err"); return; }
    $("#shiftsBody").innerHTML = state.shifts.map(s => shiftRow(s)).join("");
  }
  const shiftRow = s => `
    <tr data-code="${esc(s.code)}">
      <td class="mono"><b>${esc(s.code)}</b></td>
      <td><input class="shift-input name" data-f="name" value="${esc(s.name || "")}" placeholder="Name"></td>
      <td class="c"><input class="shift-input" data-f="start" value="${esc(s.start || "")}" placeholder="HH:MM" pattern="([01]\\d|2[0-3]):[0-5]\\d"></td>
      <td class="c"><input class="shift-input" data-f="end" value="${esc(s.end || "")}" placeholder="HH:MM" pattern="([01]\\d|2[0-3]):[0-5]\\d"></td>
      <td>${s.is_night ? '<span class="tag night">NIGHT</span>' : (s.start && s.end ? '<span class="tag seg">DAY</span>' : '<span class="tag warn">END MISSING</span>')}</td>
      <td class="mono" style="color:var(--muted)">${esc(s.source)}</td>
      <td class="act-col"><button class="btn sm primary" data-save="${esc(s.code)}">Save</button> <button class="btn sm ghost-danger" data-del="${esc(s.code)}">Delete</button></td>
    </tr>`;
  $("#shiftsBody").addEventListener("click", async e => {
    const save = e.target.closest("button[data-save]");
    if (save) {
      const tr = save.closest("tr"); const body = {};
      tr.querySelectorAll("input").forEach(i => { body[i.dataset.f] = i.value.trim(); });
      if (body.start && !/^([01]\d|2[0-3]):[0-5]\d$/.test(body.start)) return toast("Start must be HH:MM", "err");
      if (body.end && !/^([01]\d|2[0-3]):[0-5]\d$/.test(body.end)) return toast("End must be HH:MM", "err");
      if (!body.start) delete body.start; if (!body.end) delete body.end;
      try { const s = await api(`/api/shifts/${encodeURIComponent(save.dataset.save)}`, { method: "PUT", json: body }); tr.outerHTML = shiftRow(s); toast(`Saved shift ${s.code}`, "ok"); } catch (err) { toast(err.message, "err"); }
      return;
    }
    const del = e.target.closest("button[data-del]");
    if (del) {
      if (!del.classList.contains("confirm")) { del.classList.add("confirm"); del.textContent = "Confirm?"; setTimeout(() => { del.classList.remove("confirm"); del.textContent = "Delete"; }, 3000); return; }
      try { await api(`/api/shifts/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" }); loadShifts(); } catch (err) { toast(err.message, "err"); }
    }
  });
  $("#newShiftBtn").addEventListener("click", () => {
    const code = (window.prompt("Shift code (as printed by Starlink, e.g. M09):") || "").trim();
    if (!code) return;
    api(`/api/shifts/${encodeURIComponent(code)}`, { method: "PUT", json: {} }).then(loadShifts).catch(err => toast(err.message, "err"));
  });

  /* ------------------------------------------------------------ export */
  function exportUrl(format, only) {
    const r = state.current; const p = new URLSearchParams({ format });
    p.set("highlight", $("#optHighlight").checked ? "true" : "false");
    p.set("summary", $("#optSummary").checked ? "true" : "false");
    if (only) p.set("only", only);
    if ($("#optCurrent").checked) { if (state.filter.segment) p.set("segment", state.filter.segment); if (state.filter.dept) p.set("dept", state.filter.dept.split("|")[0]); }
    return `/api/reports/${r.id}/export?${p}`;
  }
  function download(url) { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove(); }
  $("#exportXlsx").addEventListener("click", () => { download(exportUrl("xlsx")); toast("Preparing Excel export…", "", 1800); });
  $("#exportMenuBtn").addEventListener("click", e => { e.stopPropagation(); const m = $("#exportMenu"); m.hidden = !m.hidden; $("#exportMenuBtn").setAttribute("aria-expanded", String(!m.hidden)); });
  $("#exportMenu").addEventListener("click", e => {
    e.stopPropagation(); const b = e.target.closest(".menu-item"); if (!b) return;
    if (b.dataset.export === "source") download(`/api/reports/${state.current.id}/source`); else download(exportUrl(b.dataset.export, b.dataset.only));
    closeMenus();
  });
  function closeMenus() { $("#exportMenu").hidden = true; $("#exportMenuBtn").setAttribute("aria-expanded", "false"); $("#userMenu").hidden = true; }
  document.addEventListener("click", closeMenus);
  $("#printBtn").addEventListener("click", () => window.print());

  /* ------------------------------------------------------------ delete report (admin) */
  let deleteTimer = null;
  $("#deleteBtn").addEventListener("click", async () => {
    const b = $("#deleteBtn");
    if (!b.classList.contains("confirm")) { b.classList.add("confirm"); b.textContent = "Confirm delete?"; clearTimeout(deleteTimer); deleteTimer = setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Delete"; }, 3500); return; }
    clearTimeout(deleteTimer);
    const id = state.current.id, label = state.current.report_date_label;
    try { await api(`/api/reports/${id}`, { method: "DELETE" }); toast(`Deleted report for ${label}`); await loadReports(); if (state.reports.length) openReport(state.reports[0].id); else showDropView(); }
    catch (e) { toast(e.message, "err"); b.classList.remove("confirm"); b.textContent = "Delete"; }
  });

  /* ------------------------------------------------------------ upload (admin) */
  const fileInput = $("#fileInput");
  $("#newUploadBtn").addEventListener("click", () => fileInput.click());
  $("#browseBtn").addEventListener("click", e => { e.stopPropagation(); fileInput.click(); });
  $("#dropzone").addEventListener("click", () => fileInput.click());
  $("#dropzone").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ""; });

  let dragDepth = 0;
  const hasFiles = e => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  const endDrag = () => { $("#dragOverlay").hidden = true; $("#dropzone").classList.remove("over"); };
  window.addEventListener("dragenter", e => { if (!hasFiles(e) || !isAdmin()) return; e.preventDefault(); dragDepth++; $("#dragOverlay").hidden = false; $("#dropzone").classList.add("over"); });
  window.addEventListener("dragover", e => { if (hasFiles(e) && isAdmin()) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
  window.addEventListener("dragleave", e => { if (!hasFiles(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (dragDepth === 0) endDrag(); });
  window.addEventListener("drop", e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth = 0; endDrag(); if (!isAdmin()) return; const f = e.dataTransfer.files[0]; if (f) upload(f); });

  async function upload(file, reportDate) {
    if (!/\.(xlsx|xlsm|xls|csv|txt)$/i.test(file.name)) { toast("Please drop the Starlink export (.xlsx, .xls or .csv).", "err"); return; }
    setBusy(true, `Parsing ${file.name}…`);
    const fd = new FormData(); fd.append("file", file); if (reportDate) fd.append("report_date", reportDate);
    try {
      const detail = await api("/api/reports/upload", { method: "POST", body: fd });
      state.current = detail; state.filter = { q: "", dept: "", status: "all", segment: "" }; $("#search").value = "";
      await loadReports(); switchView("reports"); location.hash = String(detail.id); renderReport();
      const s = detail.summary;
      toast(`${detail.replaced ? "Replaced" : "Imported"} ${detail.report_date_label}: ${s.total} employees · ${s.halfday} half day${s.halfday === 1 ? "" : "s"} · ${s.pending} to resolve`, "ok", 6000);
    } catch (e) {
      if (e.status === 422 && /report date/i.test(e.message)) { state.pendingFile = file; openDateModal(); }
      else if (e.status !== 401 && e.status !== 428) toast(e.message, "err", 7000);
    } finally { setBusy(false); }
  }
  function setBusy(on, text) { $("#dropzone").classList.toggle("busy", on); $("#dropProgress").hidden = !on; if (text) $("#dropProgressText").textContent = text; $("#newUploadBtn").disabled = on; }

  function openDateModal() { $("#dateModalInput").value = ""; $("#dateModal").hidden = false; $("#dateModalInput").focus(); }
  $("#dateModalCancel").addEventListener("click", () => { $("#dateModal").hidden = true; state.pendingFile = null; });
  $("#dateModalOk").addEventListener("click", () => { const v = $("#dateModalInput").value; if (!v) { $("#dateModalInput").focus(); return; } const f = state.pendingFile; $("#dateModal").hidden = true; state.pendingFile = null; if (f) upload(f, v); });


  /* ============================================================ AUDIT TRAIL */
  const ICONS = {
    auth: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>',
    user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/></svg>',
    report: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
    correction: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 13l5 5L20 7"/></svg>',
    export: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12m0 0l-4-4m4 4l4-4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2"/></svg>',
    shift: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    access: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M5.5 5.5l13 13"/></svg>',
    audit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/><path d="M9 12l2 2 4-4"/></svg>',
    system: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  };
  const auditState = { period: "7d", q: "", actor: "", outcome: "", cats: new Set(), views: false, events: [], nextBefore: null, hasMore: false, timer: null, catalogLoaded: false, open: new Set() };
  state.auditFilter = null;

  function auditParams(extra = {}) {
    const p = new URLSearchParams();
    p.set("period", auditState.period);
    if (auditState.q) p.set("q", auditState.q);
    if (auditState.actor) p.set("actor_id", auditState.actor);
    if (auditState.outcome) p.set("outcome", auditState.outcome);
    if (auditState.cats.size) p.set("category", Array.from(auditState.cats).join(","));
    p.set("include_views", auditState.views ? "true" : "false");
    if (state.auditFilter && state.auditFilter.report_id) p.set("report_id", state.auditFilter.report_id);
    if (state.auditFilter && state.auditFilter.target) p.set("target", state.auditFilter.target);
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    return p;
  }

  async function loadAudit(reset) {
    if (!auditState.catalogLoaded) {
      try {
        const [cat, actors] = await Promise.all([api("/api/audit/actions"), api("/api/audit/actors")]);
        $("#auditCats").innerHTML = cat.categories.map(c => `<button class="chip" data-cat="${c.key}">${esc(c.label)}</button>`).join("");
        $("#auditActor").innerHTML = `<option value="">Everyone</option>` + actors.map(a => `<option value="${a.id}">${esc(a.name || a.username)}</option>`).join("");
        auditState.catalogLoaded = true;
      } catch (e) { toast(e.message, "err"); return; }
    }
    const ctx = $("#auditContext");
    if (state.auditFilter) { ctx.hidden = false; ctx.innerHTML = `<span>Showing events for <b>${esc(state.auditFilter.label)}</b></span><button id="auditClearCtx">Show everything</button>`; }
    else ctx.hidden = true;
    if (reset) { auditState.events = []; auditState.nextBefore = null; auditState.open.clear(); }
    try {
      const params = auditParams(reset ? {} : { before_id: auditState.nextBefore });
      const [page, stats] = await Promise.all([api(`/api/audit?${params}`), reset ? api(`/api/audit/stats?period=${auditState.period}`) : null]);
      auditState.events = reset ? page.events : auditState.events.concat(page.events);
      auditState.hasMore = page.has_more; auditState.nextBefore = page.next_before_id;
      if (stats) renderAuditStats(stats);
      renderTimeline();
    } catch (e) { if (e.status !== 401) toast(e.message, "err"); }
  }

  function renderAuditStats(s) {
    const v = s.last_verified;
    const integrity = v ? (v.outcome === "success" ? ["ok", "Intact", `verified ${timeAgo(v.ts)}`] : ["bad", "Broken", `at #${v.meta.broken_at}`]) : ["unknown", "Not verified", "run a check"];
    $("#auditStats").innerHTML = `
      <div class="stat static"><span class="v">${s.events}</span><span class="l">Events</span></div>
      <div class="stat static ${s.login_failures ? "mis" : ""}"><span class="v">${s.login_failures}</span><span class="l">Failed sign-ins</span></div>
      <div class="stat static ${s.denied ? "mis" : ""}"><span class="v">${s.denied}</span><span class="l">Denied requests</span></div>
      <div class="stat static ok"><span class="v">${s.corrections}</span><span class="l">Corrections</span></div>
      <div class="stat static hlf"><span class="v">${s.exports}</span><span class="l">Exports</span></div>
      <div class="stat static warn"><span class="v">${s.changes}</span><span class="l">Config &amp; data changes</span></div>
      <div class="stat static"><span class="v">${s.actors}</span><span class="l">Active people</span></div>
      <div class="stat static integrity ${integrity[0]}" title="${esc(integrity[2])}"><span class="v">${integrity[1]}</span><span class="l">Chain of ${s.total_events} · ${esc(integrity[2])}</span></div>`;
  }

  function dayKey(iso) { return new Date(iso).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" }); }
  const tTime = iso => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fmtVal = v => v === null || v === undefined ? "—" : (typeof v === "object" ? JSON.stringify(v) : String(v));

  function renderTimeline() {
    const tl = $("#auditTimeline"); const evs = auditState.events;
    if (!evs.length) { tl.innerHTML = `<div class="tl-empty">No events match these filters.</div>`; $("#auditMore").hidden = true; $("#auditCount").textContent = ""; return; }
    let html = ""; let lastDay = null;
    evs.forEach((e, i) => {
      const d = dayKey(e.ts);
      if (d !== lastDay) { const n = evs.filter(x => dayKey(x.ts) === d).length; html += `<div class="tl-day">${esc(d)}<span>${n} event${n === 1 ? "" : "s"}</span></div>`; lastDay = d; }
      const next = evs[i + 1]; const lastOfDay = !next || dayKey(next.ts) !== d;
      const firstOfDay = i === 0 || dayKey(evs[i - 1].ts) !== d;
      const open = auditState.open.has(e.id);
      html += `<div class="ev ${lastOfDay ? "last" : ""} ${firstOfDay && lastOfDay ? "single" : ""} ${open ? "open" : ""}" data-id="${e.id}">
        <div class="ev-ico ${e.category}">${ICONS[e.category] || ICONS.system}</div>
        <div class="ev-main">
          <div class="ev-sum">${summaryHtml(e)}</div>
          <div class="ev-meta">
            <span>${esc(e.action_label)}</span>
            ${e.actor.username ? `<span>by <button class="ev-link" data-actor="${e.actor.id}">${esc(e.actor.name || e.actor.username)}</button>${e.actor.role ? ` (${esc(ROLE_LABELS[e.actor.role] || e.actor.role)})` : ""}</span>` : ""}
            ${e.ip ? `<span class="mono">${esc(e.ip)}</span>` : ""}
            ${e.report_date ? `<span>report ${esc(shortDate(e.report_date))}</span>` : ""}
            ${e.dept_code ? `<span>dept ${esc(e.dept_code)}</span>` : ""}
            ${e.request_id ? `<span class="mono" title="Request id">#${esc(e.request_id.slice(0, 8))}</span>` : ""}
          </div>
          ${open ? detailHtml(e) : ""}
        </div>
        <div class="ev-side"><span class="ev-time">${tTime(e.ts)}</span><span class="ev-badges"><span class="sev sev-${e.severity}" title="${esc(e.severity)}"></span>${e.outcome !== "success" ? `<span class="outcome ${e.outcome}">${esc(e.outcome.toUpperCase())}</span>` : ""}</span></div>
      </div>`;
    });
    tl.innerHTML = html;
    $("#auditMore").hidden = !auditState.hasMore;
    $("#auditCount").textContent = `${evs.length} event${evs.length === 1 ? "" : "s"} loaded${auditState.hasMore ? " – more available" : ""}`;
  }

  function summaryHtml(e) {
    let t = esc(e.summary);
    if (e.actor.name) t = t.replace(esc(e.actor.name), `<b>${esc(e.actor.name)}</b>`);
    else if (e.actor.username) t = t.replace(esc(e.actor.username), `<b>${esc(e.actor.username)}</b>`);
    if (e.target.label) t = t.replace(esc(e.target.label), `<b>${esc(e.target.label)}</b>`);
    return t;
  }

  function detailHtml(e) {
    const changes = e.changes && e.changes.length ? `<div class="full"><h4>What changed</h4><table class="difftable"><tr><th>Field</th><th>Before</th><th>After</th></tr>${e.changes.map(c => `<tr><td>${esc(c.field)}</td><td class="from">${esc(fmtVal(c.from))}</td><td class="to">${esc(fmtVal(c.to))}</td></tr>`).join("")}</table></div>` : "";
    const snap = (title, obj) => obj && Object.keys(obj).length && !(e.changes && e.changes.length) ? `<div><h4>${title}</h4><dl class="kv">${Object.entries(obj).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(fmtVal(v))}</dd>`).join("")}</dl></div>` : "";
    const meta = e.meta && Object.keys(e.meta).length ? `<div><h4>Details</h4><dl class="kv">${Object.entries(e.meta).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(fmtVal(v))}</dd>`).join("")}</dl></div>` : "";
    const who = `<div><h4>Context</h4><dl class="kv">
      <dt>When</dt><dd>${esc(new Date(e.ts).toLocaleString())} (${esc(e.ts)})</dd>
      ${e.actor.username ? `<dt>Actor</dt><dd>${esc(e.actor.username)} · ${esc(e.actor.role || "")} · id ${e.actor.id}</dd>` : "<dt>Actor</dt><dd>anonymous / system</dd>"}
      ${e.ip ? `<dt>IP</dt><dd>${esc(e.ip)}</dd>` : ""}
      ${e.user_agent ? `<dt>Browser</dt><dd>${esc(e.user_agent)}</dd>` : ""}
      ${e.target.type ? `<dt>Target</dt><dd>${esc(e.target.type)} ${esc(e.target.id || "")} ${e.target.label ? "– " + esc(e.target.label) : ""}</dd>` : ""}
      ${e.paycode ? `<dt>PayCode</dt><dd>${esc(e.paycode)} (${esc(e.segment || "")})</dd>` : ""}
      ${e.request_id ? `<dt>Request</dt><dd>${esc(e.request_id)}</dd>` : ""}
      <dt>Event</dt><dd>#${e.id} · ${esc(e.action)} · ${esc(e.severity)}</dd>
    </dl></div>`;
    return `<div class="ev-detail">${changes}${snap("Before", e.before)}${snap("After", e.after)}${who}${meta}
      <div class="full hashline">hash ${esc(e.hash)}<br>prev ${esc(e.prev_hash || "— (genesis)")}</div></div>`;
  }

  $("#auditTimeline").addEventListener("click", e => {
    const a = e.target.closest("button[data-actor]"); if (a) { auditState.actor = a.dataset.actor; $("#auditActor").value = a.dataset.actor; loadAudit(true); return; }
    if (e.target.closest(".ev-detail")) return;
    const ev = e.target.closest(".ev"); if (!ev) return;
    const id = Number(ev.dataset.id);
    if (auditState.open.has(id)) auditState.open.delete(id); else auditState.open.add(id);
    renderTimeline();
  });
  $("#auditMore").addEventListener("click", () => loadAudit(false));
  $("#auditPeriod").addEventListener("change", e => { auditState.period = e.target.value; loadAudit(true); });
  $("#auditActor").addEventListener("change", e => { auditState.actor = e.target.value; loadAudit(true); });
  $("#auditOutcome").addEventListener("change", e => { auditState.outcome = e.target.value; loadAudit(true); });
  $("#auditViews").addEventListener("change", e => { auditState.views = e.target.checked; loadAudit(true); });
  let searchTimer = null;
  $("#auditSearch").addEventListener("input", e => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { auditState.q = e.target.value; loadAudit(true); }, 250); });
  $("#auditCats").addEventListener("click", e => {
    const b = e.target.closest(".chip"); if (!b) return;
    const k = b.dataset.cat; if (auditState.cats.has(k)) auditState.cats.delete(k); else auditState.cats.add(k);
    $$("#auditCats .chip").forEach(c => c.classList.toggle("active", auditState.cats.has(c.dataset.cat)));
    loadAudit(true);
  });
  $("#auditContext").addEventListener("click", e => { if (e.target.id === "auditClearCtx") { state.auditFilter = null; loadAudit(true); } });

  function stopAutoRefresh() { clearInterval(auditState.timer); auditState.timer = null; }
  $("#auditAuto").addEventListener("change", e => { stopAutoRefresh(); if (e.target.checked) auditState.timer = setInterval(() => loadAudit(true), 20000); });

  $("#auditVerifyBtn").addEventListener("click", async () => {
    const b = $("#auditVerifyBtn"); b.disabled = true; b.textContent = "Verifying…";
    try {
      const r = await api("/api/audit/verify", { method: "POST" });
      if (r.ok) toast(`Audit chain intact – ${r.checked} events verified`, "ok", 6000);
      else toast(`Audit chain BROKEN at event #${r.broken_at}: ${r.reason}`, "err", 12000);
      loadAudit(true);
    } catch (err) { toast(err.message, "err"); }
    finally { b.disabled = false; b.textContent = "Verify integrity"; }
  });
  $("#auditExportCsv").addEventListener("click", () => download(`/api/audit/export?${auditParams({ format: "csv" })}`));
  $("#auditExportMenuBtn").addEventListener("click", e => { e.stopPropagation(); const m = $("#auditExportMenu"); m.hidden = !m.hidden; });
  $("#auditExportMenu").addEventListener("click", e => { e.stopPropagation(); const b = e.target.closest(".menu-item"); if (!b) return; download(`/api/audit/export?${auditParams({ format: b.dataset.fmt })}`); $("#auditExportMenu").hidden = true; });
  document.addEventListener("click", () => { $("#auditExportMenu").hidden = true; });

  /* sessions */
  $("#auditSessionsBtn").addEventListener("click", () => { const p = $("#sessionsPanel"); p.hidden = !p.hidden; if (!p.hidden) loadSessions(); });
  $("#sessionsRefresh").addEventListener("click", loadSessions);
  async function loadSessions() {
    try {
      const rows = await api("/api/sessions");
      $("#sessionsBody").innerHTML = rows.map(s => `<tr>
        <td class="name">${esc(s.full_name || s.username)}<small>${esc(s.username)}</small></td>
        <td><span class="role-badge ${s.role}">${esc(ROLE_LABELS[s.role] || s.role)}</span></td>
        <td class="mono">${esc(s.ip || "")}</td>
        <td class="remarks" title="${esc(s.user_agent || "")}">${esc((s.user_agent || "").replace(/^Mozilla\/5\.0 /, "").slice(0, 60))}</td>
        <td class="mono">${esc(fmtDT(s.created_at))}</td><td class="mono">${esc(timeAgo(s.last_seen_at))}</td>
        <td class="act-col"><button class="btn sm ghost-danger" data-revoke="${s.id}">Revoke</button></td></tr>`).join("") || `<tr><td colspan="7" class="empty-note">No active sessions.</td></tr>`;
    } catch (e) { toast(e.message, "err"); }
  }
  $("#sessionsBody").addEventListener("click", async e => {
    const b = e.target.closest("button[data-revoke]"); if (!b) return;
    if (!b.classList.contains("confirm")) { b.classList.add("confirm"); b.textContent = "Confirm?"; setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Revoke"; }, 3000); return; }
    try { await api(`/api/sessions/${b.dataset.revoke}`, { method: "DELETE" }); toast("Session revoked"); loadSessions(); loadAudit(true); }
    catch (err) { toast(err.message, "err"); if (err.status === 401) showLogin(); }
  });

  /* ============================================================ RECORD HISTORY DRAWER */
  async function openHistory(id) {
    let h;
    try { h = await api(`/api/records/${id}/history`); } catch (e) { toast(e.message, "err"); return; }
    const r = h.record, o = r.original;
    $("#drawerTitle").textContent = r.name;
    $("#drawerMeta").innerHTML = `<span>${esc(r.paycode)} · ${esc(r.segment_label)}</span><span>${esc(r.dept_name || "")}</span><span>${esc(longDate(h.report.report_date))}</span>`;
    const cell = (label, now, was) => `<div><span>${label}</span><b>${esc(now || "—")}</b>${r.is_corrected && was !== now ? `<small>${esc(was || "—")}</small>` : ""}</div>`;
    const story = [];
    if (h.imported) story.push(`<li><span class="when">${esc(fmtDT(h.imported.ts))}</span><span class="what"><b>Imported</b> by ${esc(h.imported.actor.name || h.imported.actor.username || "system")} from <i>${esc(h.report.source_filename || "?")}</i> – machine values ${esc(o.time_in || "—")} → ${esc(o.time_out || "—")}, status ${esc(o.status)}${o.hrs ? `, ${esc(o.hrs)} h` : ""}</span></li>`);
    else story.push(`<li><span class="when">${esc(fmtDT(h.report.uploaded_at))}</span><span class="what"><b>Imported</b> from <i>${esc(h.report.source_filename || "?")}</i> (before the audit trail existed) – machine values ${esc(o.time_in || "—")} → ${esc(o.time_out || "—")}, status ${esc(o.status)}</span></li>`);
    for (const e of h.events) {
      const cls = e.outcome !== "success" ? "fail" : (e.action === "correction.reverted" ? "revert" : "fix");
      story.push(`<li class="${cls}"><span class="when">${esc(fmtDT(e.ts))}${e.ip ? ` · ${esc(e.ip)}` : ""}</span><span class="what">${summaryHtml(e)}</span>${e.outcome !== "success" ? ` <span class="outcome ${e.outcome}">${esc(e.outcome.toUpperCase())}</span>` : ""}</li>`);
    }
    if (r.notes && r.notes.length) story.push(`<li><span class="when">machine flags</span><span class="what remark">${esc(r.notes.join(" · "))}</span></li>`);
    const days = h.other_days.map(d => `<tr class="${d.record_id === r.id ? "cur" : ""}"><td>${esc(shortDate(d.report_date))}</td><td class="mono">${esc(d.time_in || "—")} → ${esc(d.time_out || "—")}</td><td>${esc(d.status)}${d.corrected ? ' <span class="tag fixed">FIXED</span>' : ""}</td></tr>`).join("");
    $("#drawerBody").innerHTML = `
      <div><h4>Current values${r.is_corrected ? " (corrected – machine values struck through)" : ""}</h4>
        <div class="punch-now">${cell("In", r.time_in, o.time_in)}${cell("Out", r.time_out, o.time_out)}${cell("Hours", r.hrs, o.hrs)}${cell("Status", r.status, o.status)}</div></div>
      <div><h4>Story of this record</h4><ul class="story">${story.join("")}</ul></div>
      <div><h4>Source file</h4><dl class="kv"><dt>File</dt><dd>${esc(h.report.source_filename || "—")}</dd><dt>SHA-256</dt><dd>${esc(h.report.source_sha256 || "—")}</dd><dt>Uploaded</dt><dd>${esc(fmtDT(h.report.uploaded_at))}</dd></dl></div>
      <div><h4>Same employee, other days</h4><table class="days"><tr><th>Day</th><th>Punches</th><th>Status</th></tr>${days}</table></div>
      ${state.me.role !== "hr" ? `<div><button class="btn" id="drawerAudit">Open in audit trail</button></div>` : ""}`;
    $("#drawer").hidden = false;
    const ab = $("#drawerAudit"); if (ab) ab.addEventListener("click", () => { $("#drawer").hidden = true; state.auditFilter = { target: `record:${r.id}`, label: `${r.name} on ${shortDate(h.report.report_date)}` }; switchView("audit"); });
  }
  $("#drawerClose").addEventListener("click", () => { $("#drawer").hidden = true; });
  $("#drawer").addEventListener("click", e => { if (e.target.id === "drawer") $("#drawer").hidden = true; });


  /* ============================================================ INTEGRATIONS (admin) */
  const integ = { catalog: null, connections: [], jobs: [], runs: [], editConn: null, editJob: null, preview: null, runJob: null, schema: null, schemaConn: null };

  async function loadIntegrations() {
    try {
      if (!integ.catalog) integ.catalog = await api("/api/sync/catalog");
      [integ.connections, integ.jobs, integ.runs] = await Promise.all([api("/api/connections"), api("/api/sync/jobs"), api("/api/sync/runs?limit=40")]);
    } catch (e) { toast(e.message, "err"); return; }
    renderConnections(); renderJobs(); renderRuns();
  }
  const connName = id => { const c = integ.connections.find(x => x.id === id); return c ? c.name : "?"; };

  function renderConnections() {
    $("#connCount").textContent = integ.connections.length || "";
    $("#connCards").innerHTML = integ.connections.map(c => `
      <div class="card" data-id="${c.id}">
        <div class="top"><b>${esc(c.name)}</b><span class="dialect">${esc(c.dialect_label)}</span></div>
        <div class="sub2">${c.has_url ? "custom URL" : esc([c.host && (c.host + (c.port ? ":" + c.port : "")), c.database].filter(Boolean).join(" · "))}${c.username ? ` · ${esc(c.username)}` : ""}</div>
        <div class="status ${c.last_test_ok === true ? "ok" : c.last_test_ok === false ? "bad" : ""}">${c.last_test_ok === null || c.last_test_ok === undefined ? "Not tested yet" : esc(c.last_test_message || "")}${c.last_test_at ? ` · ${esc(timeAgo(c.last_test_at))}` : ""}${!c.driver_available ? ' · <span style="color:var(--mis)">driver missing on server</span>' : ""}</div>
        <div class="sub2">${c.read_only ? "Read-only" : "Read &amp; write"} · ${c.jobs} job${c.jobs === 1 ? "" : "s"}${c.server_info ? ` · ${esc(c.server_info.slice(0, 60))}` : ""}</div>
        <div class="btns"><button class="btn sm" data-ctest="${c.id}">Test</button><button class="btn sm" data-cconsole="${c.id}">Console</button><button class="btn sm" data-cedit="${c.id}">Edit</button><button class="btn sm ghost-danger" data-cdel="${c.id}">Delete</button></div>
      </div>`).join("") || `<p class="empty-note">No connections yet. Add the Starlink SQL Server to begin.</p>`;
  }

  function schedText(j) {
    if (!j.schedule_enabled) return "manual";
    const off = j.schedule_offset_days === 0 ? "today" : j.schedule_offset_days === 1 ? "yesterday" : `D-${j.schedule_offset_days}`;
    return `daily ${j.schedule_time} · ${off}`;
  }
  function renderJobs() {
    $("#jobCount").textContent = integ.jobs.length || "";
    $("#jobsEmpty").hidden = integ.jobs.length > 0;
    $("#jobsBody").innerHTML = integ.jobs.map(j => `
      <tr class="${j.enabled ? "" : "inactive"}" data-id="${j.id}">
        <td class="name">${esc(j.name)}${j.description ? `<small>${esc(j.description)}</small>` : ""}</td>
        <td><span class="dir ${j.direction}">${j.direction.toUpperCase()}</span></td>
        <td>${esc(j.connection_name || "")}</td>
        <td>${j.direction === "pull" ? (j.mode === "punches" ? "Punch log → attendance" : "Daily rows → attendance") : `${esc(j.dataset)} → <span class="mono">${esc((j.target_schema ? j.target_schema + "." : "") + (j.target_table || ""))}</span>${j.auto_push ? ' <span class="tag fixed">AUTO</span>' : ""}`}</td>
        <td>${esc(schedText(j))}${!j.enabled ? ' <span class="tag manual">DISABLED</span>' : ""}</td>
        <td>${j.last_status ? `<span class="run-status ${j.last_status}">${esc(j.last_status.toUpperCase())}</span> <small style="color:var(--muted)">${esc(timeAgo(j.last_run_at))}</small>` : '<span style="color:var(--faint)">never</span>'}</td>
        <td class="act-col"><button class="btn sm primary" data-jrun="${j.id}">Run…</button> <button class="btn sm" data-jedit="${j.id}">Edit</button> <button class="btn sm ghost-danger" data-jdel="${j.id}">Delete</button></td>
      </tr>`).join("");
  }
  function renderRuns() {
    $("#runsEmpty").hidden = integ.runs.length > 0;
    $("#runsBody").innerHTML = integ.runs.map(r => `
      <tr>
        <td class="mono">${esc(fmtDT(r.started_at))}</td>
        <td>${esc(r.job_name)} <span class="dir ${r.direction}">${r.direction.toUpperCase()}</span></td>
        <td>${esc(r.trigger)}${r.actor ? ` · ${esc(r.actor)}` : ""}</td>
        <td><span class="run-status ${r.status}">${esc(r.status.toUpperCase())}</span></td>
        <td class="c mono">${r.rows_in}${r.direction === "pull" ? ` → ${r.report_ids.length} day${r.report_ids.length === 1 ? "" : "s"}` : ` → ${r.rows_out}`}</td>
        <td class="remarks">${esc(r.message || "")}${r.params.date_from ? ` <small style="color:var(--faint)">(${esc(r.params.date_from)}${r.params.date_to && r.params.date_to !== r.params.date_from ? " – " + esc(r.params.date_to) : ""})</small>` : ""}</td>
        <td class="act-col">${r.report_ids.length ? `<button class="btn sm" data-open-report="${r.report_ids[0]}">Open</button>` : ""} ${r.log.length ? `<button class="btn sm" data-runlog="${r.id}">Log</button>` : ""}</td>
      </tr>`).join("");
  }
  $("#runsRefresh").addEventListener("click", loadIntegrations);
  $("#runsBody").addEventListener("click", e => {
    const o = e.target.closest("button[data-open-report]"); if (o) { switchView("reports"); openReport(Number(o.dataset.openReport)); return; }
    const l = e.target.closest("button[data-runlog]"); if (l) { const r = integ.runs.find(x => x.id === Number(l.dataset.runlog)); toast(r.log.join("\n"), "", 9000); }
  });

  /* ---- connections ---- */
  $("#connCards").addEventListener("click", async e => {
    const t = e.target.closest("button"); if (!t) return;
    if (t.dataset.ctest) { t.disabled = true; t.textContent = "Testing…"; try { const r = await api(`/api/connections/${t.dataset.ctest}/test`, { method: "POST" }); toast(r.ok ? `✓ ${r.message} · ${r.latency_ms} ms` : `✗ ${r.message}`, r.ok ? "ok" : "err", 6000); } catch (err) { toast(err.message, "err"); } loadIntegrations(); return; }
    if (t.dataset.cconsole) { openConsole(Number(t.dataset.cconsole)); return; }
    if (t.dataset.cedit) { openConnModal(integ.connections.find(c => c.id === Number(t.dataset.cedit))); return; }
    if (t.dataset.cdel) {
      if (!t.classList.contains("confirm")) { t.classList.add("confirm"); t.textContent = "Confirm?"; setTimeout(() => { t.classList.remove("confirm"); t.textContent = "Delete"; }, 3000); return; }
      try { await api(`/api/connections/${t.dataset.cdel}`, { method: "DELETE" }); toast("Connection deleted"); loadIntegrations(); } catch (err) { toast(err.message, "err"); }
    }
  });
  $("#newConnBtn").addEventListener("click", () => openConnModal(null));
  function dialectSpec(k) { return integ.catalog.dialects.find(d => d.key === k) || {}; }
  function openConnModal(c) {
    integ.editConn = c;
    $("#connModalTitle").textContent = c ? `Edit ${c.name}` : "New connection";
    $("#cDialect").innerHTML = integ.catalog.dialects.map(d => `<option value="${d.key}" ${!d.available ? 'data-missing="1"' : ""}>${esc(d.label)}${d.available ? "" : " (driver not installed)"}</option>`).join("");
    $("#cName").value = c ? c.name : ""; $("#cDialect").value = c ? c.dialect : "mssql";
    $("#cHost").value = c ? c.host || "" : ""; $("#cPort").value = c && c.port ? c.port : ""; $("#cDatabase").value = c ? c.database || "" : "";
    $("#cUser").value = c ? c.username || "" : ""; $("#cPass").value = ""; $("#cPassLabel").textContent = c && c.has_password ? "Password (leave blank to keep)" : "Password";
    $("#cOptions").value = c ? Object.entries(c.options || {}).map(([k, v]) => `${k}=${v}`).join("\n") : "";
    $("#cUrl").value = ""; $("#cReadOnly").checked = c ? c.read_only : true; $("#cNotes").value = c ? c.notes || "" : "";
    $("#connError").hidden = true; $("#connTestResult").hidden = true;
    syncConnForm(); $("#connModal").hidden = false; setTimeout(() => $("#cName").focus(), 50);
  }
  function syncConnForm() {
    const d = $("#cDialect").value; const spec = dialectSpec(d); const sqlite = d === "sqlite";
    $("#cHostField").hidden = $("#cPortField").hidden = $("#cUserField").hidden = $("#cPassField").hidden = sqlite;
    $("#cDbLabel").textContent = sqlite ? "Database file path (on the ShiftKey server)" : "Database";
    $("#cPort").placeholder = spec.default_port || "";
    $("#cHint").textContent = (spec.hint || "") + (spec.available ? "" : ` The ${spec.driver} driver is not installed in this ShiftKey image.`);
  }
  $("#cDialect").addEventListener("change", syncConnForm);
  function connBody() {
    const options = {}; $("#cOptions").value.split("\n").forEach(l => { const i = l.indexOf("="); if (i > 0) options[l.slice(0, i).trim()] = l.slice(i + 1).trim(); });
    const body = { name: $("#cName").value.trim(), dialect: $("#cDialect").value, host: $("#cHost").value.trim() || null,
      port: $("#cPort").value ? Number($("#cPort").value) : null, database: $("#cDatabase").value.trim() || null,
      username: $("#cUser").value.trim() || null, options, read_only: $("#cReadOnly").checked, notes: $("#cNotes").value.trim() || null };
    if ($("#cPass").value || !integ.editConn) body.password = $("#cPass").value;
    if ($("#cUrl").value.trim()) body.url = $("#cUrl").value.trim();
    return body;
  }
  $("#connTest").addEventListener("click", async () => {
    const b = $("#connTest"); b.disabled = true; b.textContent = "Testing…"; const out = $("#connTestResult");
    try {
      const body = connBody(); if (integ.editConn && !body.password) body.password = undefined;
      const r = integ.editConn && !$("#cPass").value ? await api(`/api/connections/${integ.editConn.id}/test`, { method: "POST" }) : await api("/api/connections/test", { method: "POST", json: Object.assign({ password: "" }, body) });
      out.hidden = false; out.className = `test-result ${r.ok ? "ok" : "bad"}`;
      out.textContent = r.ok ? `✓ ${r.message} · ${r.latency_ms} ms${r.server ? " · " + r.server.slice(0, 80) : ""}` : `✗ ${r.message}`;
    } catch (err) { out.hidden = false; out.className = "test-result bad"; out.textContent = err.message; }
    finally { b.disabled = false; b.textContent = "Test connection"; }
  });
  $("#connCancel").addEventListener("click", () => { $("#connModal").hidden = true; });
  $("#connForm").addEventListener("submit", async e => {
    e.preventDefault();
    try {
      const body = connBody();
      if (integ.editConn) await api(`/api/connections/${integ.editConn.id}`, { method: "PATCH", json: body });
      else await api("/api/connections", { method: "POST", json: Object.assign({ password: "" }, body) });
      $("#connModal").hidden = true; toast(`Saved connection ${body.name}`, "ok"); loadIntegrations();
    } catch (err) { $("#connError").textContent = err.message; $("#connError").hidden = false; }
  });

  /* ---- console + schema ---- */
  $("#consoleBtn").addEventListener("click", () => openConsole(integ.connections[0] && integ.connections[0].id));
  function openConsole(connId) {
    if (!integ.connections.length) { toast("Add a connection first.", "err"); return; }
    $("#qConn").innerHTML = integ.connections.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
    if (connId) $("#qConn").value = connId;
    if (!$("#qDate").value) { const d = new Date(Date.now() - 86400000); $("#qDate").value = d.toISOString().slice(0, 10); }
    $("#qResult").innerHTML = ""; $("#qInfo").textContent = ""; $("#schemaPane").hidden = true;
    $("#consoleModal").hidden = false; setTimeout(() => $("#qSql").focus(), 50);
  }
  $("#consoleClose").addEventListener("click", () => { $("#consoleModal").hidden = true; });
  $("#qSql").addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runConsole(); } });
  $("#qRun").addEventListener("click", runConsole);
  async function runConsole() {
    const sql = $("#qSql").value.trim(); if (!sql) return;
    $("#qRun").disabled = true; $("#qInfo").textContent = "Running…";
    try {
      const r = await api(`/api/connections/${$("#qConn").value}/query`, { method: "POST", json: { sql, params: { report_date: $("#qDate").value, date_from: $("#qDate").value, date_to: $("#qDate").value }, limit: Number($("#qLimit").value) || 200 } });
      integ.lastQuery = { sql, result: r, connection_id: Number($("#qConn").value) };
      $("#qInfo").textContent = `${r.row_count}${r.truncated ? "+" : ""} rows · ${r.elapsed_ms} ms`;
      $("#qResult").innerHTML = resultTable(r);
    } catch (err) { $("#qResult").innerHTML = `<div class="err">${esc(err.message)}</div>`; $("#qInfo").textContent = ""; }
    finally { $("#qRun").disabled = false; }
  }
  function resultTable(r, mapping) {
    if (!r.columns.length) return `<div class="err" style="color:var(--muted)">Query returned no columns.</div>`;
    const inv = {}; Object.entries(mapping || r.suggested_mapping || {}).forEach(([f, c]) => { inv[c] = f; });
    return `<table><thead><tr>${r.columns.map(c => `<th>${esc(c)}${inv[c] ? `<small>→ ${esc(inv[c])}</small>` : ""}</th>`).join("")}</tr></thead>
      <tbody>${r.rows.map(row => `<tr>${row.map(v => v === null ? `<td class="null">null</td>` : `<td title="${esc(String(v))}">${esc(String(v))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }
  $("#qSchemaBtn").addEventListener("click", async () => {
    const pane = $("#schemaPane"); pane.hidden = !pane.hidden; if (pane.hidden) return;
    const cid = Number($("#qConn").value);
    if (integ.schemaConn !== cid) { $("#schemaList").innerHTML = `<li>Loading…</li>`; try { integ.schema = await api(`/api/connections/${cid}/schema`); integ.schemaConn = cid; } catch (err) { $("#schemaList").innerHTML = `<li style="color:var(--mis)">${esc(err.message)}</li>`; return; } }
    renderSchema();
  });
  $("#qConn").addEventListener("change", () => { if (!$("#schemaPane").hidden) { integ.schemaConn = null; $("#qSchemaBtn").click(); $("#qSchemaBtn").click(); } });
  $("#schemaSearch").addEventListener("input", renderSchema);
  function renderSchema() {
    const q = $("#schemaSearch").value.toLowerCase();
    const list = (integ.schema || []).filter(t => !q || t.name.toLowerCase().includes(q) || (t.schema || "").toLowerCase().includes(q));
    $("#schemaList").innerHTML = list.map(t => `<li data-table="${esc(t.name)}" data-schema="${esc(t.schema || "")}"><span>${t.schema ? `<small>${esc(t.schema)}.</small>` : ""}${esc(t.name)}</span><small>${t.type}</small></li>`).join("") || `<li style="color:var(--muted)">No tables</li>`;
  }
  $("#schemaList").addEventListener("click", async e => {
    const li = e.target.closest("li[data-table]"); if (!li) return;
    $$("#schemaList li").forEach(x => x.classList.toggle("active", x === li));
    const q = li.dataset.schema ? `?schema=${encodeURIComponent(li.dataset.schema)}&preview=true&limit=50` : "?preview=true&limit=50";
    try {
      const d = await api(`/api/connections/${$("#qConn").value}/schema/${encodeURIComponent(li.dataset.table)}${q}`);
      const existing = li.nextElementSibling && li.nextElementSibling.classList.contains("schema-cols-li");
      $$("#schemaList .schema-cols-li").forEach(x => x.remove());
      if (!existing) li.insertAdjacentHTML("afterend", `<li class="schema-cols-li" style="cursor:default"><div class="schema-cols">${d.columns.map(c => `<div><b>${esc(c.name)}${c.primary_key ? " 🔑" : ""}</b><span>${esc(c.type)}</span></div>`).join("")}</div></li>`);
      const full = (li.dataset.schema ? li.dataset.schema + "." : "") + li.dataset.table;
      $("#qSql").value = `SELECT * FROM ${full}` + (dialectSpec(integ.connections.find(c => c.id === Number($("#qConn").value)).dialect).key === "mssql" ? "" : "");
      $("#qResult").innerHTML = resultTable(d.preview); $("#qInfo").textContent = `${d.preview.row_count}${d.preview.truncated ? "+" : ""} rows (preview of ${full})`;
      integ.lastQuery = { sql: $("#qSql").value, result: d.preview, connection_id: Number($("#qConn").value) };
    } catch (err) { toast(err.message, "err"); }
  });
  $("#qToJob").addEventListener("click", () => {
    const sql = $("#qSql").value.trim(); if (!sql) return;
    $("#consoleModal").hidden = true;
    openJobModal(null, { direction: "pull", connection_id: Number($("#qConn").value), sql, mapping: integ.lastQuery && integ.lastQuery.sql === sql ? integ.lastQuery.result.suggested_mapping : null, previewDate: $("#qDate").value });
  });

  /* ---- jobs ---- */
  $("#newJobBtn").addEventListener("click", () => openJobModal(null));
  $("#jobsBody").addEventListener("click", async e => {
    const t = e.target.closest("button"); if (!t) return;
    if (t.dataset.jedit) { openJobModal(integ.jobs.find(j => j.id === Number(t.dataset.jedit))); return; }
    if (t.dataset.jrun) { openRunModal(integ.jobs.find(j => j.id === Number(t.dataset.jrun))); return; }
    if (t.dataset.jdel) {
      if (!t.classList.contains("confirm")) { t.classList.add("confirm"); t.textContent = "Confirm?"; setTimeout(() => { t.classList.remove("confirm"); t.textContent = "Delete"; }, 3000); return; }
      try { await api(`/api/sync/jobs/${t.dataset.jdel}`, { method: "DELETE" }); toast("Job deleted"); loadIntegrations(); } catch (err) { toast(err.message, "err"); }
    }
  });

  function openJobModal(j, preset) {
    if (!integ.connections.length) { toast("Add a connection first.", "err"); return; }
    integ.editJob = j; integ.preview = null;
    const v = Object.assign({ direction: "pull", mode: "daily", options: {}, mapping: {}, employees_mapping: {}, dataset: "attendance", push_mode: "upsert", create_table: true, auto_push: false, schedule_enabled: false, schedule_time: "06:00", schedule_offset_days: 1, enabled: true }, j || {}, preset || {});
    $("#jobModalTitle").textContent = j ? `Edit ${j.name}` : "New sync job";
    $("#jName").value = v.name || ""; $("#jDirection").value = v.direction;
    $("#jConn").innerHTML = integ.connections.map(c => `<option value="${c.id}">${esc(c.name)}${c.read_only ? " (read-only)" : ""}</option>`).join("");
    $("#jConn").value = v.connection_id || integ.connections[0].id;
    $("#jMode").value = v.mode || "daily"; $("#jHrsFmt").value = v.options.hrs_format || "auto"; $("#jDateFmt").value = v.options.date_param_format || "";
    $("#jSql").value = v.sql || ""; $("#jEmpSql").value = v.employees_sql || "";
    $("#jDayStart").value = v.options.day_start || "00:00"; $("#jMaxSpan").value = v.options.max_span_hours || 16;
    $("#jReplace").checked = $("#jReplace2").checked = v.options.replace_existing !== false;
    $("#jPreviewDate").value = v.previewDate || new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    $("#jPreviewOut").innerHTML = ""; $("#jPreviewInfo").textContent = "";
    integ.jobMapping = Object.assign({}, v.mapping || {}); integ.jobEmpMapping = Object.assign({}, v.employees_mapping || {}); integ.jobColumns = null; integ.jobEmpColumns = null;
    renderMapping();
    $("#jDataset").innerHTML = integ.catalog.datasets.map(d => `<option value="${d.key}">${esc(d.label)}</option>`).join("");
    $("#jDataset").value = v.dataset || "attendance"; $("#jTable").value = v.target_table || ""; $("#jSchema").value = v.target_schema || "";
    $("#jPushMode").innerHTML = integ.catalog.push_modes.map(m => `<option value="${m.key}">${esc(m.label)}</option>`).join(""); $("#jPushMode").value = v.push_mode || "upsert";
    $("#jKeys").value = (v.key_fields || []).join(", "); $("#jCreate").checked = v.create_table !== false; $("#jAutoPush").checked = !!v.auto_push;
    renderColMap(v.column_map);
    $("#jSchedOn").checked = !!v.schedule_enabled; $("#jSchedTime").value = v.schedule_time || "06:00"; $("#jOffset").value = String(v.schedule_offset_days ?? 1);
    $("#jEnabled").checked = v.enabled !== false; $("#jDesc").value = v.description || "";
    $("#jobError").hidden = true; syncJobForm(); $("#jobModal").hidden = false; setTimeout(() => $("#jName").focus(), 50);
  }
  function syncJobForm() {
    const pull = $("#jDirection").value === "pull"; const punches = $("#jMode").value === "punches";
    $("#jPull").hidden = !pull; $("#jPush").hidden = pull;
    $("#jEmpField").hidden = !punches; $("#jPunchOpts").hidden = !punches; $("#jReplaceDaily").hidden = punches;
    $("#jSqlLabel").innerHTML = punches ? "Punch log query – rows of (employee, punch time[, direction]) between <code>:date_from</code> and <code>:date_to</code>" : "Daily rows query – use <code>:report_date</code> (one day per run) or <code>:date_from</code>/<code>:date_to</code> with a mapped date column";
    if (!pull) { const ds = integ.catalog.datasets.find(d => d.key === $("#jDataset").value); if (ds && !$("#jTable").value) $("#jTable").placeholder = ds.default_table; if (ds && !$("#jKeys").value) $("#jKeys").placeholder = ds.key_fields.join(", "); }
    renderMapping();
  }
  $("#jDirection").addEventListener("change", syncJobForm); $("#jMode").addEventListener("change", syncJobForm);
  $("#jDataset").addEventListener("change", () => { $("#jTable").value = ""; $("#jKeys").value = ""; renderColMap(null); syncJobForm(); });

  function renderMapping() {
    const punches = $("#jMode").value === "punches";
    const blocks = [];
    const block = (title, fields, mapping, columns, sample) => {
      const opts = cols => `<option value="">— not mapped —</option>` + (cols || []).map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
      return `<div class="map-group">${title}${columns ? "" : " · run the preview to see the source columns"}</div>` + fields.map(f => {
        const cur = mapping[f.key] || ""; const extra = cur && columns && !columns.includes(cur) ? `<option value="${esc(cur)}">${esc(cur)} (not in preview)</option>` : "";
        const sv = sample && cur && sample[cur] !== undefined ? ` · e.g. ${esc(String(sample[cur]))}` : "";
        return `<div class="map-row ${f.required ? "req" : ""}"><div class="lbl"><b>${esc(f.label)}</b><small>${esc(f.hint || "")}${sv}</small></div>
          <select data-map="${title === "Employee master" ? "emp" : "main"}" data-field="${f.key}" class="${cur ? "mapped" : ""}">${opts(columns)}${extra}</select></div>`;
      }).join("");
    };
    if (punches) {
      blocks.push(block("Punch log", integ.catalog.punch_fields, integ.jobMapping, integ.jobColumns, integ.jobSample));
      blocks.push(block("Employee master", integ.catalog.fields.filter(f => ["paycode", "name", "card_no", "dept_code", "dept_name", "shift", "shift_start"].includes(f.key)), integ.jobEmpMapping, integ.jobEmpColumns, integ.jobEmpSample));
    } else blocks.push(block("Column mapping", integ.catalog.fields, integ.jobMapping, integ.jobColumns, integ.jobSample));
    $("#jMapping").innerHTML = blocks.join("");
    $$("#jMapping select").forEach(sel => { const m = sel.dataset.map === "emp" ? integ.jobEmpMapping : integ.jobMapping; sel.value = m[sel.dataset.field] || ""; });
  }
  $("#jMapping").addEventListener("change", e => { const sel = e.target.closest("select[data-map]"); if (!sel) return; const m = sel.dataset.map === "emp" ? integ.jobEmpMapping : integ.jobMapping; if (sel.value) m[sel.dataset.field] = sel.value; else delete m[sel.dataset.field]; sel.classList.toggle("mapped", !!sel.value); });

  function jobOptions() {
    const o = { hrs_format: $("#jHrsFmt").value, replace_existing: $("#jMode").value === "punches" ? $("#jReplace").checked : $("#jReplace2").checked };
    if ($("#jDateFmt").value.trim()) o.date_param_format = $("#jDateFmt").value.trim();
    if ($("#jMode").value === "punches") { o.day_start = $("#jDayStart").value || "00:00"; o.max_span_hours = Number($("#jMaxSpan").value) || 16; }
    return o;
  }
  $("#jPreview").addEventListener("click", async () => {
    const b = $("#jPreview"); b.disabled = true; $("#jPreviewInfo").textContent = "Running…"; $("#jobError").hidden = true;
    try {
      const pv = await api("/api/sync/preview", { method: "POST", json: { connection_id: Number($("#jConn").value), mode: $("#jMode").value, sql: $("#jSql").value, employees_sql: $("#jEmpSql").value || null,
        mapping: Object.keys(integ.jobMapping).length ? integ.jobMapping : null, employees_mapping: Object.keys(integ.jobEmpMapping).length ? integ.jobEmpMapping : null, options: jobOptions(), date: $("#jPreviewDate").value, limit: 50 } });
      integ.preview = pv; integ.jobColumns = pv.main.columns; integ.jobSample = pv.main.rows[0] ? Object.fromEntries(pv.main.columns.map((c, i) => [c, pv.main.rows[0][i]])) : null;
      if (!Object.keys(integ.jobMapping).length) integ.jobMapping = Object.assign({}, pv.suggested_mapping);
      if (pv.employees) { integ.jobEmpColumns = pv.employees.columns; integ.jobEmpSample = pv.employees.rows[0] ? Object.fromEntries(pv.employees.columns.map((c, i) => [c, pv.employees.rows[0][i]])) : null; if (!Object.keys(integ.jobEmpMapping).length) integ.jobEmpMapping = Object.assign({}, pv.suggested_employees_mapping || {}); }
      renderMapping();
      $("#jPreviewInfo").textContent = `${pv.main.row_count}${pv.main.truncated ? "+" : ""} source rows · ${pv.main.elapsed_ms} ms`;
      let html = "";
      if (pv.parse_error) html += `<div class="form-error">${esc(pv.parse_error)}</div>`;
      for (const p of pv.parsed) {
        const s = p.summary;
        html += `<div class="pv-summary"><span>Day <b>${esc(p.report_date || "?")}</b></span><span><b>${s.total}</b> employees</span><span>P <b>${s.present}</b></span><span>A <b>${s.absent}</b></span><span class="${s.halfday ? "" : ""}">HLF <b>${s.halfday}</b></span><span class="${s.mispunch ? "bad" : ""}">MIS <b>${s.mispunch}</b></span><span>review <b>${s.warnings}</b></span><span>Indian <b>${s.by_segment.indian || 0}</b> · Local <b>${s.by_segment.local || 0}</b></span><span>${s.departments.length} departments</span></div>`;
        if (p.problems.length) html += `<div class="form-error">${esc(p.problems.join(" "))}</div>`;
        const rows = p.flagged.length ? p.flagged : p.sample;
        html += `<div class="pv-table"><table><thead><tr><th>${p.flagged.length ? "Flagged" : "Sample"}</th><th>PayCode</th><th>Department</th><th>In</th><th>Out</th><th>Hrs</th><th>Status</th><th>Notes</th></tr></thead><tbody>${rows.map(x => `<tr><td>${esc(x.name)}</td><td>${esc(x.paycode)}</td><td>${esc(x.dept || "")}</td><td>${esc(x.in || "—")}</td><td>${esc(x.out || "—")}</td><td>${esc(x.hrs || "")}</td><td>${esc(x.status)}</td><td>${esc((x.notes || []).join(" · "))}</td></tr>`).join("")}</tbody></table></div>`;
      }
      if (!pv.parsed.length && !pv.parse_error) html += `<div class="hint-box">The query returned ${pv.main.row_count} rows but none could be turned into attendance records – check the PayCode mapping.</div>`;
      html += `<div class="section-head small"><h4>Source columns</h4></div><div class="pv-table">${resultTable(Object.assign({}, pv.main, { rows: pv.main.rows.slice(0, 5) }), integ.jobMapping)}</div>`;
      $("#jPreviewOut").innerHTML = html;
    } catch (err) { $("#jPreviewInfo").textContent = ""; $("#jobError").textContent = err.message; $("#jobError").hidden = false; }
    finally { b.disabled = false; }
  });

  function renderColMap(colmap) {
    const ds = integ.catalog.datasets.find(d => d.key === $("#jDataset").value); if (!ds) return;
    $("#jColMap").innerHTML = ds.columns.map(c => { const on = !colmap || colmap[c.name]; const target = colmap && colmap[c.name] ? colmap[c.name] : c.name;
      return `<label><input type="checkbox" data-col="${esc(c.name)}" ${on ? "checked" : ""}><span title="${esc(c.type)}">${esc(c.name)}</span><input type="text" data-target="${esc(c.name)}" value="${esc(target)}"></label>`; }).join("");
  }
  function colMapBody() {
    const ds = integ.catalog.datasets.find(d => d.key === $("#jDataset").value);
    const m = {}; let identity = true;
    $$("#jColMap label").forEach(l => { const cb = l.querySelector("input[type=checkbox]"); const t = l.querySelector("input[type=text]").value.trim(); if (cb.checked) { m[cb.dataset.col] = t || cb.dataset.col; if (t && t !== cb.dataset.col) identity = false; } else identity = false; });
    return identity && Object.keys(m).length === ds.columns.length ? null : m;
  }
  $("#jobCancel").addEventListener("click", () => { $("#jobModal").hidden = true; });
  $("#jobForm").addEventListener("submit", async e => {
    e.preventDefault();
    const pull = $("#jDirection").value === "pull";
    const body = { name: $("#jName").value.trim(), direction: $("#jDirection").value, connection_id: Number($("#jConn").value), enabled: $("#jEnabled").checked, description: $("#jDesc").value.trim() || null,
      schedule_enabled: $("#jSchedOn").checked, schedule_time: $("#jSchedTime").value || null, schedule_offset_days: Number($("#jOffset").value) };
    if (pull) Object.assign(body, { mode: $("#jMode").value, sql: $("#jSql").value, employees_sql: $("#jEmpSql").value || null, mapping: integ.jobMapping, employees_mapping: integ.jobEmpMapping, options: jobOptions() });
    else Object.assign(body, { dataset: $("#jDataset").value, target_table: $("#jTable").value.trim() || null, target_schema: $("#jSchema").value.trim() || null, push_mode: $("#jPushMode").value,
      key_fields: $("#jKeys").value.trim() ? $("#jKeys").value.split(",").map(x => x.trim()).filter(Boolean) : null, column_map: colMapBody(), create_table: $("#jCreate").checked, auto_push: $("#jAutoPush").checked });
    try {
      if (integ.editJob) await api(`/api/sync/jobs/${integ.editJob.id}`, { method: "PATCH", json: body }); else await api("/api/sync/jobs", { method: "POST", json: body });
      $("#jobModal").hidden = true; toast(`Saved job ${body.name}`, "ok"); loadIntegrations();
    } catch (err) { $("#jobError").textContent = err.message; $("#jobError").hidden = false; $("#jobError").scrollIntoView({ block: "nearest" }); }
  });

  /* ---- run ---- */
  function openRunModal(j) {
    integ.runJob = j;
    const d = new Date(Date.now() - 86400000 * (j.schedule_offset_days ?? 1)).toISOString().slice(0, 10);
    $("#runTitle").textContent = `Run ${j.name}`;
    $("#runIntro").textContent = j.direction === "pull" ? "Each day in the range becomes (or replaces) one daily report." : `Pushes ${j.dataset} for the selected days to ${j.connection_name}.`;
    $("#runFrom").value = d; $("#runTo").value = d; $("#runOut").innerHTML = ""; $("#runGo").disabled = false; $("#runModal").hidden = false;
  }
  $("#runCancel").addEventListener("click", () => { $("#runModal").hidden = true; });
  $("#runForm").addEventListener("submit", async e => {
    e.preventDefault(); const b = $("#runGo"); b.disabled = true; b.textContent = "Running…"; $("#runOut").innerHTML = "";
    try {
      const r = await api(`/api/sync/jobs/${integ.runJob.id}/run`, { method: "POST", json: { date_from: $("#runFrom").value, date_to: $("#runTo").value } });
      $("#runOut").innerHTML = `<div class="test-result ${r.status === "success" ? "ok" : "bad"}"><span class="run-status ${r.status}">${esc(r.status.toUpperCase())}</span> ${esc(r.message || "")}</div>${r.log.length ? `<div class="run-log">${esc(r.log.join("\n"))}</div>` : ""}${r.report_ids.length ? `<p><button class="btn sm" type="button" id="runOpen">Open ${r.report_ids.length === 1 ? "the report" : "first report"}</button></p>` : ""}`;
      const ob = $("#runOpen"); if (ob) ob.addEventListener("click", () => { $("#runModal").hidden = true; switchView("reports"); loadReports().then(() => openReport(r.report_ids[0])); });
      loadIntegrations();
    } catch (err) { $("#runOut").innerHTML = `<div class="form-error">${esc(err.message)}</div>`; }
    finally { b.disabled = false; b.textContent = "Run again"; }
  });

  boot();
})();

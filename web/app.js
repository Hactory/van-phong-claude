// Văn phòng Claude: vẽ mỗi dự án thành một phòng, mỗi phiên là một nhân viên ngồi bàn.
// Màu áo theo bản app (01, 02, 03… khi chạy nhiều bản app Claude song song).

const APP_COLOR = { "01": "xanh", "02": "tim", "03": "la", "04": "hong", "05": "vang", CX: "codex" };
// Phiên Codex mở từ app desktop (ChatGPT) có mã app "CX": cùng nhân vật nhưng tô đen trắng.
const isCodex = (s) => s.kind === "codex";
const appName = (a) => (a === "CX" ? "Codex" : `bản ${a}`);
const STATUS = {
  cho:  { label: "Chờ bạn",  head: "❓ Cần bạn phản hồi" },
  lam:  { label: "Đang làm", head: "⚙️ Đang làm" },
  xem:  { label: "Cần xem",  head: "👀 Xong, chờ bạn xem" },
  ranh: { label: "Rảnh",     head: "☕ Phiên mở, đang rảnh" },
  nghi: { label: "Đã đóng",  head: "🌙 Phiên đã đóng" },
};
const STATUS_KEYS = Object.keys(STATUS);
const PROPS = ["cay-1", "bang-trang", "may-cafe", "cay-2", "ke-sach", "cay-nuoc", "den", "sofa", "tu-ho-so", "may-chu"];
const DEFAULT_FILTER = { status: [], apps: [], projects: [], q: "", gio: 24, routine: "all", xong: "ke", hienAn: false };

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* bỏ qua */ } },
};

let state = null;
let openId = null;
let views = [];
let filter = { ...DEFAULT_FILTER };

function ago(ms) {
  if (!ms) return "";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "vừa xong";
  if (s < 3600) return `${Math.floor(s / 60)} phút trước`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ trước`;
  return `${Math.floor(s / 86400)} ngày trước`;
}

// Thời lượng gọn: 3n4h, 2h14m, 57m, 40s
function fmtDur(ms) {
  if (ms <= 0) return "0m";
  const m = Math.floor(ms / 60000);
  if (m < 1) return `${Math.floor(ms / 1000)}s`;
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  if (d > 0) return `${d}n${h}h`;
  if (h > 0) return `${h}h${String(mm).padStart(2, "0")}m`;
  return `${mm}m`;
}
const fmtTok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n || 0));
const pctTone = (p) => (p >= 80 ? "bad" : p >= 50 ? "warn" : "ok");

// Chép bằng execCommand trước (chạy đồng bộ ngay trong cú bấm, WKWebView và trình duyệt đều nhận);
// API clipboard để dự phòng, có hạn 1 giây vì ở cửa sổ không có tiêu điểm nó có thể treo mãi.
async function copyText(text) {
  const ta = Object.assign(document.createElement("textarea"), { value: text });
  ta.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* thử API mới */ }
  ta.remove();
  if (ok || !navigator.clipboard) return ok;
  const timeout = new Promise((res) => setTimeout(() => res(false), 1000));
  return Promise.race([navigator.clipboard.writeText(text).then(() => true, () => false), timeout]);
}

// Đồng hồ đếm ngược kiểu 46:05 (giờ thì 1:02:05) cho bảng chi tiết, nhảy từng giây.
function fmtClock(ms) {
  const t = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  const mmss = `${String(m).padStart(h ? 2 : 1, "0")}:${String(sec).padStart(2, "0")}`;
  return h ? `${h}:${mmss}` : mmss;
}
// Chip cache: HTML chỉ mang mốc hết hạn, chữ đếm ngược do đồng hồ 1 giây cập nhật (không vẽ lại bàn).
function cacheChip(cache, busy, big) {
  if (!cache) return "";
  const until = cache.start + cache.ttl * 1000;
  return `<span class="cache-chip${big ? " big" : ""}" data-until="${until}" data-busy="${busy ? 1 : 0}"></span>`;
}
function tickCache() {
  if (document.hidden) return;
  const now = Date.now();
  document.querySelectorAll(".cache-chip").forEach((el) => {
    const left = Number(el.dataset.until) - now, busy = el.dataset.busy === "1", big = el.classList.contains("big");
    let text, tone;
    if (busy) { text = big ? "đang chạy" : "🔥 đang chạy"; tone = "ok"; }
    else if (left <= 0) { text = big ? "nguội" : "❄ nguội"; tone = "cold"; }
    else { text = big ? fmtClock(left) : `🔥 ${fmtDur(left)}`; tone = left <= 300000 ? "bad" : left <= 900000 ? "warn" : "ok"; }
    if (el.textContent !== text) el.textContent = text;
    el.dataset.tone = tone;
  });
}
setInterval(tickCache, 1000);

function hash(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0; return Math.abs(h); }

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Van-Phong": "1" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok && !j.error) j.error = `HTTP ${r.status}`;
  return j;
}

function toast(msg, bad, action) {
  const t = $("#toast");
  t.innerHTML = esc(msg) + (action ? ` <button class="toast-btn">${esc(action.label)}</button>` : "");
  t.className = "toast show" + (bad ? " bad" : "");
  if (action) t.querySelector(".toast-btn").onclick = () => { t.className = "toast"; action.run(); };
  const hide = () => { toast.t = setTimeout(() => (t.className = "toast"), action ? 12000 : 3800); };
  clearTimeout(toast.t);
  hide();
  t.onmouseenter = () => clearTimeout(toast.t);  // đang rê chuột vào thì giữ, kịp bấm Hoàn tác
  t.onmouseleave = () => { clearTimeout(toast.t); hide(); };
}

// ---------- bộ lọc ----------

function normFilter(f) {
  const n = { ...DEFAULT_FILTER, ...(f || {}) };
  for (const k of ["status", "apps", "projects"]) n[k] = [...new Set(n[k] || [])].sort();
  n.gio = Number(n.gio) || 24;
  n.q = (n.q || "").trim();
  return n;
}

// Chỉ giữ phần khác mặc định: gọn để lưu, và để so hai bộ lọc có giống nhau không.
function slim(f) {
  const n = normFilter(f), out = {};
  for (const [k, v] of Object.entries(n)) {
    if (JSON.stringify(v) !== JSON.stringify(DEFAULT_FILTER[k])) out[k] = v;
  }
  return out;
}
const sameFilter = (a, b) => JSON.stringify(slim(a)) === JSON.stringify(slim(b));

function setFilter(f, { fetchNow } = {}) {
  const gioChanged = normFilter(f).gio !== filter.gio;
  filter = normFilter(f);
  const s = slim(filter);
  history.replaceState(null, "", Object.keys(s).length ? "#loc=" + encodeURIComponent(JSON.stringify(s)) : location.pathname);
  store.set("vp-loc", s);
  syncFilterControls();
  if (gioChanged || fetchNow) refresh(); else render();
}

function syncFilterControls() {
  if (document.activeElement !== $("#q")) $("#q").value = filter.q;
  $("#f-gio").value = String(filter.gio);
  $("#f-routine").value = filter.routine;
  $("#f-xong").value = filter.xong;
  $("#f-an").checked = filter.hienAn;
  $("#f-reset").hidden = !Object.keys(slim(filter)).length;
}

function matchQ(s) {
  if (!filter.q) return true;
  const hay = [s.title, s.project, s.projectName, s.detail, s.needs, s.email, s.org, s.branch, s.worktree, s.routine, s.kind]
    .join(" ").toLowerCase();
  return filter.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

// Lọc chung (không tính trạng thái và dấu): dùng cho cả bàn, kệ và ô đếm.
function baseMatch(s) {
  if (filter.apps.length && !filter.apps.includes(s.app)) return false;
  if (filter.projects.length && !filter.projects.includes(s.project)) return false;
  if (filter.routine === "bo" && s.routine) return false;
  if (filter.routine === "chi" && !s.routine) return false;
  return matchQ(s);
}

// Nơi một phiên hiện ra: "ban" (bàn), "ke" (kệ đã xong), hoặc null (không hiện).
function placeOf(s) {
  if (!baseMatch(s)) return null;
  if (s.mark === "an") return filter.hienAn ? "ban" : null;
  if (s.mark === "xong") {
    if (filter.xong === "an") return null;
    if (filter.xong === "chi") return "ban";
    return filter.status.length ? null : "ke";
  }
  if (filter.xong === "chi") return null;
  if (filter.status.length && !filter.status.includes(s.status)) return null;
  return "ban";
}

// ---------- vẽ văn phòng ----------

function sprite(s) {
  const pose = s.status === "cho" && !s.mark ? "hoi" : "lam";
  if (s.routine) return `assets/robot-${pose}.png`;
  const c = APP_COLOR[s.app];
  return `assets/nv-${["xanh", "tim", "la", "hong"].includes(c) ? c : "xanh"}-${pose}.png`;
}

function bubble(s) {
  if (s.mark === "xong") return `<div class="bubble b-xong">✓ Đã xong</div>`;
  if (s.mark === "an") return `<div class="bubble b-an">Đã ẩn</div>`;
  if (s.reply && s.reply.state === "dang-chay") return `<div class="bubble b-lam"><span class="dots"><i></i><i></i><i></i></span> Đang chạy tiếp lời bạn</div>`;
  if (s.status === "cho") return `<div class="bubble b-cho">❗ Chờ bạn</div>`;
  if (s.status === "lam") return `<div class="bubble b-lam"><span class="dots"><i></i><i></i><i></i></span>${s.activity ? " " + esc(s.activity) : ""}</div>`;
  if (s.status === "xem") return `<div class="bubble b-xem">✓ Xong, xem giúp</div>`;
  return "";
}

function quickActions(s) {
  if (s.mark) return `<div class="qa"><button data-mark="" title="Bỏ đánh dấu, đưa về bàn">↺</button></div>`;
  return `<div class="qa">
    <button data-mark="xong" title="Đánh dấu xong (xếp lên kệ)">✓</button>
    <button data-mark="an" title="Ẩn khỏi văn phòng">🙈</button></div>`;
}

function deskHTML(s, inPod) {
  const tip = [s.title, s.detail, s.needs].filter(Boolean).join(" · ");
  const cls = `desk st-${s.status}${s.routine ? " robot" : ""}${s.mark ? " mk-" + s.mark : ""}${s.parent ? " child" : ""}${isCodex(s) ? " codex" : ""}`;
  // Phiên con mà phiên cha không ngồi cùng phòng: ghi rõ là con của ai.
  const parentTitle = s.parent && !inPod ? sessionById(s.parent)?.title : "";
  const meta = [s.routine ? (isCodex(s) ? "⏰ Automation" : "🤖 Routine") : "", parentTitle ? `↳ con của ${parentTitle}` : "",
    s.subActive && !inPod ? `🛸 ${s.subActive} subagent` : "", ago(s.lastActivityAt)].filter(Boolean).join(" · ");
  return `
    <div class="${cls}" role="button" tabindex="0" data-id="${s.id}" title="${esc(tip)}">
      ${quickActions(s)}
      ${bubble(s)}
      <div class="worker">
        <span class="glow"></span>
        <img class="who" src="${sprite(s)}" alt="">
      </div>
      <div class="plate">
        <span class="chip c-${APP_COLOR[s.app] || "xanh"}">${esc(s.app)}</span>
        <span class="name">${s.parent && inPod ? "↳ " : ""}${esc(s.title)}</span>
      </div>
      <div class="meta">${esc(meta)}${s.live || s.status === "lam" ? " " + cacheChip(s.cache, s.status === "lam") : ""}</div>
    </div>`;
}

// Bàn nhỏ của subagent, ngồi ngay sau bàn phiên cha. Không đưa giờ vào HTML để hoạt ảnh không chạy lại.
function subsHTML(s) {
  const list = s.subs || [];
  if (!list.length) return "";
  const shown = list.slice(0, 4);
  return `<div class="subs" data-id="${s.id}" role="button" tabindex="0" title="Subagent của ${esc(s.title)}">
    ${shown.map((x) => `<div class="sub ${x.active ? "on" : "off"}" title="${esc([x.desc, x.type, x.model].filter(Boolean).join(" · "))}">
      <img src="assets/tro-ly.png" alt=""><span>${esc(x.desc)}</span><i>${x.active ? "" : "✓"}</i></div>`).join("")}
    ${list.length > shown.length ? `<div class="sub more">+${list.length - shown.length} subagent</div>` : ""}
  </div>`;
}

// Gom phiên cha + phiên con (cùng phòng) + subagent thành một nhóm; phiên lẻ là một bàn.
function buildUnits(desks) {
  const here = new Map(desks.map((s) => [s.id, s]));
  const rootOf = (s) => { let r = s; for (let i = 0; i < 10 && r.parent && here.has(r.parent); i++) r = here.get(r.parent); return r; };
  const groups = new Map();
  desks.forEach((s) => { const r = rootOf(s); if (!groups.has(r.id)) groups.set(r.id, []); groups.get(r.id).push(s); });
  const units = [];
  for (const [rootId, members] of groups) {
    const root = here.get(rootId);
    const ordered = [root, ...members.filter((m) => m !== root)];
    const cells = ordered.map((m) => deskHTML(m, ordered.length > 1 || (m.subs || []).length > 0) + subsHTML(m)).join("");
    const kids = ordered.length - 1, subsOn = ordered.reduce((n, m) => n + (m.subActive || 0), 0);
    const nCells = ordered.length + ordered.filter((m) => (m.subs || []).length).length;
    if (nCells === 1) { units.push({ key: rootId, html: deskHTML(root, false) }); continue; }
    const head = [kids ? `${kids} phiên con` : "", subsOn ? `${subsOn} subagent đang chạy` : ""].filter(Boolean).join(" · ") || "subagent vừa xong";
    units.push({ key: rootId, html: `<div class="pod${nCells > 3 ? " wide" : ""}" style="--cells:${Math.min(nCells, 3)}">
      <div class="pod-head" data-id="${rootId}" role="button" tabindex="0">👥 <b>${esc(root.title)}</b><span>${esc(head)}</span></div>
      <div class="pod-desks">${cells}</div></div>` });
  }
  return units;
}

function sessionById(id) {
  for (const p of state?.projects || []) for (const s of p.sessions) if (s.id === id) return s;
  return null;
}

function shelfHTML(list) {
  if (!list.length) return "";
  return `<div class="shelf-head">📦 Đã xong <b>${list.length}</b></div><div class="shelf-items">${list.map((s) => `
    <span class="mini${isCodex(s) ? " codex" : ""}" role="button" tabindex="0" data-id="${s.id}" title="${esc(s.title)}">
      <img src="${sprite(s)}" alt=""><span>${esc(s.title)}</span>
      <button data-mark="" title="Đưa về bàn">↺</button>
    </span>`).join("")}</div>`;
}

function roomHead(p, desks, shelf) {
  const n = (k) => desks.filter((s) => s.status === k && !s.mark).length;
  const pills = [["cho", "Chờ bạn"], ["lam", "Đang làm"], ["xem", "Cần xem"]]
    .filter(([k]) => n(k)).map(([k, t]) => `<span class="pill p-${k}">${n(k)} ${t}</span>`).join("");
  const closed = desks.filter((s) => s.status === "nghi" && !s.mark).length;
  const sweep = closed ? `<button class="pill p-sweep" data-sweep="${esc(p.key)}" title="Đánh dấu xong mọi bàn đã đóng trong phòng">🧹 Dọn ${closed} bàn đã đóng</button>` : "";
  return `<div class="sign"><span class="ico">${esc(p.bieu_tuong)}</span><span class="pname">${esc(p.ten)}</span></div>
          <div class="pills">${pills}<span class="pill p-all">${desks.length} bàn${shelf.length ? ` · ${shelf.length} xong` : ""}</span>${sweep}</div>`;
}

function patch(el, html) {
  if (el.dataset.h !== html) { el.innerHTML = html; el.dataset.h = html; }
}

function renderOffice() {
  const office = $("#office");
  const rooms = state.projects.map((p) => {
    const desks = [], shelf = [];
    p.sessions.forEach((s) => {
      s.projectName = p.ten;
      const at = placeOf(s);
      if (at === "ban") desks.push(s); else if (at === "ke") shelf.push(s);
    });
    return { p, desks, shelf };
  }).filter((r) => r.desks.length || r.shelf.length);
  let empty = office.querySelector(".empty");
  if (!rooms.length) {
    office.querySelectorAll(".room").forEach((r) => r.remove());
    if (!empty) office.insertAdjacentHTML("afterbegin", `<div class="empty"><img src="assets/tro-ly.png" alt=""><p>Không có bàn nào khớp bộ lọc.</p></div>`);
    return;
  }
  empty?.remove();
  const keep = new Set();
  const order = [];
  rooms.forEach(({ p, desks, shelf }) => {
    keep.add(p.key);
    let room = office.querySelector(`.room[data-key="${CSS.escape(p.key)}"]`);
    if (!room) {
      room = document.createElement("section");
      room.className = "room";
      room.dataset.key = p.key;
      const h = hash(p.key);
      const props = [PROPS[h % PROPS.length], PROPS[(h >> 3) % PROPS.length]];
      if (props[0] === props[1]) props[1] = PROPS[(h + 1) % PROPS.length];
      room.innerHTML = `<header class="wall"></header>
        <div class="floor ${h % 3 === 0 ? "carpet" : ""}">
          <img class="prop prop-l" src="assets/${props[0]}.png" alt="">
          <img class="prop prop-r" src="assets/${props[1]}.png" alt="">
          <div class="desks"></div>
        </div>
        <footer class="shelf"></footer>`;
    }
    if (!room.isConnected) columns()[0].appendChild(room);
    order.push(room);
    patch(room.querySelector(".wall"), roomHead(p, desks, shelf));
    patch(room.querySelector(".shelf"), shelfHTML(shelf));
    room.querySelector(".floor").hidden = !desks.length;
    room.classList.toggle("has-cho", desks.some((s) => s.status === "cho" && !s.mark));
    const box = room.querySelector(".desks");
    const units = buildUnits(desks);
    const keys = new Set(units.map((u) => u.key));
    [...box.children].forEach((el) => { if (!keys.has(el.dataset.unit)) el.remove(); });
    units.forEach((u, i) => {
      const html = u.html.trim();
      let el = [...box.children].find((c) => c.dataset.unit === u.key);
      if (!el || el.dataset.h !== html) {
        const tmp = document.createElement("div");
        tmp.innerHTML = html;
        const fresh = tmp.firstElementChild;
        fresh.dataset.h = html;
        fresh.dataset.unit = u.key;
        if (el) el.replaceWith(fresh); else box.appendChild(fresh);
        el = fresh;
      }
      if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
    });
    box.querySelectorAll(".desk").forEach((d) => d.classList.toggle("selected", d.dataset.id === openId));
  });
  [...office.querySelectorAll(".room")].forEach((r) => { if (!keep.has(r.dataset.key)) r.remove(); });
  layoutRooms(order);
  tickCache();
}

// Xếp phòng kiểu gạch xây: mỗi phòng vào cột đang thấp nhất, phòng ngắn không để lại khoảng trống.
const ROOM_MIN = 430, ROOM_GAP = 22;
function columns() {
  const office = $("#office");
  const n = Math.max(1, Math.floor((office.clientWidth + ROOM_GAP) / (ROOM_MIN + ROOM_GAP)));
  let cols = [...office.querySelectorAll(":scope > .col")];
  if (cols.length !== n) {
    const rooms = [...office.querySelectorAll(".room")];
    cols.forEach((c) => c.remove());
    cols = Array.from({ length: n }, () => office.appendChild(Object.assign(document.createElement("div"), { className: "col" })));
    rooms.forEach((r) => cols[0].appendChild(r));
  }
  return cols;
}

function layoutRooms(order) {
  const cols = columns();
  const heights = cols.map(() => 0), placed = cols.map(() => 0);
  for (const room of order) {
    const i = heights.indexOf(Math.min(...heights));
    const at = cols[i].children[placed[i]];
    if (at !== room) cols[i].insertBefore(room, at || null);  // chỉ dời khi sai chỗ để hoạt ảnh không chạy lại
    placed[i]++;
    heights[i] += room.offsetHeight + ROOM_GAP;
  }
}

let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => state && renderOffice(), 120); });

function allSessions() { return state ? state.projects.flatMap((p) => p.sessions) : []; }

function renderHeader() {
  const all = allSessions();
  const pool = all.filter((s) => baseMatch(s) && !s.mark);
  const count = (k) => pool.filter((s) => s.status === k).length;
  patch($("#kpis"), STATUS_KEYS.map((k) =>
    `<button class="kpi k-${k}${filter.status.includes(k) ? " on" : ""}" data-status="${k}" title="Chỉ xem: ${STATUS[k].label}">
      <b>${count(k)}</b><span>${STATUS[k].label}</span></button>`).join(""));
  patch($("#f-status"), STATUS_KEYS.map((k) =>
    `<button class="fchip s-${k}${filter.status.includes(k) ? " on" : ""}" data-status="${k}">${STATUS[k].label}</button>`).join(""));
  const byApp = {};
  all.forEach((s) => { (byApp[s.app] ||= []).push(s); });
  patch($("#apps"), Object.keys(byApp).sort().map((a) => {
    const who = a === "CX" ? "Codex trong app ChatGPT" : state.apps.filter((x) => x.app === a).map((x) => `${x.email || "?"} · ${x.org}`).join("\n");
    const cho = byApp[a].filter((s) => s.status === "cho" && !s.mark).length;
    const on = !filter.apps.length || filter.apps.includes(a);
    return `<button class="appchip c-${APP_COLOR[a] || "xanh"}${on ? "" : " off"}" data-app="${a}" title="${esc(who)}">
      <i></i>${a === "CX" ? "Codex" : `Bản ${a}`}<small>${byApp[a].length}${cho ? ` · ${cho} chờ` : ""}</small></button>`;
  }).join(""));
  renderProjectMenu();
  const marked = all.filter((s) => s.mark).length;
  $("#sub").textContent = `${all.length} phiên trong ${labelGio(state.hours)}${marked ? ` · ${marked} đã đánh dấu` : ""} · cập nhật ${new Date(state.at).toLocaleTimeString("vi-VN")}`;
  updateTitle();
}

// Tiêu đề "(N) Văn phòng Claude": app đọc nó để hiện chấm đỏ trên Dock.
function updateTitle() {
  const cho = allSessions().filter((s) => s.status === "cho" && !s.mark).length;
  document.title = (cho ? `(${cho}) ` : "") + "Văn phòng Claude";
}

function labelGio(h) {
  return { 1: "1 giờ qua", 6: "6 giờ qua", 24: "24 giờ qua", 72: "3 ngày qua", 168: "7 ngày qua", 336: "14 ngày qua" }[h] || `${h} giờ qua`;
}

function renderProjectMenu() {
  const projects = state.projects.map((p) => ({ key: p.key, ten: p.ten, ico: p.bieu_tuong, n: p.sessions.length }));
  for (const k of filter.projects) if (!projects.some((p) => p.key === k)) projects.push({ key: k, ten: k, ico: "📁", n: 0 });
  const sel = filter.projects;
  $("#dd-btn").textContent = sel.length === 0 ? "Dự án: tất cả" : sel.length === 1
    ? `Dự án: ${projects.find((p) => p.key === sel[0])?.ten || sel[0]}` : `Dự án: ${sel.length} phòng`;
  $("#dd-btn").classList.toggle("on", sel.length > 0);
  const html = projects.map((p) => `<label><input type="checkbox" value="${esc(p.key)}" ${sel.includes(p.key) ? "checked" : ""}>
    <span>${esc(p.ico)} ${esc(p.ten)}</span><small>${p.n}</small></label>`).join("") +
    `<button class="link" data-proj-all>Chọn tất cả</button>`;
  const menu = $("#dd-menu");
  if (menu.dataset.h !== html) { menu.innerHTML = html; menu.dataset.h = html; }
}

// ---------- màn hình đã lưu ----------

function renderViews() {
  const chips = views.map((v) => {
    const on = sameFilter(v.loc, filter);
    return `<span class="view${on ? " on" : ""}" data-view="${esc(v.id)}">
      <button class="view-go" data-view-go="${esc(v.id)}">${v.macDinh ? "★ " : ""}${esc(v.ten)}</button>
      <button class="view-more" data-view-menu="${esc(v.id)}" aria-label="Tuỳ chọn màn hình ${esc(v.ten)}">⋯</button>
    </span>`;
  }).join("");
  // Chỉ vẽ lại phần nút khi có thay đổi; ô đặt tên / menu nằm ngoài nên không bị xoá khi đang gõ.
  patch($("#views-list"), `${chips}<button class="view-add" id="view-add">＋ Lưu màn hình này</button>`);
}

async function saveViews() {
  const r = await api("/api/views", { views });
  if (!r.ok) toast(r.error || "Không lưu được màn hình", true);
  renderViews();
}

// Ô nhập tên ngay dưới nút bấm (không dùng prompt() vì cửa sổ app riêng không hiện hộp đó).
function askName(anchor, defaultName, onDone) {
  const menu = $("#view-menu");
  menu.innerHTML = `<form class="name-form"><input maxlength="40" placeholder="Tên màn hình, ví dụ: Dự án A hôm nay" value="${esc(defaultName || "")}">
    <button class="btn primary">Lưu</button></form>`;
  placeMenu(menu, anchor);
  const input = menu.querySelector("input");
  input.focus();
  input.select();
  menu.onclick = (e) => e.stopPropagation();
  input.onkeydown = (e) => { if (e.key === "Escape") menu.hidden = true; };
  menu.querySelector("form").onsubmit = (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    menu.hidden = true;
    onDone(name);
  };
}

function placeMenu(menu, anchor) {
  const r = anchor.getBoundingClientRect(), host = $("#views").getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(r.left - host.left, host.width - 300))}px`;
  menu.style.top = `${r.bottom - host.top + 4}px`;
  menu.hidden = false;
}

function viewMenu(id, anchor) {
  const v = views.find((x) => x.id === id);
  const menu = $("#view-menu");
  menu.innerHTML = `
    <button data-vm="update">Cập nhật theo bộ lọc đang xem</button>
    <button data-vm="rename">Đổi tên</button>
    <button data-vm="default">${v.macDinh ? "Bỏ mặc định" : "Mở sẵn khi vào văn phòng"}</button>
    <button data-vm="delete" class="danger">Xoá màn hình</button>`;
  placeMenu(menu, anchor);
  menu.onclick = async (e) => {
    e.stopPropagation();
    const act = e.target.closest("[data-vm]")?.dataset.vm;
    if (!act) return;
    menu.hidden = true;
    if (act === "update") { v.loc = slim(filter); toast(`Đã cập nhật "${v.ten}"`); }
    if (act === "rename") { askName(anchor, v.ten, (n) => { v.ten = n; saveViews(); }); return; }
    if (act === "default") { const on = !v.macDinh; views.forEach((x) => delete x.macDinh); if (on) v.macDinh = true; }
    if (act === "delete") {
      const i = views.indexOf(v);
      views.splice(i, 1);
      toast(`Đã xoá "${v.ten}"`, false, { label: "Hoàn tác", run: () => { views.splice(i, 0, v); saveViews(); } });
    }
    saveViews();
  };
}

// ---------- đánh dấu xong / ẩn ----------

async function mark(ids, kind, { silent } = {}) {
  const before = new Map(allSessions().filter((s) => ids.includes(s.id)).map((s) => [s.id, s.mark || null]));
  allSessions().forEach((s) => { if (ids.includes(s.id)) s.mark = kind || null; });
  render();
  const r = await api("/api/mark", { ids, kind: kind || null });
  if (!r.ok) { toast(r.error || "Không đánh dấu được", true); refresh(); return; }
  if (openId && ids.includes(openId)) openDetail(openId, { quiet: true });
  if (silent) return;
  const what = kind === "xong" ? "Đã xếp lên kệ Đã xong" : kind === "an" ? "Đã ẩn khỏi văn phòng" : "Đã đưa về bàn";
  toast(`${what}${ids.length > 1 ? ` (${ids.length} phiên)` : ""}`, false, {
    label: "Hoàn tác",
    run: async () => {
      const groups = {};
      before.forEach((k, id) => { (groups[k ?? ""] ||= []).push(id); });
      for (const [k, list] of Object.entries(groups)) await mark(list, k || null, { silent: true });
      toast("Đã hoàn tác");
    },
  });
}

// ---------- bảng chi tiết ----------

function linkify(s) {
  return esc(s).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

function summaryHTML(d) {
  const t = d.aiSummary;
  if (t && !t.error) {
    const dec = (t.decisions || []).filter((x) => String(x).trim());
    return `<div class="sum-title">${esc(t.title || d.title)}</div>
      <p>${linkify(t.summary || "")}</p>
      ${dec.length ? `<div class="decide"><b>Cần bạn quyết:</b><ol>${dec.map((x) => `<li>${linkify(x)}</li>`).join("")}</ol></div>` : ""}
      <div class="via">Tóm tắt bởi ${esc(t.via || "bộ tóm tắt")} ·
        <button class="link" data-act="resum">tóm tắt lại</button></div>`;
  }
  const a = d.appSummary || {};
  const err = t && t.error ? `<p class="err">${esc(t.error)}</p>` : "";
  const base = a.status_detail || a.needs_action
    ? `<p>${linkify(a.status_detail || "")}</p>${a.needs_action ? `<div class="decide"><b>Cần bạn làm:</b><p>${linkify(a.needs_action)}</p></div>` : ""}<div class="via">Tóm tắt nhanh của app Claude</div>`
    : `<p class="muted">Chưa có tóm tắt.</p>`;
  const btn = d.status === "lam" || !d.canSummarize ? "" : `<button class="btn ghost" data-act="sum">✨ Tóm tắt</button>`;
  return base + err + btn;
}

// Phiên đang chờ bạn: hộp hỏi đang mở trong app, hoặc câu hỏi Claude để lại ở hộp "bước tiếp" cuối lượt.
// Bấm một lựa chọn chỉ điền sẵn câu trả lời vào ô bên dưới, chưa gửi; nút việc của app (merge, commit…) phải bấm trong app.
function askHTML(d) {
  if (d.waiting) return `<div class="decide ask"><b>${esc(d.waiting)}</b><p>Có thể là xin quyền, câu hỏi hoặc thẻ mở phiên mới. Bấm "Mở trong app" để trả lời.</p></div>`;
  if (!d.ask) return "";
  const opts = (d.askActions || []).map((a) => a.app
    ? `<span class="opt app" title="Nút của app, bấm trong app">${esc(a.label)}</span>`
    : `<button class="opt" data-act="ask-fill" data-msg="${esc(a.message || a.label)}" title="${esc(a.message || a.label)}">${esc(a.label)}</button>`).join("");
  return `<div class="decide ask"><b>Claude hỏi:</b><p>${linkify(d.ask)}</p>${opts ? `<div class="opts">${opts}</div>` : ""}</div>`;
}

// Nút đánh dấu chỉ là biểu tượng, nằm góc trên cạnh nút đóng, xa nút "Mở trong app" để khỏi bấm nhầm.
function markTools(d) {
  if (d.mark) return `<button class="tool" data-act="unmark" title="Bỏ đánh dấu, đưa về bàn" aria-label="Bỏ đánh dấu">↺</button>`;
  return `<button class="tool ok" data-act="xong" title="Đánh dấu xong (xếp lên kệ)" aria-label="Đánh dấu xong">✓</button>
    <button class="tool hide" data-act="an" title="Ẩn khỏi văn phòng" aria-label="Ẩn khỏi văn phòng">🙈</button>`;
}

function detailHTML(d) {
  const st = STATUS[d.status] || STATUS.nghi;
  const where = [d.projectName, d.branch || d.worktree].filter(Boolean).join(" · ");
  const reply = d.reply ? `<div class="reply-state rs-${d.reply.state}">${
    { "da-gui": "✓ Đã gửi vào phiên đang mở", "dang-chay": "⏳ Đang chạy tiếp lời bạn bằng CLI…", "da-xong": "✓ Phiên đã chạy xong lời bạn", "loi": "✕ Gửi lỗi: " + esc(d.reply.error || "") }[d.reply.state] || ""
  } <small>${esc(ago(d.reply.at))}</small></div>` : "";
  const timeline = (d.timeline || []).slice().reverse().map((x) =>
    `<li class="tl-${x.kind}"><span>${x.kind === "tool" ? "🔧" : x.kind === "user" ? "🧑" : "💬"}</span><p>${esc(x.text)}</p></li>`).join("");
  const markNote = d.mark === "xong" ? " · 📦 đã đánh dấu xong" : d.mark === "an" ? " · 🙈 đang ẩn" : "";
  return `
    <div class="head-tools">${markTools(d)}</div>
    <div class="card-head st-${d.status}${isCodex(d) ? " codex" : ""}">
      <img src="${sprite(d)}" alt="">
      <div><div class="state">${st.head}${markNote}</div><h2>${esc(d.title)}</h2></div>
    </div>
    <ul class="facts">
      ${isCodex(d) ? `<li>🤖 <span class="chip c-codex">Codex</span> trong app ChatGPT</li>` : `
      <li>👤 ${esc(d.email || "?")} · <span class="chip c-${APP_COLOR[d.app] || "xanh"}">app bản ${esc(d.app)}</span></li>
      <li>🏢 <span class="team t-${teamKey(d.org)}">${esc(d.org)}</span>${d.plan ? ` <small class="muted">${esc(d.plan)}</small>` : ""}
        ${d.org.startsWith("Team ") ? `<button class="link" data-act="name-org" data-org="${esc(d.orgUuid)}">đặt tên</button>` : ""}</li>`}
      <li>🆔 <code>${esc(d.cli || "")}</code></li>
      <li>📁 ${esc(where)}</li>
      ${d.routine ? `<li>${isCodex(d) ? "⏰ Automation" : "🤖 Routine"} <code>${esc(d.routine)}</code></li>` : ""}
      ${d.parent ? `<li>⬆️ Phiên cha: <button class="link" data-goto="${esc(d.parent.id)}">${esc(d.parent.title || d.parent.id)}</button></li>` : ""}
      <li>🕒 ${esc(ago(d.lastActivityAt))}${d.model ? ` · ${esc(d.model)}` : ""}</li>
      ${(d.prs || []).map((u) => `<li>🔗 <a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace("https://github.com/", ""))}</a></li>`).join("")}
    </ul>
    ${isCodex(d) ? codexMetricsHTML(d) : metricsHTML(d)}
    ${toolRowHTML(d)}
    ${askHTML(d)}
    <section class="sum" id="sum">${summaryHTML(d)}</section>
    <section class="actions">
      <button class="btn primary wide" data-act="open">🖥 Mở trong app ${isCodex(d) ? "Codex" : `Claude bản ${esc(d.app)}`}</button>
    </section>
    <section class="reply">
      <label for="reply-text">Trả lời phiên</label>
      <textarea id="reply-text" rows="3" placeholder="Gõ câu trả lời, ⌘ + Enter để gửi"></textarea>
      <div class="reply-row">
        <small>${isCodex(d) ? "App Codex giữ phiên này: văn phòng chép sẵn câu trả lời rồi mở đúng phiên, bấm ⌘V rồi Enter."
          : d.live ? "Phiên đang mở: tin vào thẳng cửa sổ phiên." : "Phiên đã đóng: chạy tiếp bằng CLI, app chỉ hiện sau khi bản đó khởi động lại."}</small>
        <button class="btn primary" data-act="reply">${isCodex(d) ? "Chép & mở" : "Gửi"}</button>
      </div>
      ${reply}
    </section>
    ${familyHTML(d)}
    ${d.lastText ? `<details class="last" ${d.status === "cho" ? "open" : ""}><summary>Lời ${isCodex(d) ? "Codex" : "Claude"} cuối</summary><blockquote>${linkify(d.lastText)}</blockquote></details>` : ""}
    ${timeline ? `<details class="tl"><summary>Hoạt động gần đây</summary><ul>${timeline}</ul></details>` : ""}`;
}

// Codex chạy bằng gói ChatGPT: không quy ra tiền (chưa có bảng giá chuẩn), chỉ báo token và lượt.
function codexMetricsHTML(d) {
  const c = d.codex || {};
  const turnSt = { completed: "xong", inProgress: "đang chạy", failed: "lỗi", interrupted: "bị dừng" }[c.turnStatus] || c.turnStatus || "";
  return `<section class="metrics">
    <div class="m"><span>🔢 Token đã dùng</span><b>${fmtTok(c.tokens || 0)}</b><small>gói ChatGPT, không tính tiền theo token</small></div>
    <div class="m"><span>🔁 Lượt</span><b>${c.turns || 0}</b><small>lượt cuối ${esc(turnSt)}${c.turnMs ? ` sau ${fmtDur(c.turnMs)}` : ""}</small></div>
    <div class="m"><span>📬 Trong app</span><b class="${c.unread ? "t-warn" : "t-ok"}">${c.unread ? "Chưa đọc" : "Đã đọc"}</b>
      <small>${d.live ? "app ChatGPT đang mở" : "app ChatGPT đang tắt"}</small></div>
  </section>`;
}

function metricsHTML(d) {
  const u = d.usage;
  if (!u) return "";
  const pct = u.moc ? Math.round((u.ctx / u.moc) * 100) : 0;
  const unpriced = (u.unpriced || []).length ? `<small class="err">Chưa có giá: ${esc(u.unpriced.join(", "))}</small>` : "";
  return `<section class="metrics">
    <div class="m"><span>💰 Phiên</span><b>$${u.total.toFixed(2)}</b>
      <small>${u.subCost ? `gồm subagent $${u.subCost.toFixed(2)} · ` : ""}${u.calls} lượt gọi · giá API như Claude Code</small>${unpriced}</div>
    <div class="m"><span>🧠 Ngữ cảnh</span><b>${fmtTok(u.ctx)} <em class="t-${pctTone(pct)}">${pct}%</em></b>
      <div class="meter"><i class="t-${pctTone(pct)}" style="width:${Math.min(pct, 100)}%"></i></div>
      <small>${u.mocCompact ? `mốc tự tóm tắt ${fmtTok(u.mocCompact)} · ` : "so với "}cửa sổ ${fmtTok(u.window)}</small></div>
    <div class="m"><span>⏱ Cache ấm còn</span><b>${cacheChip(u.cache, d.status === "lam", true) || "chưa rõ"}</b>
      <small>${u.cache ? `TTL ${u.cache.ttl >= 3600 ? "1 giờ" : "5 phút"} tính từ lượt gọi cuối` : ""}</small></div>
  </section>`;
}

let compactArmed = null;
let current = null;  // hồ sơ phiên đang mở trong bảng chi tiết  // bấm compact lần đầu để "lên cò", lần hai mới chạy (phiên đã đóng)

function toolRowHTML(d) {
  const armed = compactArmed && compactArmed.id === d.id && compactArmed.until > Date.now();
  const c = d.compact;
  const cState = c ? `<div class="reply-state rs-${c.state}">${
    { "dang-chay": "⏳ Đang compact bằng CLI…", "da-xong": "✓ Compact xong", "loi": "✕ Compact lỗi" }[c.state] || ""}
    ${c.text ? `<small>${esc(c.text)}</small>` : ""}</div>` : "";
  return `<div class="tool-row">
    <button class="btn" data-act="copy-id" title="${esc(d.cli || "")}">🆔 Copy ID</button>
    <button class="btn" data-act="handoff" title="Chép bản bàn giao: phiên, nick, dự án, tóm tắt, yêu cầu cuối, lời Claude cuối">📋 Copy handoff</button>
    ${isCodex(d) ? "" : `<button class="btn${armed ? " armed" : ""}" data-act="compact" ${d.status === "lam" || (c && c.state === "dang-chay") ? "disabled" : ""}
      title="${d.live ? "Phiên đang mở: chép sẵn /compact rồi mở phiên trong app" : "Phiên đã đóng: compact bằng CLI"}">
      🗜 ${armed ? "Bấm lần nữa để compact" : "Compact"}</button>`}
  </div>${cState}`;
}

function handoffText(d) {
  const t = d.aiSummary && !d.aiSummary.error ? d.aiSummary : null;
  const a = d.appSummary || {};
  const lines = [
    `# Bàn giao phiên: ${d.title}`, "",
    ...(isCodex(d) ? [
      `- Mã phiên Codex: ${d.cli} (app Codex trong ChatGPT, mở bằng codex://threads/${d.cli})`,
      `- Mở lại bằng CLI: cd ${d.cwd} && codex resume ${d.cli}`,
    ] : [
      `- Mã phiên: ${d.cli} (app bản ${d.app}, ${d.local || d.id})`,
      `- Mở lại bằng CLI: cd ${d.cwd} && claude --resume ${d.cli}`,
      `- Nick: ${d.email || "?"} · ${d.org}${d.plan ? ` (${d.plan})` : ""}`,
    ]),
    `- Dự án: ${[d.projectName, d.branch || d.worktree].filter(Boolean).join(" · ")}`,
    `- Thư mục: ${d.cwd}`,
    `- Trạng thái: ${(STATUS[d.status] || STATUS.nghi).label}${d.usage ? ` · ngữ cảnh ${fmtTok(d.usage.ctx)} · $${d.usage.total.toFixed(2)}` : ""}${d.codex ? ` · ${fmtTok(d.codex.tokens)} token` : ""}`,
    ...(d.prs || []).map((u) => `- PR: ${u}`),
    "", "## Tóm tắt",
    t ? t.summary : [a.status_detail, a.needs_action && `Cần làm: ${a.needs_action}`].filter(Boolean).join("\n") || "(chưa có)",
  ];
  const dec = t ? (t.decisions || []).filter(Boolean) : [];
  if (dec.length) lines.push("", "## Cần quyết", ...dec.map((x, i) => `${i + 1}. ${x}`));
  if (d.lastUser) lines.push("", "## Yêu cầu gần nhất", d.lastUser.trim());
  if (d.lastText) lines.push("", `## Lời ${isCodex(d) ? "Codex" : "Claude"} cuối`, d.lastText.trim());
  return lines.join("\n");
}

function familyHTML(d) {
  const kids = (d.children || []).filter((c) => !c.archived);
  const subs = d.subs || [];
  if (!kids.length && !subs.length) return "";
  const kidList = kids.map((c) => {
    const live = sessionById(c.id);
    const st = live ? (live.mark ? (live.mark === "xong" ? "đã xong" : "đã ẩn") : STATUS[live.status].label) : "cũ hơn khung giờ";
    return `<li><button class="link" data-goto="${esc(c.id)}">${esc(c.title || c.id)}</button><small>${esc(st)}</small></li>`;
  }).join("");
  const subList = subs.map((x) => `<li class="${x.active ? "on" : ""}"><span>${x.active ? "🛸" : "✓"}</span>
      <p>${esc(x.desc)}<small>${esc([x.type, x.model, x.active ? "đang chạy" : "xong", ago(x.at)].filter(Boolean).join(" · "))}</small></p></li>`).join("");
  return `<details class="family" open><summary>Nhóm làm việc: ${kids.length} phiên con · ${d.subActive}/${d.subTotal} subagent đang chạy</summary>
    ${kids.length ? `<h4>Phiên con</h4><ul class="kids">${kidList}</ul>` : ""}
    ${subs.length ? `<h4>Subagent</h4><ul class="sublist">${subList}</ul>` : ""}</details>`;
}

// HTML bảng chi tiết openDetail vẽ lần cuối; chỗ nào khác sửa vào bảng thì xoá để lần sau vẽ lại đủ.
let detailShown = "";
async function openDetail(id, { quiet } = {}) {
  openId = id;
  document.querySelectorAll(".desk.selected").forEach((e) => e.classList.remove("selected"));
  document.querySelector(`.desk[data-id="${id}"]`)?.classList.add("selected");
  const drawer = $("#drawer");
  if (!quiet) {
    detailShown = "";
    $("#detail").innerHTML = `<div class="loading"><img src="assets/tro-ly.png" alt=""> Đang mở hồ sơ…</div>`;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $("#scrim").classList.add("show");
  }
  const d = await api(`/api/session/${id}`);
  if (openId !== id) return;
  if (d.error) { detailShown = ""; $("#detail").innerHTML = `<p class="err">${esc(d.error)}</p>`; return; }
  const draft = $("#reply-text")?.value || "";
  const focused = document.activeElement?.id === "reply-text";
  const openState = new Map([...$("#detail").querySelectorAll("details")].map((x) => [x.className, x.open]));
  current = d;
  const html = detailHTML(d);
  if (quiet && html === detailShown) return;   // không đổi gì so với lần vẽ trước: giữ nguyên bảng
  $("#detail").innerHTML = html;
  detailShown = html;
  if (quiet) $("#detail").querySelectorAll("details").forEach((x) => { if (openState.has(x.className)) x.open = openState.get(x.className); });
  tickCache();
  if (draft) $("#reply-text").value = draft;
  if (focused) $("#reply-text").focus();
  // Bấm vào là có tóm tắt (nếu đã khai bộ tóm tắt; đệm theo lời cuối, gọi lại không tốn thêm).
  if (!quiet && !d.aiSummary && d.status !== "lam" && d.canSummarize) summarize(id, false);
}

async function summarize(id, force) {
  detailShown = "";
  const box = $("#sum");
  if (box) box.insertAdjacentHTML("beforeend", `<div class="loading small"><span class="dots"><i></i><i></i><i></i></span> đang tóm tắt…</div>`);
  const d = await api(`/api/session/${id}/summary`, { force });
  if (openId !== id || !$("#sum")) return;
  if (d.error) { toast(d.error, true); return; }
  detailShown = "";
  $("#sum").innerHTML = summaryHTML(d);
}

function closeDetail() {
  openId = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("show");
  document.querySelectorAll(".desk.selected").forEach((e) => e.classList.remove("selected"));
}

async function sendReply() {
  const box = $("#reply-text");
  const text = box.value.trim();
  if (!text || !openId) return;
  if (current && isCodex(current)) {
    const ok = await copyText(text);
    await openInApp();
    toast(ok ? "Đã chép câu trả lời và mở phiên trong app Codex: bấm ⌘V rồi Enter" : "Đã mở phiên trong app Codex, chưa chép được chữ", !ok);
    if (ok) box.value = "";
    return;
  }
  const btn = document.querySelector('[data-act="reply"]');
  btn.disabled = true;
  const r = await api(`/api/session/${openId}/reply`, { text });
  btn.disabled = false;
  if (!r.ok) { toast(r.error || "Gửi không được", true); return; }
  box.value = "";
  toast(r.via === "live" ? "Đã gửi vào phiên đang mở" : "Đang chạy tiếp phiên bằng CLI");
  openDetail(openId, { quiet: true });
}

async function doCompact(d) {
  if (d.live) {
    const ok = await copyText("/compact");
    await openInApp();
    toast(ok ? `Đã chép /compact và mở phiên ở app bản ${d.app}: bấm ⌘V rồi Enter`
             : `Đã mở phiên ở app bản ${d.app}: gõ /compact rồi Enter`);
    return;
  }
  if (!(compactArmed && compactArmed.id === d.id && compactArmed.until > Date.now())) {
    compactArmed = { id: d.id, until: Date.now() + 5000 };
    detailShown = "";
    $("#detail").innerHTML = detailHTML(d);
    setTimeout(() => { if (current && current.id === d.id) { detailShown = ""; $("#detail").innerHTML = detailHTML(current); tickCache(); } }, 5100);
    return;
  }
  compactArmed = null;
  const r = await api(`/api/session/${d.id}/compact`, {});
  if (r.live) { d.live = true; return doCompact(d); }
  toast(r.ok ? "Đang compact phiên bằng CLI (app chỉ thấy sau khi bản đó khởi động lại)" : (r.error || "Không compact được"), !r.ok);
  openDetail(d.id, { quiet: true });
}

function nameOrg(org, anchor) {
  const li = anchor.closest("li");
  detailShown = "";
  li.innerHTML = `<form class="name-form inline"><input maxlength="40" placeholder="Tên team, ví dụ: Công ty A"><button class="btn primary">Lưu</button></form>`;
  const input = li.querySelector("input");
  input.focus();
  li.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    const r = await api("/api/org-name", { org, name });
    toast(r.ok ? `Đã đặt tên team: ${name}` : (r.error || "Không lưu được"), !r.ok);
    refresh();
    openDetail(openId, { quiet: true });
  };
}

// Màu chip Team: khai trong cấu hình mục "mauTeam" ({"Tên team": "tim"}), không khai thì chọn theo tên.
const TEAM_PALETTE = ["tim", "cam", "xanh", "hong", "vang"];
function teamKey(name) {
  const fixed = state?.teamMau?.[name];
  if (fixed) return fixed;
  if (/cá nhân/i.test(name || "")) return "canhan";
  if (!name || name.startsWith("Team ") || name === "?") return "khac";
  return TEAM_PALETTE[hash(name) % TEAM_PALETTE.length];
}

async function openInApp() {
  const btn = document.querySelector('[data-act="open"]');
  detailShown = "";
  btn.disabled = true;
  btn.textContent = "Đang mở app…";
  const r = await api(`/api/session/${openId}/open`, {});
  btn.disabled = false;
  const where = r.app === "Codex" ? "app Codex" : `app Claude bản ${r.app || ""}`;
  btn.textContent = `🖥 Mở trong ${where}`;
  detailShown = "";
  if (!(current && isCodex(current) && r.ok)) toast(r.ok ? `Đã mở ở ${where}` : (r.error || "Không mở được"), !r.ok);
}

// ---------- tab Quota: mọi nick trong kho và ngoài kho ----------

let tab = "office";
let quota = null;

function setTab(t) {
  tab = t;
  document.body.dataset.tab = t;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  store.set("vp-tab", t);
  if (t === "quota") loadQuota(false);
}

async function loadQuota(force) {
  const btn = $("#q-refresh");
  if (btn) btn.disabled = true;
  const d = await api(`/api/quota${force ? "?moi=1" : ""}`);
  if (btn) btn.disabled = false;
  if (d.error) { $("#quota").innerHTML = `<p class="err">${esc(d.error)}</p>`; return; }
  quota = d;
  renderQuota();
}

function ring(label, w, maxPct) {
  if (!w) return `<div class="ring na"><svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="26"/></svg><b>—</b><span>${label}</span><small>chưa có số</small></div>`;
  const pct = Math.round(w.pct), C = 2 * Math.PI * 26;
  const tone = pct >= maxPct ? "bad" : pct >= 50 ? "warn" : "ok";
  const reset = w.resetsAt ? `↻ ${fmtDur(w.resetsAt * 1000 - Date.now())}` : "";
  return `<div class="ring t-${tone}"><svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="26"/>
    <circle class="val" cx="32" cy="32" r="26" stroke-dasharray="${(C * Math.min(pct, 100)) / 100} ${C}"/></svg>
    <b>${pct}%</b><span>${label}</span><small>${reset}</small></div>`;
}

const appNum = (p) => (p.locations.map((l) => l.label.match(/App Claude (\d\d)/)?.[1]).filter(Boolean).sort()[0] || "99");
const khoLabel = (p) => p.locations.filter((l) => l.kind === "kho").map((l) => l.label).join(", ") || "Trong kho";

function poolCard(p, maxPct, opts = {}) {
  const locs = p.locations.map((l) => {
    const m = l.label.match(/App Claude (\d\d)/);
    const cls = m ? `c-${APP_COLOR[m[1]] || "xanh"} loc-app` : l.kind === "kho" ? "loc-kho" : "loc-cli";
    return `<span class="loc ${cls}" title="${esc(l.detail || "")}">${esc(m ? `App ${m[1]}` : l.label)}${l.detail ? " ⚠︎" : ""}</span>`;
  }).join("");
  const scoped = p.scoped.map((x) => {
    const pct = Math.round(x.pct), tone = pct >= maxPct ? "bad" : pct >= 50 ? "warn" : "ok";
    return `<div class="sc"><span>${esc(x.name)}</span><div class="meter"><i class="t-${tone}" style="width:${Math.min(pct, 100)}%"></i></div>
      <b class="t-${tone}">${pct}%</b><small>${x.resetsAt ? "↻ " + fmtDur(x.resetsAt * 1000 - Date.now()) : ""}</small></div>`;
  }).join("");
  const stale = p.ageSec != null && p.ageSec > 1800;
  const old = isOld(p);   // số quá cũ thì không dám nói "dùng được"
  const full = !old && coreMax(p) >= maxPct;
  // Quota riêng một model (Fable) đầy thì chỉ model đó dừng; Opus, Sonnet vẫn chạy bằng quota 5 giờ/Tuần.
  const scopedFull = p.scoped.filter((x) => x.pct >= maxPct).map((x) => x.name);
  const note = p.locations.map((l) => l.detail).filter(Boolean)[0] || p.status || "";
  const initials = p.email.split("@")[0].slice(0, 2).toUpperCase();
  const verdict = full ? ["bad", "Gần chạm trần"]
    : p.maxPct == null ? ["na", "Chưa đo được"]
    : old ? ["na", "Số cũ"]
    : scopedFull.length ? ["warn", `Hết ${scopedFull.join(", ")}`]
    : ["ok", "Dùng được"];
  return `<article class="qcard${full ? " full" : ""}${p.maxPct == null || old ? " nodata" : ""}">
    <header>
      <span class="avatar t-${teamKey(p.team)}">${esc(initials)}</span>
      <div class="who"><b title="${esc(p.email)}">${esc(p.email)}</b>
        <div><span class="team t-${teamKey(p.team)}">${esc(p.team)}</span>${p.plan ? `<small class="muted"> ${esc(p.plan)}</small>` : ""}
          ${opts.markKho && p.inKho ? `<span class="inkho" title="Nick này cũng nằm trong kho claude-swap">🗄 ${esc(khoLabel(p))}</span>` : ""}</div></div>
      <span class="verdict ${verdict[0]}"${p.maxPct == null ? ` title="Không hỏi được số quota lúc này; nick vẫn dùng bình thường"`
        : scopedFull.length ? ` title="Chỉ ${esc(scopedFull.join(", "))} phải chờ hồi; Opus, Sonnet vẫn chạy bằng quota 5 giờ/Tuần"` : ""}>${verdict[1]}</span>
    </header>
    <div class="rings">${ring("5 giờ", p.fiveHour, maxPct)}${ring("Tuần", p.sevenDay, maxPct)}</div>
    ${scoped ? `<div class="scoped">${scoped}</div>` : ""}
    <footer><div class="locs">${locs}</div>
      <small class="${stale ? "stale" : "muted"}">${p.ageSec == null ? "chưa đo" : `đo ${fmtDur(p.ageSec * 1000)} trước${stale ? " (cũ)" : ""}`} · ${esc(p.source || "")}</small>
      ${note ? `<small class="muted note">${esc(note)}</small>` : ""}</footer>
  </article>`;
}

const isOld = (p) => p.ageSec != null && p.ageSec > 3 * 3600;
const coreMax = (p) => Math.max(p.fiveHour?.pct ?? -1, p.sevenDay?.pct ?? -1);

function renderQuota() {
  if (!quota) return;
  const maxPct = quota.maxPct || 80;
  const kho = quota.pools.filter((p) => p.inKho);
  // Nick đang đăng nhập trong từng bản app (có thể đồng thời nằm trong kho: thẻ gắn thêm dấu 🗄).
  const inApp = quota.pools.filter((p) => p.locations.some((l) => l.kind === "app")).sort((a, b) => appNum(a).localeCompare(appNum(b)));
  const cliOnly = quota.pools.filter((p) => !p.inKho && !inApp.includes(p));
  const idle = (quota.appsIdle || []).map((x) => x.replace("App Claude ", "App "));
  // "Chưa đo được" = Anthropic tạm chặn lượt HỎI quota, không phải nick hết quota: vẫn tính là dùng được.
  const unknown = kho.filter((p) => p.maxPct == null || isOld(p));
  const usable = kho.filter((p) => !unknown.includes(p) && coreMax(p) < maxPct);
  const fableOk = (p) => !p.scoped.some((x) => x.pct >= maxPct);
  const best = usable.slice().sort((a, b) => (fableOk(b) - fableOk(a)) || ((a.fiveHour?.pct ?? 0) - (b.fiveHour?.pct ?? 0)))[0];
  const byPct = (a, b) => (b.maxPct ?? -1) - (a.maxPct ?? -1);
  $("#quota").innerHTML = `
    <div class="qsum">
      <div class="qbig"><b>${usable.length + unknown.length}/${kho.length}</b><span>nick trong kho dùng được<br>
        <small>${usable.length} có số, 5 giờ và Tuần dưới ${maxPct}%${unknown.length ? ` · ${unknown.length} chưa đo được số nhưng vẫn dùng bình thường` : ""}</small></span></div>
      ${best ? `<div class="qbest">Nên dùng tiếp: <b>${esc(best.email)}</b> <span class="team t-${teamKey(best.team)}">${esc(best.team)}</span>
        <small>5 giờ ${Math.round(best.fiveHour?.pct ?? 0)}%</small></div>` : ""}
      <div class="qtools"><small>Gom lúc ${new Date(quota.generatedAt * 1000).toLocaleTimeString("vi-VN")}</small>
        <button class="btn" id="q-refresh">↻ Làm mới</button></div>
    </div>
    <h3 class="qh">🗄 Kho nick <small>claude-swap · ${kho.length} túi</small></h3>
    <div class="qgrid">${kho.sort(byPct).map((p) => poolCard(p, maxPct)).join("")}</div>
    <h3 class="qh">🖥 Đăng nhập trong app <small>từng bản app Claude · ${inApp.length} nick${idle.length ? ` · ${esc(idle.join(", "))} chưa đăng nhập` : ""} · 🗄 = có cả trong kho</small></h3>
    <div class="qgrid">${inApp.map((p) => poolCard(p, maxPct, { markKho: true })).join("")}</div>
    ${cliOnly.length ? `<h3 class="qh">⌨️ Chỉ đăng nhập ở lane CLI <small>${cliOnly.length} túi</small></h3>
    <div class="qgrid">${cliOnly.sort(byPct).map((p) => poolCard(p, maxPct)).join("")}</div>` : ""}
    ${quota.errors && quota.errors.length ? `<p class="err">${esc(quota.errors.join(" · "))}</p>` : ""}
    <p class="muted qfoot">Số của kho do claude-swap lưu, có thể cũ vài tới ~20 phút. Túi đang có phiên mở trong app thì hỏi thẳng Anthropic, mỗi túi tối đa 10 phút một lần; túi không có phiên nào mở chỉ còn số app ghi lần cuối.</p>`;
}

document.addEventListener("click", (e) => {
  const t = e.target.closest(".tabs button")?.dataset.tab;
  if (t) setTab(t);
  if (e.target.id === "q-refresh") loadQuota(true);
});
setInterval(() => { if (tab === "quota" && !document.hidden) loadQuota(false); }, 60000);

// ---------- vòng làm mới ----------

// Cửa sổ ẩn (thu nhỏ, bị che hết, tab khác): không vẽ lại văn phòng, chỉ giữ tiêu đề cho chấm đỏ Dock.
// Hiện lại thì vẽ ngay với dữ liệu mới nhất nên người xem không thấy khác gì.
function render() {
  if (!state) return;
  if (document.hidden) { updateTitle(); return; }
  renderHeader();
  renderOffice();
  renderViews();
}

let inflight = 0;
async function refresh() {
  const my = ++inflight;
  try {
    const s = await api(`/api/state?gio=${filter.gio}`);
    if (s.error) throw new Error(s.error);
    if (my !== inflight) return;
    state = s;
    render();
  } catch (e) {
    $("#sub").textContent = "Mất kết nối máy chủ văn phòng: " + e.message;
  }
}

// ---------- sự kiện ----------

function onDeskActivate(e) {
  const mk = e.target.closest("[data-mark]");
  if (mk) {
    e.stopPropagation();
    const id = mk.closest("[data-id]").dataset.id;
    mark([id], mk.dataset.mark || null);
    return;
  }
  const sweep = e.target.closest("[data-sweep]");
  if (sweep) {
    const room = state.projects.find((p) => p.key === sweep.dataset.sweep);
    const ids = room.sessions.filter((s) => placeOf(s) === "ban" && s.status === "nghi" && !s.mark).map((s) => s.id);
    if (ids.length) mark(ids, "xong");
    return;
  }
  const target = e.target.closest("[data-id]");
  if (target) openDetail(target.dataset.id);
}
$("#office").addEventListener("click", onDeskActivate);
$("#office").addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches(".desk, .mini, .pod-head, .subs")) { e.preventDefault(); onDeskActivate(e); }
});

const toggleIn = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
$("#kpis").addEventListener("click", (e) => {
  const k = e.target.closest("[data-status]")?.dataset.status;
  if (k) setFilter({ ...filter, status: filter.status.length === 1 && filter.status[0] === k ? [] : [k] });
});
$("#f-status").addEventListener("click", (e) => {
  const k = e.target.closest("[data-status]")?.dataset.status;
  if (k) setFilter({ ...filter, status: toggleIn(filter.status, k) });
});
$("#apps").addEventListener("click", (e) => {
  const a = e.target.closest(".appchip")?.dataset.app;
  if (!a) return;
  // Bấm một bản khi đang xem tất cả = chỉ xem bản đó; bấm tiếp các bản khác để thêm vào.
  const cur = filter.apps.length ? filter.apps : [];
  let next = cur.length ? toggleIn(cur, a) : [a];
  const present = [...new Set(allSessions().map((s) => s.app))];
  if (present.every((x) => next.includes(x))) next = [];
  setFilter({ ...filter, apps: next });
});
$("#dd-btn").addEventListener("click", (e) => { e.stopPropagation(); $("#dd-menu").hidden = !$("#dd-menu").hidden; });
$("#dd-menu").addEventListener("click", (e) => e.stopPropagation());
$("#dd-menu").addEventListener("change", (e) => {
  if (e.target.type !== "checkbox") return;
  setFilter({ ...filter, projects: toggleIn(filter.projects, e.target.value) });
});
$("#dd-menu").addEventListener("click", (e) => {
  if (e.target.matches("[data-proj-all]")) setFilter({ ...filter, projects: [] });
});
let qTimer;
$("#q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => setFilter({ ...filter, q: e.target.value }), 160); });
$("#f-gio").addEventListener("change", (e) => setFilter({ ...filter, gio: Number(e.target.value) }));
$("#f-routine").addEventListener("change", (e) => setFilter({ ...filter, routine: e.target.value }));
$("#f-xong").addEventListener("change", (e) => setFilter({ ...filter, xong: e.target.value }));
$("#f-an").addEventListener("change", (e) => setFilter({ ...filter, hienAn: e.target.checked }));
$("#f-reset").addEventListener("click", () => setFilter({ ...DEFAULT_FILTER }));

$("#views").addEventListener("click", (e) => {
  const go = e.target.closest("[data-view-go]")?.dataset.viewGo;
  if (go) { setFilter(views.find((v) => v.id === go).loc); $("#view-menu").hidden = true; return; }
  const more = e.target.closest("[data-view-menu]");
  if (more) { e.stopPropagation(); viewMenu(more.dataset.viewMenu, more); return; }
  if (e.target.id === "view-add") {
    e.stopPropagation();
    askName(e.target, "", (name) => {
      views.push({ id: "v" + Date.now().toString(36), ten: name, loc: slim(filter) });
      saveViews();
      toast(`Đã lưu màn hình "${name}"`);
    });
  }
});
document.addEventListener("click", () => { $("#dd-menu").hidden = true; const m = $("#view-menu"); if (m) m.hidden = true; });

$("#close").addEventListener("click", closeDetail);
$("#scrim").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDetail();
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && document.activeElement?.id === "reply-text") sendReply();
  if (e.key === "/" && document.activeElement?.tagName !== "TEXTAREA" && document.activeElement?.tagName !== "INPUT") { e.preventDefault(); $("#q").focus(); }
});
$("#detail").addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "reply") sendReply();
  if (act === "open") openInApp();
  if (act === "sum") summarize(openId, false);
  if (act === "resum") summarize(openId, true);
  if (act === "xong" || act === "an") mark([openId], act);
  if (act === "unmark") mark([openId], null);
  if (act === "copy-id" && current) copyText(current.cli).then((ok) => toast(ok ? `Đã copy ID: ${current.cli}` : "Không copy được", !ok));
  if (act === "handoff" && current) copyText(handoffText(current)).then((ok) => toast(ok ? "Đã copy bản handoff, dán vào phiên mới" : "Không copy được", !ok));
  if (act === "compact" && current) doCompact(current);
  if (act === "ask-fill") { const box = $("#reply-text"); if (box) { box.value = e.target.closest("[data-msg]").dataset.msg; box.focus(); } }
  if (act === "name-org") nameOrg(e.target.closest("[data-org]").dataset.org, e.target);
  const go = e.target.closest("[data-goto]")?.dataset.goto;
  if (go) openDetail(go);
});

// Mở trang: bộ lọc trên địa chỉ > màn hình mặc định > bộ lọc lần trước.
(async function start() {
  views = await api("/api/views").catch(() => []);
  if (!Array.isArray(views)) views = [];
  let loc = null;
  const m = location.hash.match(/^#loc=(.+)$/);
  if (m) { try { loc = JSON.parse(decodeURIComponent(m[1])); } catch { /* bỏ qua */ } }
  if (!loc) loc = views.find((v) => v.macDinh)?.loc ?? store.get("vp-loc", {});
  filter = normFilter(loc);
  syncFilterControls();
  setTab(store.get("vp-tab", "office"));
  await refresh();
  setInterval(refresh, 3000);
  setInterval(() => {
    const typing = document.activeElement?.matches?.("#detail input, #detail textarea");
    if (openId && !typing && !document.hidden) openDetail(openId, { quiet: true });
  }, 6000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    render();
    refresh();
    if (openId) openDetail(openId, { quiet: true });
    if (tab === "quota") loadQuota(false);
  });
})();

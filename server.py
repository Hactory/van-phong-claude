#!/usr/bin/env python3
"""Văn phòng Claude: máy chủ nhỏ trên máy, đọc phiên Code của mọi bản app Claude desktop
rồi phục vụ trang văn phòng ở http://127.0.0.1:4747.

Chỉ ĐỌC dữ liệu app tự ghi, không sửa gì của app:
  - danh sách phiên:  ~/Library/Application Support/Claude*/claude-code-sessions/<tài khoản>/<tổ chức>/local_*.json
  - phiên đang mở:    ~/.claude/sessions/<pid>.json (status busy/idle, ổ cắm nhắn tin)
  - nhật ký phiên:    ~/.claude/projects/<thư mục>/<cliSessionId>.jsonl (+ subagents/)
  - phiên Codex mở từ app desktop (ChatGPT): ~/.codex/state_N.sqlite (danh sách, đã đọc chưa)
    và ~/.codex/thread_history_N.sqlite (từng lượt, từng mục), mở chỉ đọc. Lượt `codex exec` chạy từ
    dòng lệnh thì bỏ qua.
Các việc có tác động, chỉ chạy khi người dùng bấm trên trang:
  - tóm tắt: gọi bộ tóm tắt khai báo trong cấu hình (mục "tomTat"); không khai báo thì tắt
  - trả lời: đẩy thẳng vào phiên đang mở qua ổ cắm nhắn tin của Claude Code, phiên đã đóng thì
    `claude --resume <id> -p`
  - mở trong app: gửi link claude://code/continue?session=local_… tới ĐÚNG tiến trình bản app
  - compact phiên đã đóng: `claude --resume <id> -p /compact`

Cấu hình riêng từng máy nằm ở ~/.claude/van-phong/cau-hinh.json (xem cau-hinh.mau.json).
Không cần thư viện ngoài. Chạy: python3 server.py [--port 4747]
"""
import argparse
import collections
import glob
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
APP_SUPPORT = os.path.join(HOME, "Library", "Application Support")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
LIVE_DIR = os.path.join(HOME, ".claude", "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HOME, ".claude", "van-phong")
WEB_DIR = os.path.join(HERE, "web")
SEND_URL_JS = os.path.join(HERE, "tools", "gui-link-toi-pid.js")
CONFIG_FILE = os.path.join(STATE_DIR, "cau-hinh.json")   # cấu hình riêng từng máy, không nằm trong repo
os.makedirs(os.path.join(STATE_DIR, "tom-tat"), exist_ok=True)

WINDOW_HOURS = 24           # mặc định: phiên đóng nhưng có hoạt động trong 24 giờ vẫn hiện
MAX_WINDOW_HOURS = 24 * 14
MARKS_FILE = os.path.join(STATE_DIR, "danh-dau.json")    # phiên đã đánh dấu xong / ẩn
VIEWS_FILE = os.path.join(STATE_DIR, "man-hinh.json")    # các màn hình (bộ lọc) đã lưu
MARK_GRACE_MS = 60_000      # hoạt động mới quá mốc này sau lúc đánh dấu thì phiên tự hiện lại
SUBAGENT_STALE_SEC = 15 * 60   # subagent chưa báo xong mà im quá 15 phút coi như đã dừng
SUBAGENT_SHOW_SEC = 15 * 60    # subagent xong trong 15 phút gần nhất vẫn ngồi cạnh phiên cha
DEFAULT_REPLY_PREFIX = "[Trả lời qua Văn phòng Claude]"


def log(msg):
    try:
        with open(os.path.join(STATE_DIR, "log.txt"), "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


_cfg = {"mtime": None, "data": {}}


def cau_hinh():
    """Đọc ~/.claude/van-phong/cau-hinh.json (đệm theo giờ sửa file). Mọi khoá đều tuỳ chọn."""
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mt = None
    if mt != _cfg["mtime"]:
        _cfg.update(mtime=mt, data=(load_json(CONFIG_FILE, {}) or {}) if mt else {})
    return _cfg["data"]


def claude_bin():
    import shutil
    return cau_hinh().get("claude") or shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude")


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


# ---------- bản app và danh tính nick ----------

def app_number(dirname):
    """Thư mục dữ liệu của bản app -> số bản: Claude = 01, Claude-NN = NN. Thư mục đặt tên khác thì
    khai số trong cấu hình, mục "banApp" (vd. {"Claude-khac": "02"}); không khai thì bỏ qua."""
    if dirname == "Claude":
        return "01"
    alias = (cau_hinh().get("banApp") or {}).get(dirname)
    if alias:
        return str(alias).zfill(2)
    m = re.fullmatch(r"Claude-(\d\d)", dirname)
    return m.group(1) if m else None


IDENTITY_FILE = os.path.join(STATE_DIR, "danh-tinh.json")
_identity = {"accounts": {}, "orgs": {}, "learned_at": 0}


def proc_envs(pids):
    """PID -> dòng lệnh kèm biến môi trường (ps eww), một lần ps cho mọi tiến trình."""
    envs = {}
    try:
        pids = ",".join(sorted({str(p) for p in pids if p}))
        if pids:
            out = subprocess.run(["ps", "eww", "-o", "pid=,command=", "-p", pids],
                                 capture_output=True, text=True, timeout=5).stdout
            want, cur = set(pids.split(",")), None
            for line in out.splitlines():
                pid, _, cmd = line.strip().partition(" ")
                if pid in want and pid not in envs:
                    cur, envs[pid] = pid, cmd
                elif cur:                       # biến môi trường có xuống dòng: vẫn thuộc tiến trình trước
                    envs[cur] += "\n" + line
    except Exception:  # noqa: BLE001
        pass
    return envs


def learn_identity():
    """Gom email theo mã tài khoản và tên theo mã tổ chức từ mọi nguồn trên máy, nhớ vào file.
    App không ghi tên tổ chức ra đĩa, nên tên chỉ có khi một CLI/kho nick từng đăng nhập tổ chức đó;
    thiếu thì đặt tên trên trang (nút "đặt tên"), lưu ở ~/.claude/van-phong/danh-tinh.json (mục "orgs")."""
    ident = load_json(IDENTITY_FILE, {}) or {}
    accounts, orgs, plans = ident.get("accounts", {}), ident.get("orgs", {}), ident.get("plans", {})
    files = [os.path.join(HOME, ".claude.json")] + glob.glob(os.path.join(HOME, ".claude*", ".claude.json")) \
        + glob.glob(os.path.join(HOME, ".claude-swap-backup", "sessions", "*", ".claude.json"))
    for f in files:
        a = (load_json(f, {}) or {}).get("oauthAccount") or {}
        if a.get("accountUuid") and a.get("emailAddress"):
            accounts.setdefault(a["accountUuid"], a["emailAddress"])
        if a.get("organizationUuid") and a.get("organizationName"):
            orgs.setdefault(a["organizationUuid"], a["organizationName"])
    # Phiên app đang mở mang sẵn email + mã tài khoản trong biến môi trường của tiến trình (một lần ps cho mọi phiên).
    lives = list(live_records().values())
    envs = proc_envs({rec["pid"] for rec in lives})
    for rec in lives:
        out = envs.get(str(rec["pid"]), "")
        env = dict(re.findall(r"\b(CLAUDE_CODE_(?:USER_EMAIL|ACCOUNT_UUID|ORGANIZATION_UUID|SUBSCRIPTION_TYPE|RATE_LIMIT_TIER))=(\S+)", out))
        if env.get("CLAUDE_CODE_ACCOUNT_UUID") and env.get("CLAUDE_CODE_USER_EMAIL"):
            accounts[env["CLAUDE_CODE_ACCOUNT_UUID"]] = env["CLAUDE_CODE_USER_EMAIL"]
        # Loại gói theo tổ chức: max/pro = tổ chức cá nhân (app không ghi tên tổ chức ra đĩa).
        if env.get("CLAUDE_CODE_ORGANIZATION_UUID") and env.get("CLAUDE_CODE_SUBSCRIPTION_TYPE"):
            plans[env["CLAUDE_CODE_ORGANIZATION_UUID"]] = {"type": env["CLAUDE_CODE_SUBSCRIPTION_TYPE"],
                                                           "tier": env.get("CLAUDE_CODE_RATE_LIMIT_TIER", "")}
    ident.update(accounts=accounts, orgs=orgs, plans=plans)
    try:
        save_json(IDENTITY_FILE, ident)
    except OSError:
        pass
    _identity.update(accounts=accounts, orgs=orgs, plans=plans, learned_at=time.time())


_identity.setdefault("plans", {})
PERSONAL_PLANS = ("max", "pro", "free")


def org_label(org_uuid, email=""):
    """Tên Team để hiện: tên tổ chức, hoặc "Cá nhân" cho tổ chức cá nhân. Tên tự đặt (danh-tinh.json) thắng mọi nguồn."""
    name = _identity["orgs"].get(org_uuid, "")
    if name.endswith("'s Organization"):
        return "Cá nhân"
    if name:
        return name
    if (_identity["plans"].get(org_uuid) or {}).get("type") in PERSONAL_PLANS:
        return "Cá nhân"
    return f"Team {org_uuid[:8]}" if org_uuid else "?"


def plan_label(org_uuid):
    """'Max 20x', 'Max 5x', 'Team', 'Pro'… từ loại gói app báo, chưa biết thì rỗng."""
    p = _identity["plans"].get(org_uuid) or {}
    m = re.search(r"max_(\d+x)", p.get("tier", ""))
    if p.get("type") == "max":
        return "Max " + (m.group(1) if m else "")
    return {"team": "Team" + (f" · Max {m.group(1)}" if m else ""), "pro": "Pro", "enterprise": "Enterprise"}.get(p.get("type"), "")


def set_org_name(org_uuid, name):
    ident = load_json(IDENTITY_FILE, {}) or {}
    ident.setdefault("orgs", {})[org_uuid] = name
    save_json(IDENTITY_FILE, ident)
    _identity["orgs"][org_uuid] = name
    _state_cache.clear()


# ---------- dự án ----------

DEFAULT_PROJECTS = {
    "~": {"ten": "Thư mục nhà", "bieu_tuong": "🏠"},
    "codex-chat": {"ten": "Codex: việc lặt vặt", "bieu_tuong": "💬"},
}


def project_config():
    """Tên hiển thị + biểu tượng từng phòng: mặc định là tên thư mục, đổi trong cấu hình mục "duAn"."""
    cfg = dict(DEFAULT_PROJECTS)
    cfg.update(cau_hinh().get("duAn") or {})
    return cfg


def project_of(cwd):
    """(mã dự án, nhánh/worktree) từ thư mục làm việc. Worktree của Claude Code nằm ở
    <repo>/.claude/worktrees/<tên>; nếu thư mục đó trỏ ra chỗ khác thì thường là .../worktrees/<repo>/<tên>."""
    cwd = (cwd or "").rstrip("/")
    m = re.search(r"/([^/]+)/\.claude/worktrees/([^/]+)", cwd)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"/worktrees/([^/]+)/([^/]+)", cwd)
    if m:
        return m.group(1), m.group(2)
    if cwd.startswith(HOME + "/"):
        rest = cwd[len(HOME) + 1:].split("/")
        return rest[0], "/".join(rest[1:]) or ""
    if cwd == HOME:
        return "~", ""
    return os.path.basename(cwd) or "?", ""


# ---------- đọc nguồn ----------

def live_records():
    """Phiên đang mở (app hoặc terminal): khoá theo cả hostSessionId (local_…) lẫn sessionId CLI."""
    out = {}
    for f in glob.glob(os.path.join(LIVE_DIR, "*.json")):
        d = load_json(f)
        if not d or not pid_alive(d.get("pid")):
            continue
        for k in (d.get("hostSessionId"), d.get("sessionId")):
            if k:
                out[k] = d
    return out


REG_KEYS = ("sessionId", "cliSessionId", "title", "cwd", "originCwd", "createdAt", "lastActivityAt",
            "isArchived", "postTurnSummary", "postTurnSummaryFor", "lastAssistantUuid", "model",
            "scheduledTaskId", "completedTurns", "permissionMode", "effort", "spawnedFrom")
# Mỗi bản ghi phiên của app nặng trung bình ~250 KB (phần lớn là cấu hình MCP), nghìn phiên là vài trăm MB:
# chỉ giữ các khoá cần, và lưu bản rút gọn ra đĩa để mở lại app khỏi phải đọc lại từ đầu.
REG_CACHE_FILE = os.path.join(STATE_DIR, "bo-dem-phien.json")
REG_CACHE_SAVE_SEC = 300
_registry_cache = {}   # path -> (inode, bản ghi rút gọn)
_registry_dirs = {}    # thư mục tổ chức -> (mtime_ns, [path…] theo thứ tự thư mục)
_registry_disk = {"loaded": False, "dirty": False, "saved": 0.0}
_registry_lock = threading.Lock()
_registry_ids = {"oid": {}, "by": {}, "dup": {}}   # file -> mã văn phòng; mã văn phòng -> bản ghi; mã trùng -> [file…]


def _reg_load(path):
    d = load_json(path)
    if not d:
        return None
    parts = path.split(os.sep)
    rec = {k: d.get(k) for k in REG_KEYS}
    rec.update(appDir=parts[-5], accountUuid=parts[-3], orgUuid=parts[-2], file=path)
    return rec


def _reg_scan(d):
    """Đọc lại một thư mục tổ chức. App ghi bản ghi kiểu ghi file tạm rồi đổi tên, nên phiên nào đổi thì inode
    của file đó đổi (và mtime của thư mục đổi): chỉ file có inode mới mới phải đọc lại.
    Trả (danh sách path, đọc đủ hết chưa); file đọc lỗi thì lần sau quét lại thư mục."""
    paths, ok = [], True
    try:
        entries = list(os.scandir(d))
    except OSError:
        return paths, False
    for e in entries:
        if not (e.name.startswith("local_") and e.name.endswith(".json")):
            continue
        ino = e.inode()
        c = _registry_cache.get(e.path)
        if not c or c[0] != ino:
            rec = _reg_load(e.path)
            if rec is None:
                ok = False
                continue
            _registry_cache[e.path] = (ino, rec)
            _registry_disk["dirty"] = True
        paths.append(e.path)
    return paths, ok


def _reg_disk_load():
    _registry_disk["loaded"] = True
    data = load_json(REG_CACHE_FILE, {}) or {}
    if data.get("keys") != list(REG_KEYS):
        return
    for path, (ino, rec) in (data.get("rows") or {}).items():
        _registry_cache[path] = (ino, rec)


def _reg_disk_save():
    if not _registry_disk["dirty"] or time.time() - _registry_disk["saved"] < REG_CACHE_SAVE_SEC:
        return
    try:
        save_json(REG_CACHE_FILE, {"keys": list(REG_KEYS), "rows": {p: list(c) for p, c in _registry_cache.items()}})
    except OSError:
        return
    _registry_disk.update(dirty=False, saved=time.time())


def _reg_index(rows):
    """Mã văn phòng của từng bản ghi. Cùng một mã local_… có thể nằm ở nhiều bản app hoặc nhiều tổ chức (chép phiên
    sang bản khác, đổi tổ chức trong cùng bản), mỗi nơi là một phiên riêng với nhật ký riêng. Mã trùng thì thêm đuôi
    __<6 ký tự băm thư mục chứa> để mỗi bàn chỉ tới đúng bản ghi của nó; mã không trùng giữ nguyên."""
    n = collections.Counter(r["sessionId"] for r in rows)
    oid, by, dup = {}, {}, {}
    for r in rows:
        sid = r["sessionId"]
        if n[sid] > 1:
            dup.setdefault(sid, []).append(r["file"])
            sid = f"{sid}__{hashlib.sha1(os.path.dirname(r['file']).encode()).hexdigest()[:6]}"
        oid[r["file"]] = sid
        by[sid] = r
    _registry_ids.update(oid=oid, by=by, dup=dup)


def registry():
    """Mọi bản ghi phiên của các bản app. Thư mục tổ chức nào mtime không đổi thì dùng lại danh sách cũ."""
    with _registry_lock:
        if not _registry_disk["loaded"]:
            _reg_disk_load()
        rows, seen, changed = [], set(), False
        dirs = glob.glob(os.path.join(APP_SUPPORT, "Claude*", "claude-code-sessions", "*", "*"))
        for d in dirs:
            try:
                mt = os.stat(d).st_mtime_ns
            except OSError:
                continue
            c = _registry_dirs.get(d)
            if not c or c[0] != mt:
                paths, ok = _reg_scan(d)
                c = _registry_dirs[d] = (mt if ok else None, paths)
                changed = True
            seen.update(c[1])
            rows.extend(_registry_cache[p][1] for p in c[1])
        for gone in set(_registry_cache) - seen:
            _registry_cache.pop(gone, None)
            _registry_disk["dirty"] = changed = True
        for gone in set(_registry_dirs) - set(dirs):
            _registry_dirs.pop(gone, None)
        if changed or len(_registry_ids["oid"]) != len(rows):
            _reg_index(rows)
        _reg_disk_save()
        return rows


def office_id(rec):
    """Mã của bàn trên văn phòng (xem _reg_index): thường chính là mã local_… của app."""
    return _registry_ids["oid"].get(rec.get("file"), rec["sessionId"])


def live_of(rec, live):
    """Phiên đang mở ứng với bản ghi này. Mã local_… trùng ở nhiều bản app thì phiên đang mở mang mã CLI của
    bản sao khác không phải của bản ghi này."""
    sid, cli = rec["sessionId"], rec.get("cliSessionId") or ""
    if sid not in _registry_ids["dup"]:
        return live.get(sid) or live.get(cli)
    lv = live.get(cli) or live.get(sid)
    if lv and lv.get("sessionId") != cli and any(
            (_registry_cache.get(f) or (0, {}))[1].get("cliSessionId") == lv.get("sessionId")
            for f in _registry_ids["dup"][sid]):
        return None
    return lv


def parent_id(rec):
    """Mã văn phòng của phiên cha. Phiên cha có mã trùng thì lấy bản nằm cùng thư mục, rồi cùng bản app."""
    psid = (rec.get("spawnedFrom") or {}).get("sessionId")
    files = _registry_ids["dup"].get(psid)
    if not files:
        return psid
    here = os.path.dirname(rec.get("file") or "")
    for f in files:
        if os.path.dirname(f) == here:
            return _registry_ids["oid"].get(f, psid)
    for f in files:
        if f.split(os.sep)[-5] == rec.get("appDir"):
            return _registry_ids["oid"].get(f, psid)
    return psid


_transcript_path = {}
_tindex = {"at": 0.0, "order": [], "dirs": {}}   # dirs: thư mục dự án -> (mtime_ns, tên các file .jsonl)


def transcript_index():
    """Tên nhật ký trong từng thư mục dự án (cả nghìn thư mục). Chỉ liệt kê lại thư mục có mtime đổi (tạo file mới
    là đổi mtime thư mục), và tối đa mỗi giây một lần, thay cho glob cả nghìn thư mục mỗi lần tìm một phiên."""
    if time.time() - _tindex["at"] < 1:
        return _tindex
    try:
        order = [e.path for e in os.scandir(PROJECTS_DIR) if not e.name.startswith(".")]
    except OSError:
        order = []
    dirs = {}
    for d in order:
        try:
            mt = os.stat(d).st_mtime_ns
        except OSError:
            continue
        c = _tindex["dirs"].get(d)
        if not c or c[0] != mt:
            try:
                c = (mt, {n for n in os.listdir(d) if n.endswith(".jsonl")})
            except OSError:
                c = (mt, set())
        dirs[d] = c
    _tindex.update(at=time.time(), order=order, dirs=dirs)
    return _tindex


def transcript_of(cli_id):
    if not cli_id:
        return None
    p = _transcript_path.get(cli_id)
    if p and os.path.exists(p):
        return p
    idx, name = transcript_index(), cli_id + ".jsonl"
    hits = [h for d in idx["order"] if name in idx["dirs"].get(d, (0, ()))[1]
            if os.path.lexists(h := os.path.join(d, name))]
    p = max(hits, key=os.path.getmtime) if hits else None
    if p:
        _transcript_path[cli_id] = p
    return p


def read_tail(path, nbytes):
    entries = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return entries
    lines = data.split("\n")
    if size > nbytes:
        lines = lines[1:]  # dòng đầu có thể bị cắt ngang
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def is_human_prompt(e):
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return False
    t = text_of(c).strip()
    return bool(t) and not t.startswith("<")


def describe_tool(name, inp):
    """Một dòng ngắn tiếng Việt cho việc agent đang làm."""
    inp = inp or {}
    short = lambda s, n=70: (s or "").replace("\n", " ")[:n]  # noqa: E731
    if name == "Bash":
        return "Chạy lệnh: " + short(inp.get("description") or inp.get("command"))
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "Sửa " + os.path.basename(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "Read":
        return "Đọc " + os.path.basename(inp.get("file_path") or "")
    if name in ("Grep", "Glob"):
        return "Tìm: " + short(inp.get("pattern"), 50)
    if name == "Agent":
        return "Giao subagent: " + short(inp.get("description"), 60)
    if name in ("WebSearch", "WebFetch"):
        return "Tra web: " + short(inp.get("query") or inp.get("url"), 60)
    if name == "Skill":
        return "Dùng skill " + short(inp.get("skill"), 40)
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"Gọi {parts[1] if len(parts) > 1 else 'MCP'}: {parts[-1]}"
    return name


# ---------- tiền, ngữ cảnh, cache ----------

# Giá API theo triệu token (vào, ra) và cửa sổ ngữ cảnh, theo bảng giá công bố (10/2026).
# Cách cộng theo đúng Claude Code để số khớp chi phí phiên Claude Code tự báo: đọc cache = 0,1 lần giá vào,
# ghi cache = 1,25 lần, không tách loại 5 phút / 1 giờ.
# Cột đọc cache bên dưới chỉ để tham khảo. Xếp tiền tố dài trước để "claude-opus-5-5" không khớp nhầm "claude-opus-5".
PRICES = [
    ("claude-fable-5-1", 10, 50, 0.25, 1_000_000), ("claude-fable-5", 10, 50, 1.0, 1_000_000),
    ("claude-opus-5-5", 4, 20, 0.20, 1_000_000), ("claude-opus-5", 5, 25, 0.5, 1_000_000),
    ("claude-opus-4-8", 5, 25, 0.5, 1_000_000), ("claude-opus-4-7", 5, 25, 0.5, 1_000_000),
    ("claude-opus-4-6", 5, 25, 0.5, 1_000_000), ("claude-opus-4-5", 5, 25, 0.5, 200_000),
    ("claude-opus-4", 15, 75, 1.5, 200_000),
    ("claude-sonnet-5-5", 2, 10, 0.2, 1_000_000), ("claude-sonnet-5", 2, 10, 0.2, 1_000_000),
    ("claude-sonnet-4-6", 3, 15, 0.3, 1_000_000), ("claude-sonnet-4", 3, 15, 0.3, 200_000),
    ("claude-haiku-5-5", 0.1, 0.5, 0.01, 1_000_000), ("claude-haiku-4-5", 1, 5, 0.1, 200_000),
]


def price_of(model):
    m = (model or "").lower()
    for row in PRICES:
        if m.startswith(row[0]):
            return row[1:]
    return None


def cost_of(model, u):
    """Tiền một lượt gọi model, quy ra giá API (gói đăng ký không trả theo token). None = chưa biết giá."""
    p = price_of(model)
    if not p:
        return None
    inp, out = p[0], p[1]
    cw, cr, it = u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0, u.get("input_tokens") or 0
    mult = 2 if u.get("speed") == "fast" else 1
    return mult * (it * inp + cw * inp * 1.25 + cr * inp * 0.1 + (u.get("output_tokens") or 0) * out) / 1e6


class UsageAcc:
    """Cộng dồn token/tiền theo mã tin (mỗi tin ghi lặp vài dòng, chỉ tính một lần) và nhớ lượt gọi cuối
    để biết ngữ cảnh đang dùng bao nhiêu và cache còn ấm tới lúc nào."""

    def __init__(self):
        self.msgs = {}          # message.id -> (tiền, vào, ra, đọc cache, ghi cache, model)
        self.last_other = 0     # mốc dòng không phải của Claude gần nhất ≈ lúc gửi yêu cầu
        self.req_start = 0
        self.ttl = 0
        self.ctx = 0
        self.model = ""
        self.unpriced = set()

    def feed(self, e):
        ts = iso_ms(e.get("timestamp"))
        if e.get("type") != "assistant":
            if ts:
                self.last_other = ts
            return
        m = e.get("message") or {}
        u = m.get("usage")
        if not u:
            return
        mid = m.get("id") or e.get("uuid")
        model = m.get("model") or ""
        c = cost_of(model, u)
        if c is None and model and not model.startswith("<"):
            self.unpriced.add(model)
        new = mid not in self.msgs
        self.msgs[mid] = (c or 0, u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                          u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0, model)
        if e.get("isSidechain"):
            return
        if new:
            self.req_start = self.last_other or ts
        cc = u.get("cache_creation") or {}
        if cc.get("ephemeral_1h_input_tokens"):
            self.ttl = 3600
        elif cc.get("ephemeral_5m_input_tokens"):
            self.ttl = 300
        self.ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens", "output_tokens"))
        self.model = model or self.model

    def summary(self):
        tot = [0, 0, 0, 0, 0]
        for c, i, o, r, w, _ in self.msgs.values():
            for k, v in enumerate((c, i, o, r, w)):
                tot[k] += v
        win = (price_of(self.model) or (0, 0, 0, 0))[3]
        return {"cost": round(tot[0], 4), "input": tot[1], "output": tot[2], "cacheRead": tot[3],
                "cacheWrite": tot[4], "calls": len(self.msgs), "ctx": self.ctx, "window": win,
                "model": self.model, "unpriced": sorted(self.unpriced)}

    def cache(self):
        if not self.req_start:
            return None
        return {"start": self.req_start, "ttl": self.ttl or 3600, "known": bool(self.ttl)}


def ctx_moc():
    """Mốc % ngữ cảnh = ngưỡng tự tóm tắt autoCompactWindow trong ~/.claude/settings.json; không đặt thì None
    (khi đó so với cả cửa sổ ngữ cảnh của model)."""
    v = (load_json(os.path.join(HOME, ".claude", "settings.json"), {}) or {}).get("autoCompactWindow")
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


_usage_cache = {}  # đường dẫn nhật ký -> (vị trí đã đọc, UsageAcc); nhật ký chỉ ghi thêm nên đọc nối tiếp


def usage_of(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return UsageAcc()
    off, acc = _usage_cache.get(path, (0, None))
    if acc is None or size < off:
        off, acc = 0, UsageAcc()
    if size > off:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read(size - off)
        cut = data.rfind(b"\n")
        if cut >= 0:
            for line in data[:cut + 1].splitlines():
                try:
                    acc.feed(json.loads(line))
                except ValueError:
                    pass
            off += cut + 1
    _usage_cache[path] = (off, acc)
    return acc


def session_usage(tpath):
    """Tiền cả phiên (phiên chính + mọi subagent), ngữ cảnh và cache của phiên chính."""
    if not tpath:
        return None
    main = usage_of(tpath)
    out = main.summary()
    sub_cost = 0
    for f in glob.glob(os.path.join(tpath[:-len(".jsonl")], "subagents", "agent-*.jsonl")):
        s = usage_of(f).summary()
        sub_cost += s["cost"]
        out["unpriced"] = sorted(set(out["unpriced"]) | set(s["unpriced"]))
    out["subCost"] = round(sub_cost, 4)
    out["total"] = round(out["cost"] + sub_cost, 4)
    out["cache"] = main.cache()
    out["mocCompact"] = ctx_moc()
    out["moc"] = out["mocCompact"] or out["window"]
    return out


_scan_cache = {}   # (path, số byte đọc) -> (mtime, size, kết quả)


def scan_cached(path, nbytes=120_000):
    if not path:
        return scan_transcript(None)
    try:
        st = os.stat(path)
    except OSError:
        return scan_transcript(None)
    c = _scan_cache.get((path, nbytes))
    if not c or c[0] != st.st_mtime or c[1] != st.st_size:
        c = _scan_cache[(path, nbytes)] = (st.st_mtime, st.st_size, scan_transcript(path, nbytes))
    return c[2]


def scan_transcript(path, nbytes=200_000):
    """Hoạt động gần nhất, lời Claude cuối, yêu cầu cuối của người dùng, nhánh git, PR."""
    res = {"activity": "", "activityAt": 0, "lastText": "", "lastUser": "", "branch": "",
           "entrypoint": "", "aiTitle": "", "timeline": [], "prs": [], "cache": None, "ask": "", "askActions": []}
    if not path:
        return res
    entries = read_tail(path, nbytes)
    acc = UsageAcc()
    for e in entries:
        acc.feed(e)
        if is_human_prompt(e):                  # người dùng đã trả lời: câu hỏi của lượt trước hết hiệu lực
            res["ask"], res["askActions"] = "", []
        res["entrypoint"] = e.get("entrypoint") or res["entrypoint"]
        if e.get("gitBranch") and e.get("gitBranch") != "HEAD":
            res["branch"] = e["gitBranch"]
        t = e.get("type")
        if t in ("custom-title", "ai-title"):
            res["aiTitle"] = e.get("customTitle") or e.get("aiTitle") or res["aiTitle"]
        elif t == "pr-link":
            url = e.get("prUrl") or e.get("url")
            if url and url not in res["prs"]:
                res["prs"].append(url)
        elif t == "assistant" and not e.get("isSidechain"):
            for b in (e.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name = b.get("name", "")
                    if name == "mcp__ccd_turn__wrap_up":
                        res["ask"], res["askActions"] = wrap_up_ask(b.get("input"))
                    if name.startswith("mcp__ccd_turn__") or name in ("TodoWrite",):
                        continue
                    res["timeline"].append({"at": e.get("timestamp"), "kind": "tool",
                                            "text": describe_tool(name, b.get("input"))})
                elif b.get("type") == "text" and b.get("text", "").strip():
                    res["timeline"].append({"at": e.get("timestamp"), "kind": "say",
                                            "text": b["text"].strip()[:220]})
        elif is_human_prompt(e):
            res["timeline"].append({"at": e.get("timestamp"), "kind": "user",
                                    "text": text_of((e.get("message") or {}).get("content")).strip()[:220]})
    # Lời Claude sau tin cuối của người dùng.
    last = []
    for e in reversed(entries):
        if is_human_prompt(e):
            res["lastUser"] = text_of((e.get("message") or {}).get("content"))
            break
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            t = text_of((e.get("message") or {}).get("content")).strip()
            if t:
                last.append(t)
    res["lastText"] = "\n\n".join(reversed(last[:3]))
    for item in reversed(res["timeline"]):
        if item["kind"] == "tool":
            res["activity"], res["activityAt"] = item["text"], iso_ms(item["at"])
            break
    res["timeline"] = res["timeline"][-14:]
    res["cache"] = acc.cache()
    return res


APP_ACTIONS = {"commit": "Commit", "create_pr": "Tạo PR", "merge": "Merge", "fix_ci": "Sửa CI", "address_comments": "Xử lý góp ý",
               "resolve_conflicts": "Gỡ xung đột", "verify": "Kiểm tra", "archive": "Lưu trữ phiên", "new_session": "Phiên mới"}


def wrap_up_ask(inp):
    """Câu hỏi + các nút ở hộp "bước tiếp" mà Claude để lại cuối lượt (tool wrap_up của app). Lượt kết bằng câu hỏi
    là phiên đang chờ người dùng quyết, dù bản tóm tắt của app chấm lượt đó là "completed"."""
    inp = inp if isinstance(inp, dict) else {}
    ask = str(inp.get("ask") or "").strip()
    if not ask:
        return "", []
    acts = inp.get("actions") or []
    if isinstance(acts, str):
        try:
            acts = json.loads(acts)
        except ValueError:
            acts = []
    out = []
    for a in acts if isinstance(acts, list) else []:
        if isinstance(a, dict) and (a.get("label") or a.get("action")):
            act = str(a.get("action") or "")
            out.append({"label": str(a.get("label") or APP_ACTIONS.get(act, act)),
                        "message": str(a.get("message") or a.get("label") or ""), "app": bool(act)})
    return ask, out[:3]


def iso_ms(s):
    if not s:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


_sub_cache = {}   # file meta -> (mtime, nội dung)


def subagents_of(path):
    """Danh sách subagent của một phiên, mới nhất trước, đọc từ <phiên>/subagents/agent-*.meta.json
    (mô tả, loại, model, đã xong chưa) và giờ ghi nhật ký cuối."""
    if not path:
        return []
    now = time.time()
    out = []
    for log_file in glob.glob(os.path.join(path[:-len(".jsonl")], "subagents", "agent-*.jsonl")):
        try:
            mt = os.path.getmtime(log_file)
        except OSError:
            continue
        meta_file = log_file[:-len(".jsonl")] + ".meta.json"
        try:
            mmt = os.path.getmtime(meta_file)
        except OSError:
            mmt = 0
        c = _sub_cache.get(meta_file)
        if not c or c[0] != mmt:
            c = _sub_cache[meta_file] = (mmt, load_json(meta_file, {}) or {})
        meta = c[1]
        active = not meta.get("finished") and now - mt < SUBAGENT_STALE_SEC
        out.append({"id": os.path.basename(log_file)[len("agent-"):-len(".jsonl")],
                    "desc": meta.get("description") or meta.get("agentType") or "Subagent",
                    "type": meta.get("agentType", ""), "model": meta.get("model", ""),
                    "active": active, "at": int(mt * 1000)})
    out.sort(key=lambda x: (not x["active"], -x["at"]))
    return out


def subagent_summary(subs):
    """Phần gửi lên văn phòng: subagent đang chạy + vừa xong (15 phút), tối đa 8."""
    now_ms = time.time() * 1000
    shown = [x for x in subs if x["active"] or now_ms - x["at"] < SUBAGENT_SHOW_SEC * 1000]
    return shown[:8], sum(1 for x in subs if x["active"]), len(subs)


# ---------- phiên Codex trong app desktop ----------

CODEX_HOME = os.path.join(HOME, ".codex")
CODEX_ORIGINATORS = ("Codex Desktop", "codex_work_desktop")   # lượt codex_exec chạy từ dòng lệnh: bỏ qua
CODEX_RUNNING_SEC = 10 * 60   # lượt "đang chạy" mà im quá 10 phút coi như app đã tắt giữa chừng
CODEX_PREFIX = "codex_"       # mã phiên Codex trên văn phòng = codex_<mã luồng>


def codex_db(prefix):
    """Kết nối CHỈ ĐỌC tới bản mới nhất của ~/.codex/<prefix>_N.sqlite. thread_history không bật WAL
    nên mode=ro có lúc không mở được; khi đó dùng immutable (bỏ qua khoá, vẫn chỉ đọc)."""
    files = [(int(m.group(1)), f) for f in glob.glob(os.path.join(CODEX_HOME, f"{prefix}_*.sqlite"))
             if (m := re.search(r"_(\d+)\.sqlite$", f))]
    if not files:
        return None
    path = max(files)[1]
    for q in ("mode=ro", "immutable=1"):
        try:
            con = sqlite3.connect(f"file:{urllib.parse.quote(path)}?{q}", uri=True, timeout=2)
            con.execute("select 1 from sqlite_master limit 1")
            return con
        except sqlite3.Error:
            continue
    return None


_codex_app = {"at": 0, "open": False}


def codex_app_open():
    if time.time() - _codex_app["at"] > 10:
        r = subprocess.run(["pgrep", "-f", "ChatGPT.app/Contents/MacOS/ChatGPT"], capture_output=True)
        _codex_app.update(at=time.time(), open=r.returncode == 0)
    return _codex_app["open"]


def codex_project(cwd):
    """Worktree app Codex tự tạo nằm ở .../worktrees/_codex-app/<mã>/<repo>; việc vặt ở ~/Documents/Codex."""
    cwd = (cwd or "").rstrip("/")
    m = re.search(r"/worktrees/_codex-app/([^/]+)/([^/]+)", cwd)
    if m:
        return m.group(2), "codex " + m.group(1)
    if cwd.startswith(os.path.join(HOME, "Documents", "Codex")):
        return "codex-chat", ""
    return project_of(cwd)


def codex_rows(since_ms=0, ids=None):
    """Luồng Codex mở từ app (cả subagent của chúng), mới nhất trước."""
    con = codex_db("state")
    if not con:
        return []
    cols = ("id, source, cwd, title, name, model, reasoning_effort, created_at_ms, updated_at_ms, "
            "tokens_used, git_branch, agent_nickname, agent_path")
    try:
        if ids:
            q = f"select {cols} from threads where id in ({','.join('?' * len(ids))})"
            cur = con.execute(q, list(ids))
        else:
            # Dấu + tắt chỉ mục archived (hầu hết luồng đều archived = 0) để SQLite đi theo chỉ mục updated_at_ms:
            # chỉ chạm các luồng trong khung giờ thay vì cả vạn luồng (~27 ms -> ~1 ms).
            cur = con.execute(f"select {cols} from threads where updated_at_ms > ? and +archived = 0 "
                              "and +originator in (?, ?) order by updated_at_ms desc", (since_ms, *CODEX_ORIGINATORS))
        names = [c[0] for c in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
        unread = {r[0] for r in con.execute(
            "select thread_id from thread_read_receipts where first_unread_turn is not null")}
    except sqlite3.Error as e:
        log(f"đọc Codex lỗi: {e}")
        return []
    finally:
        con.close()
    for r in rows:
        r["parentId"] = None
        if (r["source"] or "").startswith("{"):
            try:
                r["parentId"] = json.loads(r["source"])["subagent"]["thread_spawn"]["parent_thread_id"]
            except (ValueError, KeyError, TypeError):
                pass
        r["unread"] = r["id"] in unread
    return rows


def codex_title(r):
    t = (r.get("name") or r.get("title") or "").strip()
    t = re.sub(r"^#+\s*", "", t.split("\n")[0]).strip()
    return (t[:90] + "…") if len(t) > 90 else t or "Phiên Codex"


def describe_codex(item):
    t = item.get("type")
    short = lambda x, n=70: (x or "").replace("\n", " ")[:n]  # noqa: E731
    if t == "commandExecution":
        cmd = item.get("command") or ""
        cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
        return "Chạy lệnh: " + short(re.sub(r"^/bin/(z|ba)?sh -l?c ", "", cmd).strip("\"'"))
    if t == "mcpToolCall":
        return f"Gọi {item.get('server') or 'MCP'}: {item.get('tool') or ''}"
    if t == "fileChange":
        ch = item.get("changes") or []
        first = os.path.basename((ch[0] or {}).get("path", "")) if ch else ""
        return "Sửa " + first + (f" (+{len(ch) - 1} file)" if len(ch) > 1 else "")
    if t == "webSearch":
        return "Tra web: " + short(item.get("query"), 60)
    if t == "collabAgentToolCall":
        return {"spawn_agent": "Giao subagent", "wait": "Chờ subagent"}.get(item.get("tool"), "Điều phối subagent")
    if t == "subAgentActivity":
        return f"Subagent {os.path.basename(item.get('agentPath') or '')}: {item.get('kind') or ''}"
    if t == "contextCompaction":
        return "Tự tóm tắt ngữ cảnh"
    return ""


def codex_text(item):
    if item.get("type") == "agentMessage":
        return (item.get("text") or "").strip()
    c = item.get("content") or []
    return "\n".join(x.get("text", "") for x in c if isinstance(x, dict)).strip()


_codex_scan_cache = {}   # mã luồng -> (mốc cập nhật, kết quả)


def codex_scan(tid, stamp, nitems=80):
    """Lượt cuối, hoạt động, lời Codex cuối, yêu cầu cuối của người dùng: đọc từ thread_history (đã đánh chỉ mục
    theo luồng nên chỉ chạm vài chục dòng cuối dù file có thể rất lớn)."""
    c = _codex_scan_cache.get(tid)
    if c and c[0] == stamp:
        return c[1]
    res = {"turn": {}, "turns": 0, "timeline": [], "activity": "", "activityAt": 0, "lastText": "",
           "lastUser": "", "lastItemAt": 0, "lastAgentId": ""}
    con = codex_db("thread_history")
    if not con:
        return res
    try:
        t = con.execute("select turn_id, status, started_at, completed_at, duration_ms, error_json from thread_turns "
                        "where thread_id = ? order by rollout_ordinal desc limit 1", (tid,)).fetchone()
        if t:
            res["turn"] = dict(zip(("id", "status", "startedAt", "completedAt", "durationMs", "error"), t))
        res["turns"] = con.execute("select count(*) from thread_turns where thread_id = ?", (tid,)).fetchone()[0]
        items = con.execute("select item_type, item_json, created_at_ms from thread_items where thread_id = ? "
                            "order by rollout_ordinal desc limit ?", (tid, nitems)).fetchall()
        last_user = con.execute("select item_json, rollout_ordinal from thread_items where thread_id = ? and "
                                "item_type = 'userMessage' order by rollout_ordinal desc limit 1", (tid,)).fetchone()
        after = last_user[1] if last_user else -1
        says = con.execute("select item_json, item_id from thread_items where thread_id = ? and item_type = 'agentMessage' "
                           "and rollout_ordinal > ? order by rollout_ordinal desc limit 3", (tid, after)).fetchall()
    except sqlite3.Error as e:
        log(f"đọc lịch sử Codex {tid[:13]} lỗi: {e}")
        return res
    finally:
        con.close()
    for typ, raw, at in reversed(items):
        try:
            it = json.loads(raw)
        except ValueError:
            continue
        res["lastItemAt"] = max(res["lastItemAt"], at or 0)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((at or 0) / 1000)) + "Z"
        if typ == "userMessage":
            res["timeline"].append({"at": iso, "kind": "user", "text": codex_text(it)[:220]})
        elif typ == "agentMessage" and codex_text(it):
            res["timeline"].append({"at": iso, "kind": "say", "text": codex_text(it)[:220]})
        elif (d := describe_codex(it)):
            res["timeline"].append({"at": iso, "kind": "tool", "text": d})
            res["activity"], res["activityAt"] = d, at or 0
    if last_user:
        try:
            res["lastUser"] = codex_text(json.loads(last_user[0]))
        except ValueError:
            pass
    texts = []
    for raw, iid in says:
        try:
            texts.append(codex_text(json.loads(raw)))
        except ValueError:
            pass
    res["lastText"] = "\n\n".join(reversed([x for x in texts if x]))
    res["lastAgentId"] = says[0][1] if says else ""
    res["timeline"] = res["timeline"][-14:]
    _codex_scan_cache[tid] = (stamp, res)
    return res


def codex_status(row, scan):
    """lam: lượt cuối đang chạy · cho: lượt cuối lỗi · xem: Codex trả lời mà chưa đọc (app đánh dấu)
    · ranh: app ChatGPT đang mở · nghi: app tắt."""
    turn = scan["turn"]
    last = max(row.get("updated_at_ms") or 0, scan["lastItemAt"])
    if turn.get("status") == "inProgress" and time.time() * 1000 - last < CODEX_RUNNING_SEC * 1000:
        return "lam"
    if turn.get("status") == "failed":
        return "cho"
    if row.get("unread"):
        return "xem"
    return "ranh" if codex_app_open() else "nghi"


def codex_sub_state(rows):
    """Subagent của luồng Codex: đang chạy hay đã xong, theo lượt cuối của từng luồng con."""
    if not rows:
        return {}
    con = codex_db("thread_history")
    if not con:
        return {}
    ids = [r["id"] for r in rows]
    try:
        q = (f"select thread_id, status from thread_turns t where thread_id in ({','.join('?' * len(ids))}) and "
             "rollout_ordinal = (select max(rollout_ordinal) from thread_turns where thread_id = t.thread_id)")
        return dict(con.execute(q, ids).fetchall())
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def codex_subs(rows):
    now_ms = time.time() * 1000
    st = codex_sub_state(rows)
    out = [{"id": r["id"], "desc": " · ".join(x for x in (r.get("agent_nickname"), os.path.basename(r.get("agent_path") or ""))
                                              if x) or "Subagent Codex",
            "type": "Codex", "model": r.get("model") or "",
            "active": st.get(r["id"]) == "inProgress" and now_ms - (r["updated_at_ms"] or 0) < SUBAGENT_STALE_SEC * 1000,
            "at": r["updated_at_ms"] or 0} for r in rows]
    out.sort(key=lambda x: (not x["active"], -x["at"]))
    return out


def codex_sessions(hours, marks):
    """Bàn làm việc cho phiên Codex mở từ app desktop (không gồm subagent: chúng ngồi cạnh phiên cha)."""
    rows = codex_rows(time.time() * 1000 - hours * 3600_000)
    kids = {}
    for r in rows:
        if r["parentId"]:
            kids.setdefault(r["parentId"], []).append(r)
    out = []
    for r in rows:
        if r["parentId"]:
            continue
        sid = CODEX_PREFIX + r["id"]
        scan = codex_scan(r["id"], r["updated_at_ms"])
        st = codex_status(r, scan)
        subs, sub_active, sub_total = subagent_summary(codex_subs(kids.get(r["id"], [])))
        proj, sub = codex_project(r["cwd"])
        last_at = max(r["updated_at_ms"] or 0, scan["lastItemAt"])
        err = (scan["turn"].get("error") or "")[:200] if st == "cho" else ""
        mark = live_mark(marks.get(sid), {"lastActivityAt": last_at}, st)
        # Automation của app Codex (lịch tự chạy) gửi tin <heartbeat>: coi như routine.
        auto = re.search(r"<automation_id>([^<]+)</automation_id>", scan["lastUser"])
        out.append({
            "id": sid, "kind": "codex", "title": codex_title(r), "project": proj, "worktree": sub,
            "branch": r.get("git_branch") or "", "app": "CX", "email": "", "org": "Codex", "plan": "",
            "status": st, "live": codex_app_open(), "routine": auto.group(1) if auto else "",
            "activity": scan["activity"] if st == "lam" else "",
            "detail": ("Lượt cuối lỗi " + err) if err else scan["lastText"][:160],
            "needs": "", "lastActivityAt": last_at,
            "subs": subs, "subActive": sub_active, "subTotal": sub_total, "parent": None,
            "reply": None, "mark": mark["kind"] if mark else None, "cache": None,
        })
    return out


def codex_known(hours=MAX_WINDOW_HOURS):
    """Mã văn phòng -> mốc hoạt động, để đánh dấu xong/ẩn được cả phiên Codex."""
    return {CODEX_PREFIX + r["id"]: r["updated_at_ms"] or 0
            for r in codex_rows(time.time() * 1000 - hours * 3600_000) if not r["parentId"]}


def codex_detail(sid, want_summary=False, force=False):
    tid = sid[len(CODEX_PREFIX):]
    rows = codex_rows(ids=[tid])
    if not rows:
        return None
    r = rows[0]
    scan = codex_scan(tid, r["updated_at_ms"])
    st = codex_status(r, scan)
    last_at = max(r["updated_at_ms"] or 0, scan["lastItemAt"])
    proj, sub = codex_project(r["cwd"])
    pseudo = {"sessionId": sid, "lastAssistantUuid": scan["lastAgentId"], "lastActivityAt": last_at}
    cached = load_json(os.path.join(STATE_DIR, "tom-tat", sid + ".json"))
    sum_res = cached["result"] if cached and cached.get("key") == (scan["lastAgentId"] or str(last_at)) else None
    if want_summary:
        sum_res = summarize(pseudo, scan, force=force)
    if sum_res and not sum_res.get("error"):
        # Lời nhắc tóm tắt viết cho Claude nên hay gọi người làm là "Claude"; ở đây là Codex.
        fix = lambda x: re.sub(r"\bClaude\b", "Codex", x) if isinstance(x, str) else x  # noqa: E731
        sum_res = {**sum_res, "title": fix(sum_res.get("title")), "summary": fix(sum_res.get("summary")),
                  "decisions": [fix(x) for x in sum_res.get("decisions") or []]}
    con = codex_db("state")
    kid_rows = []
    if con:
        try:
            # Luồng con sinh ra sau luồng cha nên cập nhật sau lúc cha tạo: lọc theo chỉ mục updated_at_ms (lùi 1 ngày
            # cho chắc) trước khi so chuỗi, thay vì so chuỗi trên cả vạn luồng (~10 ms -> dưới 1 ms với luồng mới).
            like, since = f'%"parent_thread_id":"{tid}"%', (r.get("created_at_ms") or 0) - 86400_000
            kid_ids = [x[0] for x in con.execute(
                "select id from threads where updated_at_ms >= ? and source like ? "
                "union all select id from threads where updated_at_ms is null and source like ?", (since, like, like))]
        except sqlite3.Error:
            kid_ids = []
        finally:
            con.close()
        kid_rows = codex_rows(ids=kid_ids) if kid_ids else []
    subs = codex_subs(kid_rows)
    turn = scan["turn"]
    return {
        "id": sid, "kind": "codex", "cli": tid, "title": codex_title(r), "status": st, "live": codex_app_open(),
        "app": "CX", "mark": (live_mark(load_marks().get(sid), {"lastActivityAt": last_at}, st) or {}).get("kind"),
        "email": "", "org": "Codex", "orgUuid": "", "plan": "",
        "project": proj, "projectName": project_config().get(proj, {}).get("ten", proj),
        "worktree": sub, "branch": r.get("git_branch") or "", "cwd": r["cwd"],
        "model": " · ".join(x for x in (r.get("model"), r.get("reasoning_effort")) if x),
        "routine": (m.group(1) if (m := re.search(r"<automation_id>([^<]+)</automation_id>", scan["lastUser"])) else ""),
        "appSummary": {"status_detail": ("Lượt cuối lỗi: " + (turn.get("error") or "")[:300]) if st == "cho" else ""},
        "aiSummary": sum_res, "lastText": scan["lastText"][-4000:], "lastUser": scan["lastUser"][-1500:],
        "timeline": scan["timeline"], "prs": [],
        "subs": subs[:30], "subActive": sum(1 for x in subs if x["active"]), "subTotal": len(subs),
        "parent": None, "children": [], "lastActivityAt": last_at, "reply": None, "compact": None,
        "codex": {"tokens": r.get("tokens_used") or 0, "turns": scan["turns"], "turnStatus": turn.get("status"),
                  "turnMs": turn.get("durationMs"), "unread": r["unread"]},
        "usage": None,
        "canSummarize": summary_enabled(),
    }


def open_codex(sid):
    url = "codex://threads/" + urllib.parse.quote(sid[len(CODEX_PREFIX):])
    r = subprocess.run(["open", url], capture_output=True, text=True)
    log(f"mở Codex {sid[:20]}: {'ok' if r.returncode == 0 else r.stderr.strip()[:200]}")
    return {"ok": r.returncode == 0, "app": "Codex", "error": r.stderr.strip()[:300]}


# ---------- dự án (Beta): phiên điều phối và luồng việc trên đám mây ----------
#
# Mục "Dự án" của app Claude: mỗi dự án có một phiên điều phối và các luồng việc, đều chạy trên máy ảo đám mây của
# Anthropic nên trên máy không có nhật ký nào. Văn phòng hỏi máy chủ theo đúng các đường app dùng (/v1/code/channels,
# /v1/code/sessions) bằng khoá OAuth của chính các phiên app đang mở: lấy từ biến môi trường của tiến trình, chỉ giữ
# trong bộ nhớ, không ghi ra đĩa. Hỏi trong luồng nền, tối đa mỗi CLOUD_EVERY giây và chỉ khi trang đang hỏi trạng thái.

CLOUD_API = "https://api.anthropic.com"
CLOUD_PREFIX = "cloud_"       # mã bàn = cloud_<mã phiên trên máy chủ>
CLOUD_EVERY = 45
CLOUD_KID_EVERY = 300         # phiên của luồng việc: hỏi lại khi trạng thái luồng đổi hoặc sau 5 phút
CLOUD_PAUSE = 300             # bị giới hạn lượt (429) thì nghỉ 5 phút
CLOUD_SECTIONS = ("needs_input", "failed", "working", "idle")   # luồng đã xong hoặc lưu trữ thì thôi hiện
CLOUD_ICONS = {"Bullhorn": "📣", "DNA": "🧬", "Eye": "👁️", "GraduationCap": "🎓", "Rocket": "🚀", "Lightbulb": "💡",
               "Code": "💻", "Book": "📚", "Heart": "❤️", "Star": "⭐", "Briefcase": "💼", "Chart": "📊"}
_cloud = {"at": 0.0, "busy": False, "pause": 0.0, "desks": [], "rooms": {}, "info": {}, "threads": {}, "kids": {}}
_cloud_lock = threading.Lock()


def cloud_tokens():
    """(mã tài khoản, mã tổ chức) -> [(khoá OAuth, số bản app)…] mới nhất trước, từ các phiên app đang mở. Chỉ lấy phiên
    mà tổ chức trong khoá trùng tổ chức của bản ghi phiên (phiên cũ có thể còn cầm khoá của tổ chức trước)."""
    by_sid = {}
    for r in registry():
        by_sid.setdefault(r["sessionId"], []).append(r)
    lives = sorted({id(d): d for d in live_records().values()}.values(), key=lambda d: -(d.get("startedAt") or 0))
    envs = proc_envs({d.get("pid") for d in lives})
    out = {}
    for d in lives:
        recs = by_sid.get(d.get("hostSessionId") or "") or []
        rec = next((r for r in recs if r.get("cliSessionId") == d.get("sessionId")), recs[0] if recs else None)
        if not rec:
            continue
        env = dict(re.findall(r"\b(CLAUDE_CODE_(?:OAUTH_TOKEN|ORGANIZATION_UUID|ACCOUNT_UUID))=(\S+)", envs.get(str(d["pid"]), "")))
        tok, org = env.get("CLAUDE_CODE_OAUTH_TOKEN"), env.get("CLAUDE_CODE_ORGANIZATION_UUID")
        num = app_number(rec["appDir"])
        if tok and num and org == rec["orgUuid"]:
            cands = out.setdefault((env.get("CLAUDE_CODE_ACCOUNT_UUID") or rec["accountUuid"], org), [])
            if all(t != tok for t, _ in cands):
                cands.append((tok, num))
    return out


def cloud_get(path, tok, org):
    req = urllib.request.Request(CLOUD_API + path, headers={
        "Authorization": f"Bearer {tok}", "anthropic-version": "2023-06-01", "anthropic-beta": "ccr-byoc-2025-07-29",
        "x-organization-uuid": org, "User-Agent": "van-phong-claude"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def cloud_session(sid, tok, org):
    return (cloud_get(f"/v1/code/sessions/{urllib.parse.quote(sid)}", tok, org) or {}).get("response_shape") or {}


def cloud_status(s, section=""):
    """lam: đang chạy · cho: chờ duyệt, bị chặn, có việc cần làm, luồng chờ trả lời hay lỗi · xem: xong, cần xem
    · ranh: còn lại (phiên trên đám mây lúc nào cũng mở được)."""
    pts = s.get("post_turn_summary") or {}
    if section == "working" or s.get("worker_status") == "running":
        return "lam"
    if section in ("needs_input", "failed") or s.get("requires_action_details_list") \
            or s.get("status_bucket") in ("blocked", "needs_input") or (pts.get("needs_action") or "").strip():
        return "cho"
    return "xem" if s.get("status_bucket") == "review_ready" else "ranh"


def cloud_needs(s, section, st):
    if st != "cho":
        return ""
    if s.get("requires_action_details_list"):
        return "Đang chờ bạn duyệt một thao tác trong app"
    need = ((s.get("post_turn_summary") or {}).get("needs_action") or "").strip()
    if need:
        return need
    return "Luồng việc bị lỗi" if section == "failed" else "Đang chờ bạn trả lời trong dự án"


def cloud_desk(s, base, title, room, parent, section=""):
    pts = s.get("post_turn_summary") or {}
    st = cloud_status(s, section)
    return {**base, "id": CLOUD_PREFIX + s["id"], "kind": "cloud", "title": (title or "Phiên dự án")[:90], "project": room,
            "worktree": "", "branch": "", "status": st, "live": True, "routine": "", "activity": "",
            "detail": pts.get("status_detail") or "", "needs": cloud_needs(s, section, st),
            "lastActivityAt": iso_ms(s.get("last_event_at") or s.get("updated_at")),
            "subs": [], "subActive": 0, "subTotal": 0, "parent": parent, "reply": None, "mark": None, "cache": None}


def cloud_refresh():
    try:
        desks, rooms, info, seen_kids = [], {}, {}, set()
        for (acct, org), cands in cloud_tokens().items():
            chans = tok = num = None
            for t, n in cands:
                try:
                    chans, tok, num = cloud_get("/v1/code/channels?scope=all", t, org).get("data") or [], t, n
                    break
                except urllib.error.HTTPError as e:
                    if e.code not in (401, 403):      # khoá hết hạn hay sai tổ chức: thử khoá của phiên khác
                        raise
            if chans is None:
                continue
            email = _identity["accounts"].get(acct, "")
            base = {"app": num, "email": email, "org": org_label(org, email), "plan": plan_label(org)}
            for ch in chans:
                if ch.get("archived_at") or ch.get("disabled_at") or not ch.get("channel_session_id"):
                    continue
                room = f"du-an-{ch['id']}"
                rooms[room] = {"ten": ch.get("name") or "Dự án", "bieu_tuong": CLOUD_ICONS.get(ch.get("icon"), "🗂️")}
                coord = cloud_session(ch["channel_session_id"], tok, org)
                if not coord.get("id") or coord.get("status") == "archived":
                    continue
                cid = CLOUD_PREFIX + coord["id"]
                desks.append(cloud_desk(coord, base, f"Điều phối · {ch.get('name') or 'Dự án'}", room, None))
                info[cid] = {"desk": desks[-1], "session": coord, "channel": ch, "org": org, "thread": None, "section": ""}
                key = (ch.get("last_message_at"), ch.get("updated_at"))
                th = _cloud["threads"].get(ch["id"])
                if not th or th[0] != key or time.time() - th[1] > CLOUD_KID_EVERY:
                    th = _cloud["threads"][ch["id"]] = (key, time.time(), cloud_get(
                        f"/v1/code/channels/{urllib.parse.quote(ch['id'])}/threads", tok, org).get("sections") or [])
                for sec in th[2]:
                    if sec.get("status") not in CLOUD_SECTIONS:
                        continue
                    for t in sec.get("data") or []:
                        for b in t.get("bound_sessions") or []:
                            if not b.get("id") or b["id"] in seen_kids:
                                continue
                            seen_kids.add(b["id"])
                            k = _cloud["kids"].get(b["id"])
                            if not k or k[0] != b.get("status_bucket") or time.time() - k[1] > CLOUD_KID_EVERY:
                                k = _cloud["kids"][b["id"]] = (b.get("status_bucket"), time.time(), cloud_session(b["id"], tok, org))
                            ks = k[2]
                            # Luồng chạy trên chính máy này (bridge) đã có bàn riêng từ bản ghi phiên của app.
                            if not ks.get("id") or ks.get("environment_kind") == "bridge" or ks.get("status") == "archived":
                                continue
                            title = b.get("title") or ks.get("title") or (t.get("body") or "").split("\n")[0]
                            desks.append(cloud_desk(ks, base, title, room, cid, sec["status"]))
                            info[desks[-1]["id"]] = {"desk": desks[-1], "session": ks, "channel": ch, "org": org,
                                                     "thread": t, "section": sec["status"]}
        _cloud.update(desks=desks, rooms=rooms, info=info)
        _state_cache.clear()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _cloud["pause"] = time.time() + CLOUD_PAUSE
        log(f"dự án trên đám mây lỗi HTTP {e.code}")
    except (OSError, ValueError) as e:
        log(f"dự án trên đám mây lỗi: {type(e).__name__}: {e}")
    finally:
        _cloud["busy"] = False


def cloud_kick():
    """Gọi mỗi lần trang hỏi trạng thái: tới hạn thì làm mới trong luồng nền, trang không phải chờ mạng."""
    now = time.time()
    with _cloud_lock:
        if _cloud["busy"] or now - _cloud["at"] < CLOUD_EVERY or now < _cloud["pause"]:
            return
        _cloud.update(busy=True, at=now)
    threading.Thread(target=cloud_refresh, daemon=True).start()


def cloud_sessions(hours, marks):
    """Bàn của dự án: phiên điều phối luôn hiện (như phiên đang mở), luồng việc hiện khi đang chạy, chờ bạn
    hoặc có hoạt động trong khung giờ."""
    now_ms = time.time() * 1000
    out = []
    for d in _cloud["desks"]:
        if d["parent"] and d["status"] not in ("lam", "cho") and now_ms - d["lastActivityAt"] > hours * 3600_000:
            continue
        mark = live_mark(marks.get(d["id"]), d, d["status"])
        out.append({**d, "mark": mark["kind"] if mark else None})
    return out


def cloud_known():
    return {d["id"]: d["lastActivityAt"] for d in _cloud["desks"]}


def cloud_detail(sid):
    inf = _cloud["info"].get(sid)
    if not inf:
        return None
    d, s, ch = inf["desk"], inf["session"], inf["channel"]
    parent = _cloud["info"].get(d["parent"]) if d["parent"] else None
    kids = [x for x in _cloud["desks"] if x["parent"] == sid]
    mark = live_mark(load_marks().get(sid), d, d["status"])
    return {
        "id": sid, "kind": "cloud", "cli": s.get("id"), "title": d["title"], "status": d["status"], "live": True,
        "app": d["app"], "mark": mark["kind"] if mark else None, "email": d["email"], "org": d["org"],
        "orgUuid": inf["org"], "plan": d["plan"], "project": d["project"], "projectName": ch.get("name") or "",
        "worktree": "", "branch": "", "cwd": "", "model": (s.get("config") or {}).get("model") or "", "routine": "",
        "appSummary": {"status_detail": d["detail"], "needs_action": d["needs"]}, "aiSummary": None,
        "lastText": ((s.get("post_turn_summary") or {}).get("recent_action") or ""),
        "lastUser": ((inf.get("thread") or {}).get("body") or "")[-1500:],
        "timeline": [], "prs": [], "subs": [], "subActive": 0, "subTotal": 0,
        "parent": {"id": d["parent"], "title": parent["desk"]["title"]} if parent else None,
        "children": [{"id": k["id"], "title": k["title"], "archived": False} for k in kids],
        "lastActivityAt": d["lastActivityAt"], "reply": None, "compact": None, "usage": None, "canSummarize": False,
        "ask": "", "askActions": [], "waiting": "",
        "cloud": {"url": s.get("session_url") or "", "channel": ch.get("id"), "section": inf.get("section") or "",
                  "context": (s.get("external_metadata") or {}).get("context_usage") or {}},
    }


def open_cloud(sid):
    inf = _cloud["info"].get(sid)
    if not inf:
        return {"ok": False, "error": "Không thấy phiên dự án (văn phòng chưa tải xong danh sách dự án)"}
    url = "claude://claude.ai/epitaxy/project/" + urllib.parse.quote(inf["channel"]["id"])
    return open_link_in_app(inf["desk"]["app"], url, sid)


# ---------- trạng thái ----------

def status_of(rec, live, ask=""):
    """lam (đang chạy) · cho (chờ người dùng) · xem (xong, cần xem lại) · ranh (mở, rảnh) · nghi (đã đóng).
    cho gồm: app đang mở hộp hỏi chờ bấm (phiên "waiting": xin quyền, câu hỏi, thẻ mở phiên mới…), lượt cuối bị chặn
    hoặc có việc cần làm theo tóm tắt của app, và lượt cuối kết bằng câu hỏi ở hộp "bước tiếp" (ask)."""
    if live and live.get("status") == "busy":
        return "lam"
    if live and live.get("status") == "waiting":
        return "cho"
    s = rec.get("postTurnSummary") or {}
    cat = s.get("status_category")
    if cat == "blocked" or (s.get("needs_action") or "").strip() or ask:
        return "cho"
    if cat == "review_ready":
        return "xem"
    return "ranh" if live else "nghi"


WAITING_TEXT = "App đang mở hộp hỏi, chờ bạn bấm"


def needs_of(rec, live, ask=""):
    """Câu "cần bạn làm" đi kèm trạng thái cho: hộp hỏi đang mở, rồi việc cần làm theo tóm tắt app, rồi câu hỏi cuối lượt."""
    if live and live.get("status") == "waiting":
        return WAITING_TEXT
    return ((rec.get("postTurnSummary") or {}).get("needs_action") or "").strip() or ask


_state_cache = {}  # số giờ -> (lúc dựng, dữ liệu)
_state_lock = threading.Lock()
_marks_lock = threading.Lock()


def load_marks():
    return load_json(MARKS_FILE, {}) or {}


def set_marks(ids, kind):
    """kind = "xong" | "an" | None (bỏ đánh dấu). Ghi mốc hoạt động hiện tại để biết khi nào có việc mới."""
    seen = {office_id(r): r.get("lastActivityAt") or 0 for r in registry()}
    seen.update(codex_known())
    seen.update(cloud_known())
    with _marks_lock:
        marks = load_marks()
        for sid in ids:
            if kind is None:
                marks.pop(sid, None)
            elif sid in seen:
                marks[sid] = {"kind": kind, "at": int(time.time() * 1000), "seen": seen[sid]}
        # Bỏ dấu của phiên đã biến mất khỏi app hoặc đã cũ quá 30 ngày.
        cutoff = time.time() * 1000 - 30 * 86400_000
        marks = {k: v for k, v in marks.items() if k in seen and v.get("at", 0) > cutoff}
        save_json(MARKS_FILE, marks)
    _state_cache.clear()
    return marks
_replies = {}   # local_id -> {"at", "state": dang-gui|da-gui|loi, "text"}


def live_mark(mark, rec, status):
    """Có việc mới sau lúc đánh dấu (hoặc đang chạy lại) thì dấu hết hiệu lực, phiên hiện lại."""
    if not mark or status == "lam" or (rec.get("lastActivityAt") or 0) > mark.get("seen", 0) + MARK_GRACE_MS:
        return None
    return mark


def build_state(hours=WINDOW_HOURS):
    if time.time() - _identity["learned_at"] > 300:
        learn_identity()
    live = live_records()
    marks = load_marks()
    cfg = project_config()
    now_ms = time.time() * 1000
    sessions = []
    for rec in registry():
        if rec.get("isArchived"):
            continue
        sid = office_id(rec)
        lv = live_of(rec, live)
        recent = now_ms - (rec.get("lastActivityAt") or 0) < hours * 3600_000
        if not lv and not recent:
            continue
        num = app_number(rec["appDir"])
        if not num:
            continue
        tpath = transcript_of(rec.get("cliSessionId"))
        scan = scan_cached(tpath)
        subs, sub_active, sub_total = subagent_summary(subagents_of(tpath))
        proj, sub = project_of(rec.get("originCwd") or rec.get("cwd"))
        email = _identity["accounts"].get(rec["accountUuid"], "")
        st = status_of(rec, lv, scan["ask"])
        summary = rec.get("postTurnSummary") or {}
        # Dấu cũ đặt khi hai bản sao còn chung một mã thì vẫn giữ cho cả hai tới lần đánh dấu sau.
        mark = live_mark(marks.get(sid) or marks.get(rec["sessionId"]), rec, st)
        sessions.append({
            "id": sid,
            "title": rec.get("title") or scan["aiTitle"] or os.path.basename(rec.get("cwd") or ""),
            "project": proj,
            "worktree": sub,
            "branch": scan["branch"],
            "app": num,
            "email": email,
            "org": org_label(rec["orgUuid"], email),
            "plan": plan_label(rec["orgUuid"]),
            "status": st,
            "live": bool(lv),
            "routine": rec.get("scheduledTaskId") or "",
            "activity": scan["activity"] if st == "lam" else "",
            "detail": summary.get("status_detail", ""),
            "needs": needs_of(rec, lv, scan["ask"]),
            "lastActivityAt": max(rec.get("lastActivityAt") or 0, scan["activityAt"]),
            "subs": subs,
            "subActive": sub_active,
            "subTotal": sub_total,
            "parent": parent_id(rec),
            "reply": _replies.get(sid),
            "mark": mark["kind"] if mark else None,
            "cache": scan["cache"],
        })
    try:
        sessions += codex_sessions(hours, marks)
    except Exception as e:  # noqa: BLE001  (Codex hỏng không được kéo sập cả văn phòng)
        log(f"phiên Codex lỗi: {type(e).__name__}: {e}")
    sessions += cloud_sessions(hours, marks)
    for k, v in _cloud["rooms"].items():
        cfg.setdefault(k, v)
    projects = {}
    for s in sessions:
        p = projects.setdefault(s["project"], {"key": s["project"], "sessions": [],
                                               **cfg.get(s["project"], {"ten": s["project"], "bieu_tuong": "📁"})})
        p["sessions"].append(s)
    order = {"cho": 0, "lam": 1, "xem": 2, "ranh": 3, "nghi": 4}
    for p in projects.values():
        p["sessions"].sort(key=lambda s: (order[s["status"]], -s["lastActivityAt"]))
        p["score"] = min(order[s["status"]] for s in p["sessions"])
    plist = sorted(projects.values(), key=lambda p: (p["score"], -len(p["sessions"])))
    counts = {k: sum(1 for s in sessions if s["status"] == k and not s["mark"]) for k in order}
    apps = sorted({(s["app"], s["email"], s["org"]) for s in sessions})  # nick theo bản app
    return {"at": int(now_ms), "hours": hours, "projects": plist, "counts": counts,
            "apps": [{"app": a, "email": e, "org": o} for a, e, o in apps],
            "teamMau": cau_hinh().get("mauTeam") or {}}


def get_state(hours=WINDOW_HOURS):
    hours = max(1, min(int(hours), MAX_WINDOW_HOURS))
    cloud_kick()
    with _state_lock:
        c = _state_cache.get(hours)
        if not c or time.time() - c[0] > 1.5:
            c = _state_cache[hours] = (time.time(), build_state(hours))
        return c[1]


DEFAULT_VIEWS = [
    {"id": "cho-ban", "ten": "Chờ bạn", "loc": {"status": ["cho"]}},
    {"id": "dang-chay", "ten": "Đang chạy", "loc": {"status": ["lam"]}},
    {"id": "toan-canh", "ten": "Toàn cảnh 24 giờ", "loc": {}, "macDinh": True},
    {"id": "routine", "ten": "Routine", "loc": {"routine": "chi", "gio": 72}},
]


def load_views():
    v = load_json(VIEWS_FILE)
    return v if isinstance(v, list) else DEFAULT_VIEWS


def find_session(sid):
    registry()
    return _registry_ids["by"].get(sid)


# ---------- tóm tắt (tuỳ chọn, khai trong cấu hình mục "tomTat") ----------
#
# Hai cách khai:
#   {"tomTat": {"lenh": ["claude", "-p", "--model", "haiku"]}}
#       chạy lệnh, đưa lời nhắc vào stdin, đọc JSON từ stdout (tốn một lượt gọi model mỗi lần tóm tắt)
#   {"tomTat": {"hook": "~/duong-dan/module.py"}}
#       nạp một module Python có hàm tom_tat(user, last) -> (dict | None, tên bộ tóm tắt)
# Kết quả cần các khoá: title, summary, decisions (danh sách), needs_reply. Đệm theo lời cuối của phiên,
# bấm lại không tốn thêm.

SUMMARY_PROMPT = """Bạn tóm tắt lượt làm việc vừa xong của một trợ lý lập trình cho người dùng đọc lướt trên điện thoại.

Yêu cầu gần nhất của người dùng:
<user>
{user}
</user>

Trả lời cuối của trợ lý:
<last>
{last}
</last>

Chỉ trả về một đối tượng JSON, không kèm chữ nào khác:
{"title": "tiêu đề ngắn dưới 60 ký tự", "summary": "2-3 câu tiếng Việt đời thường: đã làm gì, kết quả ra sao",
 "decisions": ["mỗi việc người dùng cần quyết hoặc trả lời, bỏ trống nếu không có"], "needs_reply": true}"""


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_summary_hook = {}


def summary_enabled():
    t = cau_hinh().get("tomTat") or {}
    return bool(t.get("lenh") or t.get("hook"))


def parse_json_loose(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def run_summarizer(user, last):
    """(kết quả dict | None, tên bộ tóm tắt)."""
    t = cau_hinh().get("tomTat") or {}
    if t.get("hook"):
        path = os.path.expanduser(t["hook"])
        if path not in _summary_hook:
            import sys
            sys.path.insert(0, os.path.dirname(path))   # module được phép import các file cạnh nó
            _summary_hook[path] = load_module("vp_tom_tat", path)
        return _summary_hook[path].tom_tat(user, last)
    cmd = [os.path.expanduser(x) for x in t["lenh"]]
    prompt = SUMMARY_PROMPT.replace("{user}", user).replace("{last}", last)
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=180)
    return parse_json_loose(r.stdout), t.get("ten") or os.path.basename(cmd[0])


def summarize(rec, scan, force=False):
    if not summary_enabled():
        return {"error": "Chưa khai bộ tóm tắt trong ~/.claude/van-phong/cau-hinh.json (mục tomTat)"}
    cache_file = os.path.join(STATE_DIR, "tom-tat", (office_id(rec) if rec.get("file") else rec["sessionId"]) + ".json")
    key = rec.get("lastAssistantUuid") or str(rec.get("lastActivityAt"))
    cached = load_json(cache_file)
    if cached and cached.get("key") == key and not force:
        return cached["result"]
    if not scan["lastText"].strip():
        return {"title": "", "summary": "Phiên chưa có lời nhắn nào của trợ lý.", "decisions": [], "needs_reply": False}
    try:
        res, via = run_summarizer(scan["lastUser"][-2000:], scan["lastText"][-8000:])
    except Exception as e:  # noqa: BLE001
        return {"error": f"Bộ tóm tắt lỗi: {type(e).__name__}: {e}"}
    if not isinstance(res, dict):
        return {"error": f"Bộ tóm tắt {via} không trả JSON"}
    res["via"] = via
    save_json(cache_file, {"key": key, "at": time.time(), "result": res})
    return res


def session_detail(sid, want_summary=False, force=False):
    if sid.startswith(CODEX_PREFIX):
        return codex_detail(sid, want_summary, force)
    if sid.startswith(CLOUD_PREFIX):
        return cloud_detail(sid)
    rec = find_session(sid)
    if not rec:
        return None
    live = live_records()
    lv = live_of(rec, live)
    tpath = transcript_of(rec.get("cliSessionId"))
    scan = scan_cached(tpath, 400_000)
    proj, sub = project_of(rec.get("originCwd") or rec.get("cwd"))
    email = _identity["accounts"].get(rec["accountUuid"], "")
    cached = load_json(os.path.join(STATE_DIR, "tom-tat", sid + ".json"))
    key = rec.get("lastAssistantUuid") or str(rec.get("lastActivityAt"))
    sum_res = cached["result"] if cached and cached.get("key") == key else None
    if want_summary:
        sum_res = summarize(rec, scan, force=force)
    all_subs = subagents_of(tpath)
    regs = registry()
    titles = {office_id(r): r.get("title") or "" for r in regs}
    parent = parent_id(rec)
    children = [{"id": office_id(r), "title": r.get("title") or "", "archived": bool(r.get("isArchived"))}
                for r in regs if (r.get("spawnedFrom") or {}).get("sessionId") == rec["sessionId"] and parent_id(r) == sid]
    return {
        "id": sid, "local": rec["sessionId"], "cli": rec.get("cliSessionId"), "title": rec.get("title") or scan["aiTitle"],
        "status": status_of(rec, lv, scan["ask"]), "live": bool(lv), "app": app_number(rec["appDir"]),
        "mark": (live_mark(load_marks().get(sid), rec, status_of(rec, lv, scan["ask"])) or {}).get("kind"),
        "ask": scan["ask"], "askActions": scan["askActions"],
        "waiting": WAITING_TEXT if lv and lv.get("status") == "waiting" else "",
        "email": email, "org": org_label(rec["orgUuid"], email), "orgUuid": rec["orgUuid"],
        "plan": plan_label(rec["orgUuid"]),
        "project": proj, "projectName": project_config().get(proj, {}).get("ten", proj),
        "worktree": sub, "branch": scan["branch"], "cwd": rec.get("cwd"),
        "model": rec.get("model"), "routine": rec.get("scheduledTaskId") or "",
        "appSummary": rec.get("postTurnSummary") or {}, "aiSummary": sum_res,
        "lastText": scan["lastText"][-4000:], "lastUser": scan["lastUser"][-1500:],
        "timeline": scan["timeline"], "prs": scan["prs"][-5:],
        "subs": all_subs[:30], "subActive": sum(1 for x in all_subs if x["active"]), "subTotal": len(all_subs),
        "parent": {"id": parent, "title": titles.get(parent, "")} if parent else None,
        "children": children,
        "lastActivityAt": rec.get("lastActivityAt"),
        "reply": _replies.get(sid),
        "usage": session_usage(tpath),
        "compact": _compacts.get(sid),
        "canSummarize": summary_enabled(),
    }


# ---------- trả lời phiên ----------

def send_reply(sid, text):
    rec = find_session(sid)
    if not rec:
        return {"ok": False, "error": "Không tìm thấy phiên"}
    cli = rec.get("cliSessionId")
    if (_replies.get(sid) or {}).get("state") == "dang-chay":
        return {"ok": False, "error": "Phiên đang chạy tiếp lượt trước, đợi xong rồi gửi lại"}
    prompt = f"{cau_hinh().get('tienToTraLoi') or DEFAULT_REPLY_PREFIX}\n{text}"
    live = live_records()
    lv = live_of(rec, live)
    if lv and lv.get("messagingSocketPath") and os.path.exists(lv["messagingSocketPath"]):
        ok = deliver_live(lv, prompt)
        _replies[sid] = {"at": int(time.time() * 1000), "state": "da-gui" if ok else "loi", "text": text[:200],
                         "via": "phiên đang mở"}
        log(f"trả lời {sid[:14]} qua ổ cắm: {'ok' if ok else 'lỗi'}")
        if ok:
            return {"ok": True, "via": "live"}
        return {"ok": False, "error": "Gửi vào phiên đang mở không được (xem ~/.claude/van-phong/log.txt)"}
    # Phiên đã đóng: chạy tiếp bằng CLI.
    _replies[sid] = {"at": int(time.time() * 1000), "state": "dang-chay", "text": text[:200], "via": "chạy tiếp bằng CLI"}
    threading.Thread(target=resume_session, args=(sid, rec, prompt), daemon=True).start()
    return {"ok": True, "via": "resume"}


def deliver_live(lv, prompt):
    """Đẩy tin vào phiên đang mở qua ổ cắm nhắn tin giữa các phiên của Claude Code (phiên nhận cần
    bật crossSessionInbound: "accept"). Khoá xác thực lấy ở ~/.claude/sessions/<pid>.*.key."""
    import socket
    import uuid
    keys = glob.glob(os.path.join(LIVE_DIR, f"{lv['pid']}.*.key"))
    if not keys:
        return False
    tok = (load_json(keys[0]) or {}).get("peerToken")
    frames = [{"type": "auth", "token": tok},
              {"type": "user", "message": {"content": prompt}, "from": "van-phong",
               "from_name": "Văn phòng Claude", "uuid": str(uuid.uuid4()), "msg_id": uuid.uuid4().hex}]
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    try:
        s.connect(lv["messagingSocketPath"])
        s.sendall("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames).encode())
        s.shutdown(socket.SHUT_WR)
        try:
            s.recv(1000)
        except OSError:
            pass
        return True
    except OSError as e:
        log(f"ổ cắm lỗi: {e}")
        return False
    finally:
        s.close()


def resume_session(sid, rec, prompt):
    cwd = rec.get("cwd") if os.path.isdir(rec.get("cwd") or "") else HOME
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    # Chạy không người trông nên mặc định chỉ dùng quyền thường; muốn rộng hơn thì khai "quyenTraLoi".
    mode = cau_hinh().get("quyenTraLoi") or "default"
    try:
        r = subprocess.run([claude_bin(), "--resume", rec["cliSessionId"], "-p", prompt, "--permission-mode", mode],
                           cwd=cwd, env=env, capture_output=True, text=True, timeout=3 * 3600)
        ok = r.returncode == 0
        err = "" if ok else (r.stderr or r.stdout or "")[-400:]
    except subprocess.TimeoutExpired:
        ok, err = False, "Chạy quá 3 tiếng, đã dừng"
    _replies[sid] = {"at": int(time.time() * 1000), "state": "da-xong" if ok else "loi",
                     "text": (_replies.get(sid) or {}).get("text", ""), "via": "chạy tiếp bằng CLI", "error": err}
    log(f"chạy tiếp {sid[:14]}: {'ok' if ok else 'lỗi ' + err[:200]}")


# ---------- quota mọi nick (bộ gom collect.py) ----------

CSWAP_PY = os.path.join(HOME, ".local", "share", "uv", "tools", "claude-swap", "bin", "python")
_quota = {"at": 0, "data": None, "busy": False}
_quota_lock = threading.Lock()


def collect_script():
    p = os.path.join(HERE, "collect.py")
    return p if os.path.isfile(p) else None


def refresh_quota():
    script = collect_script()
    if not script:
        return {"error": "Không thấy bộ gom quota collect.py"}
    py = CSWAP_PY if os.path.isfile(CSWAP_PY) else "python3"
    try:
        r = subprocess.run([py, "-I", script, "--fresh-sec", "600"], capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, ValueError) as e:
        return {"error": f"Bộ gom quota lỗi: {type(e).__name__}"}
    for p in data.get("pools", []):
        p["team"] = org_label(p.get("orgUuid", "")) if p.get("orgUuid") else (p.get("orgName") or "?")
        if p["team"].startswith("Team ") and p.get("orgName") and not p["orgName"].startswith("Team "):
            p["team"] = "Cá nhân" if p["orgName"].endswith("'s Organization") else p["orgName"]
        p["plan"] = plan_label(p.get("orgUuid", ""))
    return data


def get_quota(force=False):
    """Đệm 60 giây; bộ gom tự giãn nhịp hỏi Anthropic (10 phút/túi) nên gọi lại không tốn gì thêm."""
    with _quota_lock:
        if _quota["data"] is not None and not force and time.time() - _quota["at"] < 60:
            return _quota["data"]
    data = refresh_quota()
    with _quota_lock:
        _quota.update(at=time.time(), data=data)
    return data


# ---------- compact ----------

_compacts = {}  # local_id -> {"at", "state": dang-chay|da-xong|loi, "text"}


def compact_session(sid):
    """Phiên đã đóng: chạy `/compact` bằng CLI (CLI nhận lệnh gạch chéo ở chế độ -p).
    Phiên đang mở thì không đẩy lệnh vào được từ ngoài: trang tự chép sẵn /compact và mở phiên trong app."""
    rec = find_session(sid)
    if not rec or not rec.get("cliSessionId"):
        return {"ok": False, "error": "Không tìm thấy phiên"}
    live = live_records()
    if live_of(rec, live):
        return {"ok": False, "live": True}
    if (_compacts.get(sid) or {}).get("state") == "dang-chay":
        return {"ok": False, "error": "Đang compact rồi, đợi xong"}
    _compacts[sid] = {"at": int(time.time() * 1000), "state": "dang-chay", "text": ""}
    threading.Thread(target=run_compact, args=(sid, rec), daemon=True).start()
    return {"ok": True}


def run_compact(sid, rec):
    cwd = rec.get("cwd") if os.path.isdir(rec.get("cwd") or "") else HOME
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    try:
        r = subprocess.run([claude_bin(), "--resume", rec["cliSessionId"],
                            "-p", "/compact", "--output-format", "json"],
                           cwd=cwd, env=env, capture_output=True, text=True, timeout=1800)
        text = ""
        try:
            d = json.loads(r.stdout)
            d = d[-1] if isinstance(d, list) else d
            text = str(d.get("result") or "")
        except (ValueError, AttributeError, IndexError):
            text = (r.stderr or r.stdout or "")[-300:]
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        ok, text = False, "Quá 30 phút, đã dừng"
    _compacts[sid] = {"at": int(time.time() * 1000), "state": "da-xong" if ok else "loi", "text": text[:400]}
    log(f"compact {sid[:14]}: {'ok' if ok else 'lỗi'} {text[:120]!r}")


# ---------- mở đúng bản app + đúng phiên ----------

def app_pid(num):
    """PID tiến trình gốc của bản app. Bản chính chạy không có --user-data-dir (thư mục "Claude"); bản phụ
    chạy chung Claude.app với --user-data-dir=<.../Claude-NN>, nên nhận theo tên thư mục đó."""
    out = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not re.search(r"Claude\.app/Contents/MacOS/Claude( |$)", cmd):
            continue
        m = re.search(r"--user-data-dir=(.+?)(?= --|$)", cmd)
        if app_number(os.path.basename(m.group(1).rstrip("/")) if m else "Claude") == num:
            return int(pid)
    return None


def app_launcher(num):
    """Cách bật bản app đang tắt: khai trong cấu hình mục "moApp" ({"03": "~/Applications/Claude-03.app"});
    không khai thì bản 01 mở Claude.app, bản khác tìm ~/Applications/Claude-NN.app."""
    path = (cau_hinh().get("moApp") or {}).get(num)
    if path:
        return os.path.expanduser(path)
    if num == "01":
        return "/Applications/Claude.app"
    return os.path.join(HOME, "Applications", f"Claude-{num}.app")


def open_in_app(sid):
    if sid.startswith(CODEX_PREFIX):
        return open_codex(sid)
    if sid.startswith(CLOUD_PREFIX):
        return open_cloud(sid)
    rec = find_session(sid)
    if not rec:
        return {"ok": False, "error": "Không tìm thấy phiên"}
    url = "claude://code/continue?session=" + urllib.parse.quote(rec["sessionId"])
    return open_link_in_app(app_number(rec["appDir"]), url, sid)


def open_link_in_app(num, url, label):
    """Bật bản app nếu đang tắt, gửi link claude:// tới đúng tiến trình đó rồi đưa cửa sổ lên trước."""
    pid = app_pid(num)
    if not pid:
        launcher = app_launcher(num)
        if not os.path.isdir(launcher):
            return {"ok": False, "error": f"Bản {num} chưa mở và không thấy {launcher}"}
        subprocess.run(["open", launcher])
        for _ in range(40):
            time.sleep(0.5)
            pid = app_pid(num)
            if pid:
                break
        if not pid:
            return {"ok": False, "error": f"Đã bấm mở bản {num} nhưng chưa thấy tiến trình"}
        time.sleep(4)  # chờ app nạp xong danh sách phiên
    r = subprocess.run(["osascript", "-l", "JavaScript", SEND_URL_JS, str(pid), url], capture_output=True, text=True)
    subprocess.run(["osascript", "-e",
                    f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true'],
                   capture_output=True)
    ok = r.returncode == 0
    log(f"mở {label[:14]} ở bản {num} (PID {pid}): {'ok' if ok else r.stderr.strip()[:200]}")
    return {"ok": ok, "app": num, "error": "" if ok else (r.stderr or r.stdout).strip()[:300]}


# ---------- HTTP ----------

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
        ".json": "application/json", ".webp": "image/webp"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/state":
                return self.send(200, get_state((q.get("gio") or [WINDOW_HOURS])[0]))
            if u.path == "/api/views":
                return self.send(200, load_views())
            if u.path == "/api/quota":
                return self.send(200, get_quota(force=bool(q.get("moi"))))
            m = re.fullmatch(r"/api/session/((?:local|codex|cloud)_[A-Za-z0-9_-]+)", u.path)
            if m:
                d = session_detail(m.group(1))
                return self.send(200 if d else 404, d or {"error": "không thấy phiên"})
            return self.static(u.path)
        except Exception as e:  # noqa: BLE001
            log(f"GET {u.path} lỗi: {type(e).__name__}: {e}")
            return self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        # Chỉ nhận từ chính trang này: header riêng buộc trình duyệt hỏi CORS trước, mà máy chủ không trả lời.
        if self.headers.get("X-Van-Phong") != "1":
            return self.send(403, {"error": "thiếu header"})
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self.send(403, {"error": "sai nguồn"})
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if u.path == "/api/views":
            views = body.get("views")
            if not isinstance(views, list) or len(views) > 60:
                return self.send(400, {"error": "danh sách màn hình không hợp lệ"})
            save_json(VIEWS_FILE, views)
            return self.send(200, {"ok": True})
        if u.path == "/api/org-name":
            uuid_, name = str(body.get("org") or ""), str(body.get("name") or "").strip()[:40]
            if not re.fullmatch(r"[0-9a-f-]{36}", uuid_) or not name:
                return self.send(400, {"error": "thiếu mã tổ chức hoặc tên"})
            set_org_name(uuid_, name)
            _quota["at"] = 0
            return self.send(200, {"ok": True})
        if u.path == "/api/mark":
            ids = [i for i in body.get("ids") or [] if re.fullmatch(r"(?:local|codex|cloud)_[A-Za-z0-9_-]+", str(i))]
            kind = body.get("kind")
            if kind not in ("xong", "an", None) or not ids:
                return self.send(400, {"error": "thiếu phiên hoặc sai loại đánh dấu"})
            set_marks(ids, kind)
            log(f"đánh dấu {kind} cho {len(ids)} phiên")
            return self.send(200, {"ok": True, "count": len(ids)})
        m = re.fullmatch(r"/api/session/((?:local|codex|cloud)_[A-Za-z0-9_-]+)/(summary|reply|open|compact)", u.path)
        if not m:
            return self.send(404, {"error": "không có"})
        sid, action = m.groups()
        if sid.startswith(CLOUD_PREFIX) and action in ("reply", "compact"):
            return self.send(400, {"ok": False, "error": "Phiên dự án chạy trên đám mây: trả lời ngay trong app"})
        if sid.startswith(CODEX_PREFIX) and action in ("reply", "compact"):
            # App Codex giữ khoá ghi luồng; trang tự chép câu trả lời rồi mở đúng luồng trong app.
            return self.send(400, {"ok": False, "error": "Phiên Codex: trả lời ngay trong app Codex"})
        try:
            if action == "summary":
                d = session_detail(sid, want_summary=True, force=bool(body.get("force")))
                return self.send(200 if d else 404, d or {"error": "không thấy phiên"})
            if action == "reply":
                text = (body.get("text") or "").strip()
                if not text:
                    return self.send(400, {"ok": False, "error": "Chưa có nội dung"})
                return self.send(200, send_reply(sid, text))
            if action == "open":
                return self.send(200, open_in_app(sid))
            if action == "compact":
                return self.send(200, compact_session(sid))
        except Exception as e:  # noqa: BLE001
            log(f"POST {u.path} lỗi: {type(e).__name__}: {e}")
            return self.send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            return self.send(404, {"error": "không có"})
        with open(full, "rb") as f:
            data = f.read()
        return self.send(200, data, MIME.get(os.path.splitext(full)[1], "application/octet-stream"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("VAN_PHONG_PORT", 4747)))
    ap.add_argument("--dump", action="store_true", help="in trạng thái JSON rồi thoát")
    a = ap.parse_args()
    if a.dump:
        print(json.dumps(build_state(), ensure_ascii=False, indent=1))
        return
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    log(f"khởi động ở cổng {a.port}")
    threading.Thread(target=get_state, daemon=True).start()  # nạp sẵn để lần mở trang đầu không phải chờ
    print(f"Văn phòng Claude: http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""codex-kho: kho nhiều nick Codex (ChatGPT) để các lượt gọi Codex chạy luân phiên.

Mỗi nick là một CODEX_HOME riêng ở ~/.codex-kho/nicks/<SỐ>/. Chỉ file đăng nhập (auth.json)
là của riêng nick; cấu hình, AGENTS.md, skills và lịch sử phiên là liên kết về ~/.codex, còn
CSDL phiên dùng chung qua CODEX_SQLITE_HOME. Nhờ vậy lượt sau rơi vào nick khác vẫn nối tiếp
được đúng phiên (`codex exec resume <id>`). Một email ở nhiều workspace là nhiều nick riêng
(phân biệt bằng email + mã workspace trong token). Cách nạp nick: README.md, mục "Đăng nhập kho nick".
Bản này chạy độc lập, chỉ cần lệnh `codex`; bản gốc nằm ở scripts/codex-kho.py của cc-orchestrator.

Lệnh:
  codex-kho nap [--device]        nạp thêm nick: đăng nhập trên web, chọn workspace cho nick đó
  codex-kho list [--refresh] [--json]  bảng nick + quota; dấu → là nick sẽ được chọn
  codex-kho quota [SỐ ...] [--json]    hỏi lại quota qua `codex app-server` (không gọi model)
  codex-kho pick [--exclude 1,3]  in "SỐ<TAB>CODEX_HOME<TAB>email<TAB>workspace"; exit 3 khi kho cạn
  codex-kho run [SỐ] [-- ARG...]  chạy codex trên một nick (bỏ SỐ thì tự chọn)
  codex-kho exec|e|resume|review  như `codex ...` nhưng chạy trên nick tự chọn
  codex-kho remove SỐ             đăng xuất và gỡ nick khỏi kho

Luật chọn (giống kho Claude):
  1. Chỉ lấy nick mà MỌI cửa sổ quota đã biết (5 giờ, tuần) đều DƯỚI CODEX_KHO_MAX_PCT (80%).
  2. Trong số đó lấy nick có cửa sổ căng nhất còn dư nhiều nhất; mỗi lượt đang chạy trên nick
     cộng CODEX_KHO_INFLIGHT_PENALTY điểm (10) để việc song song toả ra nhiều nick; hoà điểm
     thì lấy nick lâu chưa dùng nhất (luân phiên).
  3. Nick chưa có số quota vẫn dùng được nhưng xếp sau mọi nick có số.
  4. Lượt `exec` báo chạm hạn mức hay mất đăng nhập → nick bị gác lại, lượt sau tự chọn nick khác.
  5. Kho trống hoặc cạn → chạy bằng đăng nhập gốc ~/.codex như trước khi có kho
     (CODEX_KHO_FALLBACK=none để báo lỗi, exit 75).
Số quota lấy từ nhật ký phiên sau mỗi lượt (miễn phí) và hỏi lại nền khi cũ quá CODEX_KHO_TTL giây.

Cấu hình (tuỳ chọn, ~/.config/cc-orchestrator/ai-tools.conf): CODEX_KHO_MAX_PCT, CODEX_KHO_INFLIGHT_PENALTY,
CODEX_KHO_TTL, CODEX_KHO_FALLBACK. Biến môi trường: CODEX_KHO_EXCLUDE (danh sách số cần né),
CODEX_KHO_QUIET=1 (không in dòng nick); cho test: CODEX_KHO_DIR, CODEX_KHO_MAIN, CODEX_KHO_CODEX.
"""
import base64
import fcntl
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HOME = os.path.expanduser("~")
ROOT = os.environ.get("CODEX_KHO_DIR") or os.path.join(HOME, ".codex-kho")
MAIN = os.environ.get("CODEX_KHO_MAIN") or os.path.join(HOME, ".codex")
NICKS = os.path.join(ROOT, "nicks")
INFLIGHT = os.path.join(ROOT, "inflight")
STATE = os.path.join(ROOT, "state.json")
CONF = os.environ.get("CC_AI_CONFIG") or os.path.join(HOME, ".config", "cc-orchestrator", "ai-tools.conf")
SELF = os.path.abspath(__file__)

# Thứ nick dùng chung với ~/.codex (liên kết). KHÔNG liên kết file *.sqlite*: SQLite đặt tên
# file -wal/-shm theo đường dẫn được mở, mở qua liên kết là hai bên ghi hai WAL khác nhau → hỏng
# CSDL. CSDL dùng chung bằng biến CODEX_SQLITE_HOME. Cũng không liên kết models_cache.json (danh
# sách model tuỳ gói của từng nick) và trạng thái riêng của app desktop.
SHARED = ("config.toml", "AGENTS.md", "sessions", "archived_sessions", "history.jsonl",
          "session_index.jsonl", "thread-writer-locks", "skills", "rules", "prompts", "hooks.json",
          "hooks", "plugins", "packages", "memories", "memories_v2", "memories_extensions",
          "vendor_imports", "shell_snapshots", "version.json", "attachments", "generated_images",
          "artifacts", "worktrees", "reference-worktrees")
CREDS = ["-c", 'cli_auth_credentials_store="file"']  # đăng nhập nằm trong auth.json của từng nick

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SESSION_RE = re.compile(r"session id:\s*([0-9a-f-]{36})", re.I)
# `codex exec` in ra stderr MỌI file nó đọc (repo có cả mã xử lý quota), nên chỉ tin dòng có
# khung lỗi của chính Codex: "ERROR: ..." hoặc "stream error: ...". 429 lẻ thì Codex tự thử
# lại, không gác nick vì nó; chỉ gác khi đúng là hết hạn mức hoặc mất đăng nhập.
ERR_FRAME = re.compile(r"^\s*(error|stream error)\b", re.I)
LIMIT_RE = re.compile(r"usage limit|limit reached|purchase more credits|upgrade to (pro|plus)|"
                      r"billing hard limit|out of credits", re.I)
AUTH_RE = re.compile(r"\b401\b|unauthori[sz]ed|refresh.?token|(log|sign) ?in again|not logged in|"
                     r"token (is )?(expired|invalid|revoked)", re.I)


def say(msg):
    print("codex-kho: " + msg, file=sys.stderr, flush=True)


def cfg(key, default):
    """Giống cfg() của các script bash: dòng KEY= cuối cùng thắng, bỏ khoảng trắng cuối."""
    val = None
    try:
        with open(CONF, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*%s=(.*)$" % re.escape(key), line)
                if m:
                    val = m.group(1).rstrip()
    except OSError:
        pass
    return val if val else default


def num_cfg(key, default):
    try:
        return float(cfg(key, default))
    except ValueError:
        return float(default)


def real_codex():
    cands = [os.environ.get("CODEX_KHO_CODEX", "")]
    cands += [os.path.join(d, "codex") for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    cands += [os.path.join(HOME, ".local", "bin", "codex"), "/opt/homebrew/bin/codex",
              "/Applications/Codex.app/Contents/Resources/codex"]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def safe_rmtree(path):
    """Gỡ liên kết trước rồi mới xoá, để xoá thư mục nick không bao giờ chạm vào ~/.codex."""
    for name in os.listdir(path):
        p = os.path.join(path, name)
        if os.path.islink(p):
            os.unlink(p)
    shutil.rmtree(path, ignore_errors=True)


# --- trạng thái chung: quota, gác nick, lần dùng cuối (khoá file để nhiều lượt cùng ghi) ---
class State:
    def __enter__(self):
        os.makedirs(ROOT, exist_ok=True)
        self.lock = open(os.path.join(ROOT, "state.lock"), "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            with open(STATE, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        return self

    def nick(self, n):
        return self.data.setdefault("nicks", {}).setdefault(str(n), {})

    def __exit__(self, *exc):
        tmp = STATE + ".%d.tmp" % os.getpid()
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
        return False


def nicks():
    """[(SỐ, CODEX_HOME, meta)] của các nick đã đăng nhập, theo số."""
    out = []
    try:
        names = os.listdir(NICKS)
    except OSError:
        return out
    for d in names:
        home = os.path.join(NICKS, d)
        if d.isdigit() and os.path.isfile(os.path.join(home, "auth.json")):
            try:
                with open(os.path.join(home, "kho.json"), encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {}
            out.append((int(d), home, meta))
    return sorted(out)


def link(home):
    """Nối các thứ dùng chung về ~/.codex; trả tên những mục đã tách riêng (không còn là liên kết)."""
    diverged = []
    for name in SHARED:
        src, dst = os.path.join(MAIN, name), os.path.join(home, name)
        if os.path.islink(dst):
            continue
        if os.path.exists(dst):
            diverged.append(name)
        elif os.path.exists(src):
            os.symlink(src, dst)
    return diverged


def child_env(home):
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    if home != MAIN:
        env["CODEX_SQLITE_HOME"] = MAIN
    return env


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def busy():
    counts = {}
    try:
        names = os.listdir(INFLIGHT)
    except OSError:
        return counts
    for b in names:
        n, _, pid = b.partition(".")
        if alive(pid):
            counts[n] = counts.get(n, 0) + 1
        else:
            try:
                os.remove(os.path.join(INFLIGHT, b))
            except OSError:
                pass
    return counts


def hold(n, pid):
    os.makedirs(INFLIGHT, exist_ok=True)
    open(os.path.join(INFLIGHT, "%s.%s" % (n, pid)), "w").close()


def release(n, pid):
    try:
        os.remove(os.path.join(INFLIGHT, "%s.%s" % (n, pid)))
    except OSError:
        pass


# --- quota ---
def window_label(mins):
    return {300: "5h", 10080: "tuần"}.get(mins, ("%sp" % mins) if mins else "?")


def windows_of(rl, mins_key, pct_key, reset_key, limit_id):
    out = []
    for k in ("primary", "secondary"):
        w = (rl or {}).get(k)
        if w and w.get(pct_key) is not None:
            out.append({"limit": limit_id, "mins": w.get(mins_key), "pct": float(w[pct_key]),
                        "resetsAt": w.get(reset_key)})
    return out


def limits_windows(lim):
    wins, reached = [], False
    for lid, rl in (lim.get("rateLimitsByLimitId") or {"codex": lim.get("rateLimits") or {}}).items():
        wins += windows_of(rl, "windowDurationMins", "usedPercent", "resetsAt", lid)
        reached = reached or bool((rl or {}).get("rateLimitReachedType"))
    return wins, reached


def eff_pct(w, now):
    r = w.get("resetsAt")
    return 0.0 if r and r <= now else float(w.get("pct") or 0)


def set_quota(st, n, windows, reached, src, replace_limit=None):
    q = st.nick(n).setdefault("quota", {})
    if replace_limit is not None:  # nhật ký phiên chỉ có một hạn mức: thay đúng hạn mức đó
        windows = [w for w in q.get("windows", []) if w.get("limit") != replace_limit] + windows
    q.update({"at": time.time(), "src": src, "windows": windows, "reached": bool(reached)})


def app_server(home, codex, timeout=25):
    """(account, rateLimits, lỗi) từ `codex app-server` của một CODEX_HOME. Đọc thô từ ống để
    không kẹt vì bộ đệm dòng (cùng cách tab Quota của Văn phòng Claude)."""
    try:
        p = subprocess.Popen([codex] + CREDS + ["-s", "read-only", "-a", "never", "app-server"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             cwd=tempfile.gettempdir(), env=child_env(home))
    except OSError as e:
        return None, None, "không chạy được codex (%s)" % type(e).__name__
    fd, buf, end = p.stdout.fileno(), [b""], time.time() + timeout

    def send(o):
        p.stdin.write((json.dumps(o) + "\n").encode())
        p.stdin.flush()

    def wait(want):
        while time.time() < end:
            while b"\n" in buf[0]:
                line, buf[0] = buf[0].split(b"\n", 1)
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("id") == want:
                    return m
            if select.select([fd], [], [], 0.5)[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None
                buf[0] += chunk
        return None
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-kho", "version": "1"}}})
        if not wait(1):
            return None, None, "codex app-server không trả lời"
        send({"method": "initialized"})
        send({"id": 2, "method": "account/read", "params": {}})
        acc = wait(2) or {}
        send({"id": 3, "method": "account/rateLimits/read"})
        lim = wait(3) or {}
    except (OSError, ValueError) as e:
        return None, None, "codex app-server lỗi (%s)" % type(e).__name__
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    account = (acc.get("result") or {}).get("account")
    if lim.get("error") or not lim.get("result"):
        return account, None, ((lim.get("error") or {}).get("message") or "không đọc được giới hạn")[:160]
    return account, lim["result"], ""


def token_claims(home):
    """Phần công khai của id_token (email, mã workspace, gói). Không in, không lưu token."""
    try:
        with open(os.path.join(home, "auth.json"), encoding="utf-8") as f:
            tok = (json.load(f).get("tokens") or {})
        part = tok.get("id_token", "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (OSError, ValueError, IndexError):
        return {}
    auth = claims.get("https://api.openai.com/auth") or {}
    return {"email": claims.get("email") or "",
            "workspaceId": auth.get("chatgpt_account_id") or tok.get("account_id") or "",
            "plan": auth.get("chatgpt_plan_type") or ""}


def write_meta(home, meta):
    tmp = os.path.join(home, "kho.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(home, "kho.json"))


def auth_of(home):
    try:
        return os.path.getmtime(os.path.join(home, "auth.json"))
    except OSError:
        return 0


def mark(st, n, why, auth_mtime=None):
    m = {"why": why, "at": time.time()}
    if auth_mtime is not None:
        m["auth"] = auth_mtime  # gác tới khi đăng nhập lại (auth.json đổi)
    else:
        m["until"] = time.time() + 3600  # số quota thật (hỏi lại nền) quyết tiếp sau mốc này
    st.nick(n)["mark"] = m


def mark_active(m, home, now):
    if not m:
        return False
    if "auth" in m:
        return auth_of(home) == m["auth"]
    return m.get("until", 0) > now


def refresh(targets, codex):
    """Hỏi quota song song cho [(SỐ, home, meta)], ghi vào state; trả {SỐ: lỗi}."""
    results = {}

    def one(n, home):
        results[n] = app_server(home, codex)
    threads = [threading.Thread(target=one, args=(n, h)) for n, h, _ in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    errs = {}
    with State() as st:
        for n, home, meta in targets:
            account, lim, err = results.get(n, (None, None, "không có kết quả"))
            if account:
                upd = dict(meta, email=account.get("email") or meta.get("email", ""),
                           workspace=account.get("workspaceName") or meta.get("workspace") or "",
                           plan=account.get("planType") or meta.get("plan", ""))
                if upd != meta:
                    write_meta(home, upd)
            if lim:
                wins, reached = limits_windows(lim)
                set_quota(st, n, wins, reached, "app")
                if "auth" in (st.nick(n).get("mark") or {}):
                    st.nick(n).pop("mark", None)  # hỏi được quota tức là đăng nhập còn sống
            else:
                errs[n] = err
                if AUTH_RE.search(err or ""):
                    mark(st, n, "mất đăng nhập", auth_of(home))
        st.data.pop("refreshing", None)
    return errs


def refresh_in_background(st, all_nicks, now):
    ttl = num_cfg("CODEX_KHO_TTL", "600")
    stale = [str(n) for n, _, _ in all_nicks if now - (st.nick(n).get("quota") or {}).get("at", 0) > ttl]
    r = st.data.get("refreshing") or {}
    if not stale or (alive(r.get("pid")) and now - r.get("at", 0) < 120):
        return
    p = subprocess.Popen([sys.executable, "-I", SELF, "quota", "--quiet"] + stale, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    st.data["refreshing"] = {"pid": p.pid, "at": now}


# --- xếp hạng ---
def rows(st, exclude=(), now=None):
    now = now or time.time()
    max_pct = num_cfg("CODEX_KHO_MAX_PCT", cfg("KHO_MAX_PCT", "80"))
    penalty = num_cfg("CODEX_KHO_INFLIGHT_PENALTY", "10")
    b = busy()
    out = []
    for n, home, meta in nicks():
        s = st.nick(n)
        q = s.get("quota") or {}
        wins = q.get("windows") or []
        pcts = [eff_pct(w, now) for w in wins]
        why = ""
        if str(n) in exclude:
            why = "đã thử ở lượt này"
        elif mark_active(s.get("mark"), home, now):
            m = s["mark"]
            why = m["why"] + (time.strftime(" (gác tới %H:%M)", time.localtime(m["until"])) if "until" in m else "")
        elif q.get("reached") and pcts and max(pcts) >= 100:
            why = "chạm hạn mức"
        else:
            over = ["%s %g%%" % (window_label(w.get("mins")), eff_pct(w, now)) for w in wins
                    if eff_pct(w, now) >= max_pct]
            if over:
                why = "chạm ngưỡng %g%%: %s" % (max_pct, ", ".join(over))
        rank = (max(pcts) if pcts else max_pct) + penalty * b.get(str(n), 0)
        out.append({"n": n, "home": home, "meta": meta, "why": why, "rank": rank, "wins": wins,
                    "busy": b.get(str(n), 0), "last": s.get("lastUsed", 0), "q": q})
    return out


def best(rs):
    ok = [r for r in rs if not r["why"]]
    return min(ok, key=lambda r: (r["rank"], r["last"], r["n"])) if ok else None


def label(meta):
    ws = meta.get("workspace") or "Cá nhân"
    return "%s [%s%s]" % (meta.get("email") or "?", ws, (" · " + meta["plan"]) if meta.get("plan") else "")


def quota_text(r, now):
    if not r["wins"]:
        return "chưa có số quota"
    parts = []
    for w in sorted(r["wins"], key=lambda w: w.get("mins") or 0):
        t = "%s %g%%" % (window_label(w.get("mins")), eff_pct(w, now))
        if w.get("resetsAt") and w["resetsAt"] > now:
            left = int(w["resetsAt"] - now)
            d, h, m = left // 86400, left % 86400 // 3600, left % 3600 // 60
            t += " (hồi %s)" % ("%dn%dg" % (d, h) if d else "%dg%02dp" % (h, m))
        parts.append(t)
    age = int(now - (r["q"].get("at") or now))
    return "  ".join(parts) + ("  · số cũ %dp" % (age // 60) if age >= 120 else "")


# --- chạy codex trên nick ---
def find_rollout(sid):
    days = sorted(glob.glob(os.path.join(MAIN, "sessions", "*", "*", "*")), reverse=True)[:3]
    for d in days:
        hit = glob.glob(os.path.join(d, "rollout-*-%s.jsonl" % sid))
        if hit:
            return hit[0]
    hit = glob.glob(os.path.join(MAIN, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % sid))
    return hit[0] if hit else None


def record_from_rollout(n, sid):
    """Số quota Codex ghi trong nhật ký của chính phiên vừa chạy (miễn phí, đúng nick đó)."""
    f = find_rollout(sid)
    if not f:
        return False
    try:
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400000))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
        except ValueError:
            continue
        lid = rl.get("limit_id") or "codex"
        wins = windows_of(rl, "window_minutes", "used_percent", "resets_at", lid)
        if wins:
            with State() as st:
                set_quota(st, n, wins, rl.get("rate_limit_reached_type"), "rollout", replace_limit=lid)
            return True
    return False


def choose(exclude=(), want=None, pid=None):
    """Chọn (và giữ chỗ cho pid, trong cùng một khoá) một nick; trả row hoặc None."""
    now = time.time()
    with State() as st:
        all_nicks = nicks()
        rs = rows(st, exclude, now)
        r = next((x for x in rs if x["n"] == want), None) if want is not None else best(rs)
        if r and pid:
            hold(r["n"], pid)
            st.nick(r["n"])["lastUsed"] = now
        if want is None and all_nicks:
            refresh_in_background(st, all_nicks, now)
    return r


def run(args, want=None):
    codex = real_codex()
    if not codex:
        say("không thấy codex (đặt CODEX_KHO_CODEX hoặc cài codex)")
        return 127
    quiet = os.environ.get("CODEX_KHO_QUIET") == "1"
    exclude = [x for x in os.environ.get("CODEX_KHO_EXCLUDE", "").split(",") if x]
    r = choose(exclude, want, os.getpid())
    if want is not None and not r:
        say("không có nick %s trong kho" % want)
        return 2
    if r:
        diverged = link(r["home"])
        home, n = r["home"], r["n"]
        if not quiet:
            say("nick=%d %s%s" % (n, label(r["meta"]), " (tách riêng: %s)" % ", ".join(diverged) if diverged else ""))
    else:
        if (os.environ.get("CODEX_KHO_FALLBACK") or cfg("CODEX_KHO_FALLBACK", "goc")) == "none":
            say("kho không còn nick nào dùng được (xem: codex-kho list)")
            return 75
        home, n = MAIN, None
        if not quiet:
            say("kho trống hoặc cạn — chạy bằng đăng nhập gốc %s" % home.replace(HOME, "~"))
    argv = [codex] + CREDS + list(args)
    env = child_env(home)
    if not args or args[0] not in ("exec", "e"):
        os.execve(codex, argv, env)  # tương tác: giữ nguyên TTY; dấu giữ chỗ hết khi PID này thoát
    return run_exec(argv, env, n)


def run_exec(argv, env, n):
    """Chạy `codex exec`, chuyển stderr y nguyên, soi dòng lỗi của chính Codex để gác nick."""
    p = subprocess.Popen(argv, env=env, stderr=subprocess.PIPE)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda s, f: p.send_signal(s))
    sid, flagged, pending = None, False, b""
    out = getattr(sys.stderr, "buffer", sys.stderr)
    while True:
        try:
            chunk = os.read(p.stderr.fileno(), 65536)
        except InterruptedError:
            continue
        if not chunk:
            break
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for raw in lines:
            line = ANSI.sub("", raw.decode("utf-8", "replace"))
            if sid is None:
                m = SESSION_RE.search(line)
                if m:
                    sid = m.group(1)
            if n is not None and not flagged and ERR_FRAME.search(line):
                kind = "auth" if AUTH_RE.search(line) else ("limit" if LIMIT_RE.search(line) else None)
                if kind:
                    flagged = True
                    with State() as st:  # gác NGAY: bên gọi có thể giết cả nhóm tiến trình khi thấy dòng này
                        if kind == "auth":
                            mark(st, n, "mất đăng nhập", auth_of(env["CODEX_HOME"]))
                        else:
                            mark(st, n, "chạm hạn mức")
            out.write(raw + b"\n")
        out.flush()
    if pending:
        out.write(pending)
        out.flush()
    rc = p.wait()
    if n is not None:
        release(n, os.getpid())
        if sid:
            record_from_rollout(n, sid)
        if flagged:
            say("nick %d chạm hạn mức hoặc mất đăng nhập — đã gác, lượt sau tự chọn nick khác" % n)
    return rc if rc >= 0 else 128 - rc


# --- các lệnh ---
def as_json(r, now):
    return {"n": r["n"], "email": r["meta"].get("email", ""), "workspace": r["meta"].get("workspace") or "",
            "plan": r["meta"].get("plan", ""), "why": r["why"], "busy": r["busy"],
            "updatedAt": r["q"].get("at"), "src": r["q"].get("src"),
            "windows": [dict(w, pct=eff_pct(w, now)) for w in r["wins"]]}


def cmd_list(argv):
    codex = real_codex()
    if "--refresh" in argv and codex and nicks():
        for n, e in refresh(nicks(), codex).items():
            say("nick %d: không hỏi được quota (%s)" % (n, e))
    now = time.time()
    with State() as st:
        rs = rows(st, (), now)
    pick = best(rs)
    if "--json" in argv:
        print(json.dumps({"pick": pick["n"] if pick else None, "nicks": [as_json(r, now) for r in rs]},
                         ensure_ascii=False))
        return 0
    for r in rs:
        state = r["why"] or ("dùng được" if r["wins"] else "dùng được, xếp sau nick có số")
        extra = ("  đang chạy %d" % r["busy"]) if r["busy"] else ""
        diverged = link(r["home"])
        if diverged:
            extra += "  ⚠ tách riêng: " + ", ".join(diverged)
        print("%s %d  %s  %s%s  %s" % ("→" if pick and r["n"] == pick["n"] else " ", r["n"], label(r["meta"]),
                                     quota_text(r, now), extra, state))
    if not rs:
        print("(kho trống — nạp nick: codex-kho nap)")
    elif not pick:
        print("(không còn nick nào dùng được — lượt gọi sẽ chạy bằng đăng nhập gốc ~/.codex)")
    return 0


def cmd_quota(argv):
    codex = real_codex()
    want = {int(a) for a in argv if a.isdigit()}
    targets = [t for t in nicks() if not want or t[0] in want]
    if codex and targets:
        errs = refresh(targets, codex)
        if "--quiet" not in argv and "--json" not in argv:
            for n, e in errs.items():
                say("nick %d: không hỏi được quota (%s)" % (n, e))
    if "--quiet" in argv:
        return 0
    return cmd_list([a for a in argv if a == "--json"])


def cmd_pick(argv):
    exclude = []
    if "--exclude" in argv:
        i = argv.index("--exclude")
        exclude = [x for x in (argv[i + 1] if i + 1 < len(argv) else "").split(",") if x]
    with State() as st:
        r = best(rows(st, exclude))
    if not r:
        say("không còn nick nào dùng được")
        return 3
    print("\t".join([str(r["n"]), r["home"], r["meta"].get("email", ""), r["meta"].get("workspace") or "Cá nhân"]))
    return 0


def cmd_nap(argv):
    codex = real_codex()
    if not codex:
        say("không thấy codex")
        return 127
    os.makedirs(NICKS, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".nap-", dir=NICKS)
    link(tmp)
    env = child_env(tmp)
    if "--device" in argv:
        # Mã thiết bị: đăng nhập được từ máy khác, nhưng workspace doanh nghiệp mặc định chặn kiểu này
        # ("contact your workspace admin to enable device code authentication", đo 04/11/2026).
        login = [codex] + CREDS + ["login", "--device-auth"]
        hint = "nhập mã"
    else:
        # Đăng nhập web: trang trả kết quả về localhost:1455 của máy này. Không để Codex tự mở trình
        # duyệt mặc định (thường đang giữ nick khác): người dùng tự dán link vào đúng cửa sổ.
        login = [codex] + CREDS + ["login"]
        env.update(BROWSER="/usr/bin/true", NO_BROWSER="1")
        hint = "đăng nhập"
    print("Mở đường link dưới đây trong trình duyệt TRÊN MÁY NÀY, ở cửa sổ đang đăng nhập ĐÚNG nick muốn nạp"
          " (cửa sổ ẩn danh nếu cần), %s, rồi chọn workspace cho nick này." % hint, flush=True)
    rc = subprocess.call(login, env=env)
    if rc != 0 or not os.path.isfile(os.path.join(tmp, "auth.json")):
        safe_rmtree(tmp)
        say("đăng nhập không xong (exit %d), chưa nạp gì" % rc)
        return rc or 1
    claims = token_claims(tmp)
    account, lim, err = app_server(tmp, codex)
    meta = {"email": (account or {}).get("email") or claims.get("email", ""),
            "workspace": (account or {}).get("workspaceName") or "",
            "workspaceId": claims.get("workspaceId", ""),
            "plan": (account or {}).get("planType") or claims.get("plan", ""), "addedAt": int(time.time())}
    for n, home, m in nicks():
        if m.get("email") == meta["email"] and m.get("workspaceId") == meta["workspaceId"]:
            safe_rmtree(tmp)
            say("nick này đã có trong kho (nick %d: %s), bỏ lượt nạp vừa rồi" % (n, label(m)))
            return 0
    n = max([x[0] for x in nicks()] or [0]) + 1  # không dùng lại số đã gỡ, giống claude-swap
    home = os.path.join(NICKS, str(n))
    os.rename(tmp, home)
    write_meta(home, meta)
    if lim:
        with State() as st:
            wins, reached = limits_windows(lim)
            set_quota(st, n, wins, reached, "app")
    print("Đã nạp nick %d: %s" % (n, label(meta)), flush=True)
    if err:
        say("chưa hỏi được quota (%s), lượt gọi đầu sẽ tự hỏi lại" % err)
    return 0


def cmd_remove(argv):
    if not argv or not argv[0].isdigit():
        say("remove cần SỐ nick")
        return 2
    n = int(argv[0])
    hit = next((t for t in nicks() if t[0] == n), None)
    home = os.path.join(NICKS, str(n))
    if not os.path.isdir(home):
        say("không có nick %d" % n)
        return 2
    codex = real_codex()
    if hit and codex:
        subprocess.call([codex] + CREDS + ["logout"], env=child_env(home),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    safe_rmtree(home)
    with State() as st:
        st.data.get("nicks", {}).pop(str(n), None)
    print("Đã gỡ nick %d%s" % (n, (": " + label(hit[2])) if hit else ""))
    return 0


def main(argv):
    cmd, rest = (argv[0] if argv else ""), argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    table = {"list": cmd_list, "quota": cmd_quota, "pick": cmd_pick, "nap": cmd_nap, "add": cmd_nap,
             "remove": cmd_remove, "rm": cmd_remove}
    if cmd in table:
        return table[cmd](rest)
    if cmd == "run":
        want = None
        if rest and rest[0].isdigit():
            want, rest = int(rest[0]), rest[1:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return run(rest, want)
    return run(argv)  # mọi thứ khác là đối số của codex


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

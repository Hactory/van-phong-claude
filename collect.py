"""collect.py — gom quota MỌI nick Claude trên máy thành một JSON (tab Quota của Văn phòng Claude).

Chạy bằng python của claude-swap (có sẵn module claude_swap):
    ~/.local/share/uv/tools/claude-swap/bin/python -I collect.py [--fresh-sec 1800]

Gom theo TÚI QUOTA = một email trong một team (organizationUuid). Cùng túi có thể đang
đăng nhập ở nhiều chỗ; mỗi túi liệt kê đủ các chỗ đó, và lấy số quota MỚI NHẤT trong
mọi nguồn:

  1. Kho claude-swap  — `cswap list --json` (số claude-swap lưu, kèm tuổi).
  2. Lane CLI          — các thư mục cấu hình ~/.claude, ~/.claude-* (định danh đọc ở
                         .claude.json). Túi nào chưa có số mới thì hỏi thẳng Anthropic
                         bằng token trong Keychain của lane đó. CHỈ ĐỌC: token hết hạn
                         thì báo, KHÔNG tự làm mới (làm mới sẽ xoay khoá, lane đó mất
                         đăng nhập). Token không bao giờ được in ra.
  3. App Claude desktop — ~/Library/Application Support/Claude*/plan-usage-history.json
                         (app tự ghi % quota theo team), config.json cho tài khoản.
                         App chỉ ghi file này thỉnh thoảng (có khi cũ hàng chục giờ), nên:
  4. Phiên Code đang mở trong app — tiến trình mang sẵn token của app trong biến môi
                         trường; túi nào chưa có số mới thì hỏi Anthropic bằng token đó.
                         CHỈ ĐỌC, không làm mới, không in, không ghi ra đâu.

Thêm hai thẻ quota ngoài Claude (mỗi thẻ hỏi tối đa --fresh-other giây một lần):

  5. Codex   — hỏi `codex app-server` (giao thức JSON-RPC của chính Codex: account/read,
               account/rateLimits/read); Codex tự gọi OpenAI bằng đăng nhập của nó. Không chạy
               được thì lấy số Codex ghi lần cuối trong nhật ký phiên ($CODEX_HOME/sessions).
  6. Antigravity — máy chủ cục bộ của app Antigravity đang mở (RetrieveUserQuotaSummary,
               GetUserStatus; mã CSRF đọc từ tham số tiến trình, chỉ giữ trong bộ nhớ). App
               đóng thì chạy `agy -p /usage --output-format json` (báo cáo quota, không gửi lời
               nhắc nào cho model).

Không gọi model nào, không tốn token LLM.
"""
import argparse
import glob
import json
import os
import re
import select
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
NOW = time.time()
# Cấu hình dùng chung với Văn phòng Claude: "banApp" (thư mục app đặt tên khác -> số bản),
# "boQuaLane" (thư mục ~/.claude-* không phải lane đăng nhập thật).
CONFIG = {}
try:
    with open(os.path.join(HOME, ".claude", "van-phong", "cau-hinh.json"), encoding="utf-8") as _f:
        CONFIG = json.load(_f) or {}
except Exception:
    pass


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def iso_to_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def window(pct, resets_at=None):
    if pct is None:
        return None
    return {"pct": float(pct), "resetsAt": iso_to_epoch(resets_at) if isinstance(resets_at, str) else resets_at}


pools = {}          # key "email|orgUuid" -> pool
kho_emails = set()  # email có ít nhất một nick trong kho
org_names = {}      # orgUuid -> tên team
acc_email = {}      # accountUuid -> email
errors = []


def pool_for(email, org_uuid, org_name=None):
    key = f"{email or '?'}|{org_uuid or '?'}"
    p = pools.get(key)
    if p is None:
        p = pools[key] = {"key": key, "email": email or "?", "orgUuid": org_uuid or "",
                          "orgName": org_name or "", "fiveHour": None, "sevenDay": None,
                          "scoped": [], "source": "", "updatedAt": None, "status": "",
                          "locations": []}
    if org_name and not p["orgName"]:
        p["orgName"] = org_name
    return p


def offer(p, five, seven, scoped, updated_at, source):
    """Nhận số quota từ một nguồn; chỉ giữ nếu MỚI hơn số đang có."""
    if five is None and seven is None:
        return
    if p["updatedAt"] is not None and updated_at is not None and updated_at <= p["updatedAt"]:
        return
    p.update(fiveHour=five, sevenDay=seven, scoped=scoped or [], updatedAt=updated_at, source=source)


def add_loc(p, kind, label, detail=""):
    p["locations"].append({"kind": kind, "label": label, "detail": detail})


def note_identity(acct):
    if not isinstance(acct, dict):
        return
    if acct.get("organizationUuid") and acct.get("organizationName"):
        org_names.setdefault(acct["organizationUuid"], acct["organizationName"])
    if acct.get("accountUuid") and acct.get("emailAddress"):
        acc_email.setdefault(acct["accountUuid"], acct["emailAddress"])


# ---------- 0. định danh từ mọi .claude.json (không có bí mật) ----------
CONFIG_JSONS = [os.path.join(HOME, ".claude.json")] \
    + glob.glob(os.path.join(HOME, ".claude-*", ".claude.json")) \
    + glob.glob(os.path.join(HOME, ".claude-swap-backup", "sessions", "*", ".claude.json"))
for f in CONFIG_JSONS:
    d = load_json(f)
    if d:
        note_identity(d.get("oauthAccount"))


# ---------- 1. kho claude-swap ----------
def collect_kho():
    cswap = os.environ.get("CLAUDE_KHO_CSWAP") or os.path.join(HOME, ".local", "bin", "cswap")
    if not os.path.exists(cswap):
        errors.append("chưa cài claude-swap")
        return
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    try:
        out = subprocess.run([cswap, "list", "--json"], capture_output=True, text=True, timeout=30, env=env).stdout
        data = json.loads(out)
    except Exception as e:
        errors.append(f"kho: không đọc được cswap list ({type(e).__name__})")
        return
    for a in data.get("accounts", []):
        org = a.get("organizationUuid") or ""
        if org and a.get("organizationName"):
            org_names[org] = a["organizationName"]
        p = pool_for(a.get("email"), org, a.get("organizationName"))
        kho_emails.add(a.get("email"))
        u = a.get("usage") or {}
        f5, s7 = u.get("fiveHour") or {}, u.get("sevenDay") or {}
        age = a.get("usageAgeSeconds")
        updated = NOW - float(age) if isinstance(age, (int, float)) else None
        offer(p, window(f5.get("pct"), f5.get("resetsAt")), window(s7.get("pct"), s7.get("resetsAt")),
              [{"name": s.get("name"), "pct": s.get("pct"), "resetsAt": iso_to_epoch(s.get("resetsAt"))}
               for s in (u.get("scoped") or []) if s.get("pct") is not None],
              updated, "kho")
        status = a.get("usageStatus") or ""
        if status == "unavailable":
            status = "Anthropic tạm chặn hỏi quota (~30 lần/giờ mỗi tài khoản), tự hồi sau tối đa 1 giờ; nick vẫn dùng được"
        add_loc(p, "kho", f"Kho #{a.get('number')}", "" if status == "ok" else status)


# ---------- 2. lane CLI ----------
def keychain_secret(service):
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def read_lane_token(config_dir, is_default):
    """Đọc token OAuth của lane. Trả (token, fingerprint, status). Token KHÔNG được in/ghi."""
    try:
        from claude_swap.session import keychain_service_name
        from claude_swap import oauth
    except Exception:
        return None, None, "thiếu module claude_swap"
    service = "Claude Code-credentials" if is_default else keychain_service_name(config_dir)
    raw = keychain_secret(service)
    if not raw:
        return None, None, "chưa đăng nhập / không đọc được Keychain"
    try:
        o = (json.loads(raw).get("claudeAiOauth") or {})
        fp = oauth.credential_fingerprint(raw)
    except Exception:
        return None, None, "đăng nhập không đọc được"
    finally:
        raw = None
    if not o.get("accessToken"):
        return None, None, "không có token OAuth"
    if oauth.is_oauth_token_expired(o.get("expiresAt")):
        return None, fp, "token hết hạn (tự làm mới khi lane này được dùng)"
    return o["accessToken"], fp, ""


def http_why(e):
    code = getattr(e, "code", None)
    why = {401: "token bị từ chối", 403: "team này không cho xem quota qua API",
           429: "Anthropic đang giới hạn lượt hỏi, thử lại sau"}.get(code, type(e).__name__)
    return f"{why}" + (f" (HTTP {code})" if code else "")


def token_owner(token):
    """Hỏi Anthropic token này thuộc ai: (email, orgUuid, orgName) hoặc lỗi.

    ĐỪNG tin .claude.json: file có thể còn ghi nick cũ trong khi token Keychain đã là nick khác,
    tin theo file sẽ gắn số của nick này cho nick kia."""
    import urllib.request
    try:
        from claude_swap import oauth
    except ImportError:
        return None, "cần cài claude-swap để xác minh chủ token"
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/profile", headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": oauth.OAUTH_BETA_HEADER,
        "User-Agent": "claude-swap/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        return None, "không xác minh được chủ token: " + http_why(e)
    a, o = d.get("account") or {}, d.get("organization") or {}
    email = a.get("email") or a.get("email_address")
    if not email:
        return None, "không xác minh được chủ token"
    return (email, o.get("uuid") or "", o.get("name") or ""), ""


def fetch_usage(token):
    try:
        from claude_swap import oauth
    except ImportError:
        return None, None, [], "cần cài claude-swap để hỏi quota"
    try:
        r = oauth.build_usage_result(oauth.request_usage_data(token)) or {}
    except Exception as e:
        return None, None, [], "hỏi quota lỗi: " + http_why(e)
    f5, s7 = r.get("five_hour") or {}, r.get("seven_day") or {}
    return (window(f5.get("pct"), f5.get("resets_at")), window(s7.get("pct"), s7.get("resets_at")),
            [{"name": s["name"], "pct": s["pct"], "resetsAt": iso_to_epoch(s.get("resets_at"))}
             for s in r.get("scoped", [])], "")


CACHE = os.path.join(HOME, ".cache", "claude-quota", "live.json")   # chỉ số %, không có token
BACKOFF_429 = 3600   # Anthropic trả Retry-After 3600s khi hết lượt hỏi


def load_cache():
    return load_json(CACHE) or {}


def save_cache(c):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass


LANE_LABELS = {".claude": "CLI mặc định (~/.claude)"}
LANE_SKIP = {".claude-swap-backup"} | set(CONFIG.get("boQuaLane") or [])


def collect_lanes(fresh_sec):
    cache = load_cache()
    dirs = [(os.path.join(HOME, ".claude"), os.path.join(HOME, ".claude.json"), True)]
    for d in sorted(glob.glob(os.path.join(HOME, ".claude-*"))):
        # .claude-swap-backup là chỗ claude-swap cất nick, không phải lane; thêm thư mục khác qua "boQuaLane".
        if os.path.isdir(d) and os.path.basename(d) not in LANE_SKIP \
                and os.path.exists(os.path.join(d, ".claude.json")):
            dirs.append((d, os.path.join(d, ".claude.json"), False))
    for cdir, cjson, is_default in dirs:
        name = os.path.basename(cdir)
        label = LANE_LABELS.get(name, f"CLI ~/{name}")
        token, fp, status = read_lane_token(cdir, is_default)
        if not fp:
            continue                       # lane chưa đăng nhập: không có gì để theo dõi
        c = cache.get(cdir) or {}
        if c.get("fp") != fp:              # token đổi (đăng nhập nick khác) → bỏ mọi thứ đã đệm
            c = {"fp": fp}
        # Chủ token: lấy từ đệm theo dấu vân tay; chưa có thì hỏi Anthropic.
        owner = c.get("owner")
        if not owner and token and NOW >= (c.get("backoffUntil") or 0):
            owner, why = token_owner(token)
            if owner:
                c["owner"] = owner
            elif "HTTP 429" in why:
                c["backoffUntil"] = NOW + BACKOFF_429
                status = why
            else:
                status = why
        if not owner:
            # Chưa xác minh được thì KHÔNG gắn vào túi nào theo .claude.json (có thể đã cũ).
            acct = (load_json(cjson) or {}).get("oauthAccount") or {}
            p = pool_for("chưa xác minh", f"lane:{name}", "")
            p["orgName"] = f"{label} — file ghi {acct.get('emailAddress', '?')}, chưa xác minh chủ token"
            add_loc(p, "cli", label, status or "chưa xác minh được chủ token")
            cache[cdir] = c
            continue
        email, org_uuid, org_name = owner
        if org_uuid and org_name:
            org_names.setdefault(org_uuid, org_name)
        p = pool_for(email, org_uuid, org_name)
        if c.get("at"):
            offer(p, c.get("five"), c.get("seven"), c.get("scoped"), c["at"], "lane CLI")
        stale = p["updatedAt"] is None or NOW - p["updatedAt"] > fresh_sec
        # Hạn mức hỏi quota tính theo TÀI KHOẢN (~30 lần/giờ, dùng chung mọi chỗ). Tài
        # khoản đã nằm trong kho thì claude-swap đã hỏi giùm: lane đừng hỏi thêm.
        if stale and token and email in kho_emails:
            status = "tài khoản này có trong kho, dùng số của kho để đỡ tốn lượt hỏi"
        elif stale and token and NOW < (c.get("backoffUntil") or 0):
            status = "Anthropic đang giới hạn lượt hỏi, tạm nghỉ hỏi lane này"
        elif stale and token:
            five, seven, scoped, status = fetch_usage(token)
            if not status:
                offer(p, five, seven, scoped, NOW, "lane CLI")
                c.update(at=NOW, five=five, seven=seven, scoped=scoped)
            elif "HTTP 429" in status:
                c["backoffUntil"] = NOW + BACKOFF_429
        acct = (load_json(cjson) or {}).get("oauthAccount") or {}
        if acct.get("emailAddress") and (acct.get("emailAddress") != email or acct.get("organizationUuid") != org_uuid):
            status = (status + "; " if status else "") + \
                f"file .claude.json còn ghi {acct.get('emailAddress')} / {acct.get('organizationName')} (đã cũ)"
        add_loc(p, "cli", label, status)
        cache[cdir] = c
        token = None
    save_cache(cache)


# ---------- 3. app Claude desktop ----------
def app_label(dirname):
    """Nhãn bản app; None cho thư mục không phải một bản đánh số (không theo mẫu Claude-NN). Thư mục đặt tên
    khác khai số trong cấu hình, mục "banApp" (vd. {"Claude-khac": "02"})."""
    if dirname == "Claude":
        return "App Claude 01"
    alias = (CONFIG.get("banApp") or {}).get(dirname)
    if alias:
        return f"App Claude {str(alias).zfill(2)}"
    suffix = dirname.split("-", 1)[1] if "-" in dirname else ""
    return f"App Claude {suffix}" if suffix.isdigit() and len(suffix) == 2 else None


apps_idle = []      # bản app chưa đăng nhập nick nào


def app_login(d):
    """(accountUuid, orgUuid) app đang đăng nhập = thư mục của phiên Code tạo gần nhất
    (claude-code-sessions/<tài khoản>/<tổ chức>/local_*.json); app không ghi tổ chức hiện tại ra chỗ khác."""
    files = glob.glob(os.path.join(d, "claude-code-sessions", "*", "*", "local_*.json"))
    if not files:
        return None
    parts = max(files, key=os.path.getmtime).split(os.sep)
    return parts[-3], parts[-2]


def collect_apps():
    base = os.path.join(HOME, "Library", "Application Support")
    for d in sorted(glob.glob(os.path.join(base, "Claude*"))):
        label = app_label(os.path.basename(d))
        if not label:
            continue
        cfg = load_json(os.path.join(d, "config.json")) or {}
        hist = load_json(os.path.join(d, "plan-usage-history.json")) or {}
        sample = (hist.get("samples") or [None])[-1] or {}
        login = app_login(d)
        acct = (login or (None, None))[0] or cfg.get("lastKnownAccountUuid") or ""
        org = (login or (None, None))[1] or sample.get("org") or ""
        if not acct or not org:
            apps_idle.append(label)
            continue
        p = pool_for(acc_email.get(acct, "?"), org, org_names.get(org))
        if sample.get("org") == org:      # số app ghi chỉ dùng khi đúng tổ chức đang đăng nhập
            u = sample.get("u") or {}
            offer(p, window(u.get("fh")), window(u.get("sd")), [], (sample.get("t") or 0) / 1000.0, "app desktop")
        add_loc(p, "app", label)


# ---------- 4. phiên Code đang mở trong app desktop ----------
def live_app_tokens():
    """(email, orgUuid) -> [(startedAt, token)] từ biến môi trường các phiên app đang chạy, mới nhất trước.
    Token trong biến môi trường là bản lúc phiên khởi động; phiên chạy lâu có thể cầm token đã hết hạn."""
    import re
    found = {}
    for f in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        rec = load_json(f) or {}
        if rec.get("entrypoint") != "claude-desktop" or not rec.get("pid"):
            continue
        try:
            out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(rec["pid"])],
                                 capture_output=True, text=True, timeout=3).stdout
        except Exception:
            continue
        env = dict(re.findall(r"\b(CLAUDE_CODE_(?:OAUTH_TOKEN|ORGANIZATION_UUID|USER_EMAIL|ACCOUNT_UUID))=(\S+)", out))
        email = env.get("CLAUDE_CODE_USER_EMAIL") or acc_email.get(env.get("CLAUDE_CODE_ACCOUNT_UUID", ""))
        if not env.get("CLAUDE_CODE_OAUTH_TOKEN") or not email:
            continue
        found.setdefault((email, env.get("CLAUDE_CODE_ORGANIZATION_UUID", "")), []).append(
            (rec.get("startedAt") or 0, env["CLAUDE_CODE_OAUTH_TOKEN"]))
    for v in found.values():
        v.sort(key=lambda x: -x[0])
    return found


def collect_app_sessions(fresh_sec):
    cache = load_cache()
    for (email, org), tokens in live_app_tokens().items():
        p = pool_for(email, org, org_names.get(org))
        key = f"app:{email}|{org}"
        c = cache.get(key) or {}
        if c.get("at"):
            offer(p, c.get("five"), c.get("seven"), c.get("scoped"), c["at"], "phiên app đang mở")
        if p["updatedAt"] is not None and NOW - p["updatedAt"] <= fresh_sec:
            continue
        if NOW < (c.get("backoffUntil") or 0):
            p["status"] = "Anthropic đang giới hạn lượt hỏi, tạm nghỉ hỏi túi này"
            continue
        status = ""
        for _, token in tokens[:3]:      # token cũ hết hạn (401) thì thử phiên mở sau
            five, seven, scoped, status = fetch_usage(token)
            if not status:
                offer(p, five, seven, scoped, NOW, "phiên app đang mở")
                c.update(at=NOW, five=five, seven=seven, scoped=scoped)
                break
            if "HTTP 429" in status:
                c["backoffUntil"] = NOW + BACKOFF_429
                break
        if status:
            p["status"] = status
        cache[key] = c
    save_cache(cache)


# ---------- 5, 6. Codex và Antigravity ----------
others = []         # thẻ quota ngoài Claude, cùng dạng: groups[].windows[] (pct = % ĐÃ DÙNG)


def find_cli(name, extra):
    """Lệnh `name`: khai trong cấu hình (khoá trùng tên lệnh), PATH, rồi các chỗ cài quen thuộc.
    App mở máy chủ với PATH tối giản của macOS nên PATH thường không có ~/.local/bin."""
    for c in [CONFIG.get(name), shutil.which(name)] + extra:
        c = os.path.expanduser(c) if c else None
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def window_name(mins=None, key=""):
    key = str(key).lower()
    if mins == 300 or key in ("5h", "session"):
        return "5 giờ"
    if mins == 10080 or key in ("weekly", "week"):
        return "Tuần"
    if mins == 43200 or key in ("monthly", "month"):
        return "Tháng"
    return f"{round(mins / 60)} giờ" if mins else (key or "?")


def codex_rpc(codex):
    """(account, rateLimits, lỗi) từ `codex app-server`. Đọc thô từ ống để không kẹt vì bộ đệm dòng."""
    try:
        p = subprocess.Popen([codex, "-s", "read-only", "-a", "never", "app-server"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir())
    except OSError as e:
        return None, None, f"không chạy được codex ({type(e).__name__})"
    fd, buf, end = p.stdout.fileno(), [b""], time.time() + 25

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
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "van-phong-claude", "version": "1"}}})
        if not wait(1):
            return None, None, "codex app-server không trả lời"
        send({"method": "initialized"})
        send({"id": 2, "method": "account/read", "params": {}})
        acc = wait(2) or {}
        send({"id": 3, "method": "account/rateLimits/read"})
        lim = wait(3) or {}
    except (OSError, ValueError) as e:
        return None, None, f"codex app-server lỗi ({type(e).__name__})"
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
    if lim.get("error") or not lim.get("result"):
        why = (lim.get("error") or {}).get("message") or "không đọc được giới hạn"
        return (acc.get("result") or {}).get("account"), None, f"codex: {why[:120]}"
    return (acc.get("result") or {}).get("account"), lim["result"], ""


def codex_windows(lim, name_key="windowDurationMins", pct_key="usedPercent", reset_key="resetsAt"):
    out = []
    for k in ("primary", "secondary"):
        w = (lim or {}).get(k)
        if w and w.get(pct_key) is not None:
            out.append({"name": window_name(w.get(name_key)), "pct": float(w[pct_key]), "resetsAt": w.get(reset_key)})
    return out


def codex_from_sessions(home):
    """Số Codex ghi lần cuối trong nhật ký phiên (sự kiện token_count), kèm giờ ghi."""
    files = sorted(glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl")),
                   key=lambda f: os.path.getmtime(f), reverse=True)[:5]
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 400_000))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            rl = (ev.get("payload") or {}).get("rate_limits") or {}
            wins = codex_windows(rl, "window_minutes", "used_percent", "resets_at")
            if wins:
                return {"groups": [{"name": "", "windows": wins}], "plan": rl.get("plan_type") or "",
                        "updatedAt": iso_to_epoch(ev.get("timestamp")) or os.path.getmtime(f)}
    return None


def collect_codex():
    home = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    codex = find_cli("codex", [os.path.join(HOME, ".local", "bin", "codex"), "/opt/homebrew/bin/codex",
                               "/usr/local/bin/codex", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                               "/Applications/Codex.app/Contents/Resources/codex"])
    if not codex and not os.path.isdir(home):
        return None
    card = {"provider": "codex", "title": "Codex", "email": "", "plan": "", "groups": [], "extras": [],
            "source": "", "updatedAt": None, "status": ""}
    acc, lim, why = codex_rpc(codex) if codex else (None, None, "không thấy lệnh codex")
    if acc:
        card["email"] = acc.get("email") or ""
        card["plan"] = acc.get("planType") or ""
    if lim:
        rl = lim.get("rateLimits") or {}
        card["plan"] = rl.get("planType") or card["plan"]
        groups = [{"name": "", "windows": codex_windows(rl)}]
        # Giới hạn riêng từng model (vd. bản Spark) nằm cạnh giới hạn chung.
        for lid, v in (lim.get("rateLimitsByLimitId") or {}).items():
            if lid != rl.get("limitId") and codex_windows(v):
                groups.append({"name": v.get("limitName") or lid, "windows": codex_windows(v)})
        card["groups"] = [g for g in groups if g["windows"]]
        cr = rl.get("credits") or {}
        if cr.get("unlimited"):
            card["extras"].append("Credits không giới hạn")
        elif cr.get("hasCredits") and cr.get("balance"):
            card["extras"].append(f"Credits còn {float(cr['balance']):,.0f}".replace(",", "."))
        n = (lim.get("rateLimitResetCredits") or {}).get("availableCount")
        if n:
            card["extras"].append(f"{n} lượt reset quota miễn phí")
        if rl.get("rateLimitReachedType"):
            card["status"] = "Codex báo đang chạm giới hạn"
        card.update(source="codex app-server", updatedAt=NOW)
    else:
        # Kho nick Codex dùng chung thư mục phiên với ~/.codex: số trong nhật ký có thể là của nick kho.
        old = None if kho_nicks_exist() else codex_from_sessions(home)
        if old:
            card.update(groups=old["groups"], plan=card["plan"] or old["plan"], updatedAt=old["updatedAt"],
                        source="nhật ký phiên Codex")
        card["status"] = why
    return card if card["groups"] or card["email"] else None


def ag_call(port, tok, method):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False            # máy chủ cục bộ dùng chứng chỉ tự ký, chỉ nghe ở 127.0.0.1
    ctx.verify_mode = ssl.CERT_NONE
    import urllib.request
    body = json.dumps({"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                                    "locale": "en", "ideVersion": "unknown"}}).encode()
    req = urllib.request.Request(f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}",
                                 data=body, headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                                                     "X-Codeium-Csrf-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def ag_from_app():
    """(summary, userStatus) từ máy chủ ngôn ngữ của app Antigravity đang mở, hoặc (None, None)."""
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None, None
    for line in out.splitlines():
        if "/Antigravity.app/" not in line or "language_server" not in line or "--csrf_token" not in line:
            continue
        a = line.split()
        i = a.index("--csrf_token")
        if i + 1 >= len(a):
            continue
        pid, tok = a[0], a[i + 1]
        try:
            lo = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        for port in re.findall(r"127\.0\.0\.1:(\d+) \(LISTEN\)", lo):
            summary = ag_call(port, tok, "RetrieveUserQuotaSummary")
            if summary:
                return summary.get("response") or summary, (ag_call(port, tok, "GetUserStatus") or {}).get("userStatus") or {}
    return None, None


def ag_from_cli(agy):
    """Báo cáo quota của `agy -p /usage` trong thư mục tạm; chặn mở trình duyệt nếu agy đòi đăng nhập lại."""
    with tempfile.TemporaryDirectory(prefix="van-phong-agy-") as tmp:
        stub = os.path.join(tmp, "open")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(stub, 0o755)
        env = dict(os.environ, AGY_CLI_DISABLE_AUTO_UPDATE="true", BROWSER=stub,
                   PATH=tmp + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"))
        try:
            p = subprocess.Popen([agy, "-p", "/usage", "--output-format", "json"], cwd=tmp, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            try:
                out, _ = p.communicate(timeout=60)
            finally:
                try:
                    os.killpg(p.pid, 15)     # dọn mọi tiến trình con agy kịp mở (máy chủ MCP…)
                except OSError:
                    pass
            d = json.loads(out.decode() or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return None, f"agy không trả báo cáo quota ({type(e).__name__})"
    data = ((d.get("command") or {}).get("data")) or {}
    if d.get("status") != "SUCCESS" or not data.get("groups"):
        return None, "agy chưa đăng nhập hoặc không trả báo cáo quota"
    return {"groups": [{"displayName": g.get("name"), "buckets": [
        {"window": b.get("window"), "bucketId": b.get("id"), "remainingFraction": b.get("remaining_fraction"),
         "resetTime": b.get("reset_time")} for b in g.get("buckets") or []]} for g in data["groups"]]}, ""


def ag_groups(summary):
    groups = []
    for g in summary.get("groups") or []:
        name = re.sub(r"\s+models?$", "", g.get("displayName") or "", flags=re.I).replace(" and ", "/")
        wins = []
        for b in g.get("buckets") or []:
            frac = b.get("remainingFraction")
            if frac is None:
                continue
            reset = b.get("resetTime")
            wins.append({"name": window_name(key=b.get("window") or b.get("bucketId", "").split("-")[-1]),
                         "pct": round((1 - float(frac)) * 100, 1),
                         "resetsAt": iso_to_epoch(reset) if isinstance(reset, str) else reset})
        wins.sort(key=lambda w: w["name"] != "5 giờ")
        if wins:
            groups.append({"name": name, "windows": wins})
    return groups


def collect_antigravity():
    agy = find_cli("agy", [os.path.join(HOME, ".local", "bin", "agy"), "/opt/homebrew/bin/agy", "/usr/local/bin/agy"])
    card = {"provider": "antigravity", "title": "Antigravity", "email": "", "plan": "", "groups": [], "extras": [],
            "source": "", "updatedAt": None, "status": ""}
    summary, user = ag_from_app()
    if summary:
        card.update(source="app Antigravity", email=user.get("email") or "",
                    plan=(user.get("userTier") or {}).get("name")
                    or ((user.get("planStatus") or {}).get("planInfo") or {}).get("planName") or "")
    elif agy:
        summary, why = ag_from_cli(agy)
        card.update(source="agy CLI", status=why)
    elif not os.path.isdir("/Applications/Antigravity.app"):
        return None
    else:
        card["status"] = "app Antigravity đang đóng và không thấy lệnh agy"
    if summary:
        card.update(groups=ag_groups(summary), updatedAt=NOW)
    return card


def kho_nicks_exist():
    d = os.path.join(HOME, ".codex-kho", "nicks")
    return os.path.isdir(d) and any(x.isdigit() for x in os.listdir(d))


def collect_codex_kho(fresh_sec):
    """Kho nhiều nick Codex (lệnh `codex-kho`): mỗi nick một thẻ. Chỉ đọc số kho đã lưu; có nick cũ
    quá fresh_sec giây thì nhờ `codex-kho quota` hỏi lại (qua codex app-server, không gọi model)."""
    kho = find_cli("codex-kho", [os.path.join(HOME, ".local", "bin", "codex-kho"),
                                 os.path.join(HOME, "cc-orchestrator", "scripts", "codex-kho")])
    if not kho:
        return []

    def read(cmd, timeout):
        try:
            r = subprocess.run([kho] + cmd, capture_output=True, text=True, timeout=timeout,
                               cwd=tempfile.gettempdir())
            return json.loads(r.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    data = read(["list", "--json"], 20)
    if not data or not data.get("nicks"):
        return []
    if any(NOW - (n.get("updatedAt") or 0) >= fresh_sec for n in data["nicks"]):
        data = read(["quota", "--json"], 90) or data
    cards = []
    for n in data["nicks"]:
        wins = [{"name": window_name(w.get("mins")), "pct": float(w["pct"]), "resetsAt": w.get("resetsAt")}
                for w in sorted(n.get("windows") or [], key=lambda w: w.get("mins") or 0)]
        pick = data.get("pick") == n["n"]
        extras = [f"Nick {n['n']}"] + ([f"đang chạy {n['busy']} lượt"] if n.get("busy") else []) \
            + (["→ lượt sau chạy nick này"] if pick else [])
        cards.append({"provider": "codex", "kho": n["n"], "pick": pick, "title": n.get("workspace") or "Cá nhân",
                      "email": n.get("email", ""), "plan": n.get("plan", ""),
                      "groups": [{"name": "", "windows": wins}] if wins else [], "extras": extras,
                      "source": "kho nick Codex", "updatedAt": n.get("updatedAt"), "status": n.get("why", "")})
    return cards


def collect_others(fresh_sec):
    """Codex và Antigravity: dùng số đã đệm nếu chưa quá fresh_sec giây, không thì hỏi lại."""
    cache = load_cache()
    for key, fn in (("codex", collect_codex), ("antigravity", collect_antigravity)):
        c = cache.get(key) or {}
        card = c.get("card") if NOW - (c.get("at") or 0) < fresh_sec else None
        if card is None:
            card = fn()
            if card and card.get("groups"):
                cache[key] = {"at": NOW, "card": card}
            elif card and (c.get("card") or {}).get("groups"):
                # Hỏi lần này hỏng: giữ số cũ, kèm lý do.
                card = dict(c["card"], status=card.get("status") or "lần hỏi mới nhất không được")
        if card:
            others.append(card)
    save_cache(cache)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh-sec", type=int, default=1800,
                    help="túi đã có số mới hơn ngần này giây thì khỏi hỏi Anthropic qua lane CLI")
    ap.add_argument("--fresh-other", type=int, default=600,
                    help="số quota Codex, Antigravity mới hơn ngần này giây thì khỏi hỏi lại")
    args = ap.parse_args()
    collect_kho()
    collect_apps()
    collect_app_sessions(args.fresh_sec)
    collect_lanes(args.fresh_sec)
    collect_others(args.fresh_other)
    codex_kho = collect_codex_kho(args.fresh_other)
    for c in others + codex_kho:
        c["ageSec"] = None if c["updatedAt"] is None else max(0, int(NOW - c["updatedAt"]))
        vals = [w["pct"] for g in c["groups"] for w in g["windows"]]
        c["maxPct"] = max(vals) if vals else None
    out = []
    for p in pools.values():
        if not p["orgName"]:
            p["orgName"] = org_names.get(p["orgUuid"]) or (f"Team {p['orgUuid'][:8]}" if p["orgUuid"] else "?")
        p["ageSec"] = None if p["updatedAt"] is None else max(0, int(NOW - p["updatedAt"]))
        vals = [w["pct"] for w in [p["fiveHour"], p["sevenDay"]] if w] + [s["pct"] for s in p["scoped"]]
        p["maxPct"] = max(vals) if vals else None
        p["inKho"] = any(l["kind"] == "kho" for l in p["locations"])
        out.append(p)
    out.sort(key=lambda p: (not p["inKho"], p["email"], p["orgName"]))
    json.dump({"generatedAt": NOW, "maxPct": float(os.environ.get("KHO_MAX_PCT", "80")),
               "pools": out, "others": others, "codexKho": codex_kho, "appsIdle": apps_idle, "errors": errors},
              sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()

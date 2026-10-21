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

Không gọi model nào, không tốn token LLM.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh-sec", type=int, default=1800,
                    help="túi đã có số mới hơn ngần này giây thì khỏi hỏi Anthropic qua lane CLI")
    args = ap.parse_args()
    collect_kho()
    collect_apps()
    collect_app_sessions(args.fresh_sec)
    collect_lanes(args.fresh_sec)
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
               "pools": out, "appsIdle": apps_idle, "errors": errors}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()

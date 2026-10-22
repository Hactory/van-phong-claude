#!/usr/bin/env python3
"""Eval hiệu năng máy chủ: so bản gốc (một git ref) với bản đang sửa trên CÙNG dữ liệu thật của máy.

Hai thứ phải đúng cùng lúc:
  1. Đầu ra giống hệt: build_state (24 giờ, 14 ngày) và session_detail của vài phiên mẫu, bỏ qua mốc giờ
     dựng ("at"). Dữ liệu sống có thể đổi giữa hai lần gọi, nên mỗi phép so chạy gốc, mới, gốc; hai lần
     gốc khác nhau thì thử lại.
  2. Nhẹ hơn: thời gian CPU (không tính lúc chờ đĩa) lần đầu, đo trong tiến trình riêng nên đệm nguội thật,
     và trung vị khi đệm đã ấm. Đo ấm xen kẽ gốc/mới từng lần, có chờ giữa các lần như trang thật
     (mặc định 0,5 giây) để các phiên đang chạy kịp ghi thêm.

    python3 tools/eval_hieu_nang.py               # gốc = HEAD
    python3 tools/eval_hieu_nang.py --goc <ref> --lan 15 --cho 0.5
"""
import argparse
import copy
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def nap(ten, path):
    spec = importlib.util.spec_from_file_location(ten, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[ten] = mod
    spec.loader.exec_module(mod)
    return mod


def ban_goc(ref):
    src = subprocess.run(["git", "-C", HERE, "show", f"{ref}:server.py"], capture_output=True, text=True, check=True).stdout
    d = tempfile.mkdtemp(prefix="vp-eval-")
    path = os.path.join(d, "server.py")
    with open(path, "w") as f:
        f.write(src)
    for sub in ("web", "tools", "collect.py"):         # để HERE của bản gốc trỏ đúng tài nguyên
        os.symlink(os.path.join(HERE, sub), os.path.join(d, sub))
    return path


def chuan(x):
    """Bỏ phần chỉ khác vì thời điểm gọi."""
    x = copy.deepcopy(x)
    if isinstance(x, dict):
        x.pop("at", None)
    return json.loads(json.dumps(x, sort_keys=True, ensure_ascii=False))


def khac_dau(a, b, path="", out=None, toi_da=5):
    out = [] if out is None else out
    if len(out) >= toi_da:
        return out
    if type(a) is not type(b):
        out.append(f"{path}: kiểu {type(a).__name__} ≠ {type(b).__name__}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: chỉ có ở {'gốc' if k in a else 'mới'}")
            else:
                khac_dau(a[k], b[k], f"{path}.{k}", out, toi_da)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} phần tử ≠ {len(b)}")
        for i, (u, v) in enumerate(zip(a, b)):
            khac_dau(u, v, f"{path}[{i}]", out, toi_da)
    elif a != b:
        out.append(f"{path}: {str(a)[:60]!r} ≠ {str(b)[:60]!r}")
    return out


def so_dau_ra(ten, f_goc, f_moi, thu=4):
    for _ in range(thu):
        g1, m, g2 = chuan(f_goc()), chuan(f_moi()), chuan(f_goc())
        if g1 != g2:
            continue                        # dữ liệu đang đổi, thử lại
        if g1 == m:
            return True, []
        return False, khac_dau(g1, m)
    return None, ["dữ liệu đổi liên tục, chưa so được"]


def do_am(f_goc, f_moi, lan, cho):
    """Trung vị thời gian CPU, xen kẽ gốc và mới để máy bận lúc nào thì cả hai cùng chịu."""
    g, m = [], []
    for i in range(lan):
        for f, ds in ((f_goc, g), (f_moi, m)) if i % 2 == 0 else ((f_moi, m), (f_goc, g)):
            time.sleep(cho)
            t = time.process_time()
            f()
            ds.append(time.process_time() - t)
    return statistics.median(g), statistics.median(m)


DO_NGUOI = """
import sys, time, importlib.util
spec = importlib.util.spec_from_file_location("s", sys.argv[1]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
for h in (24, 336):
    t = time.process_time(); m.build_state(h); print(time.process_time() - t)
"""


def do_nguoi(path):
    out = subprocess.run([sys.executable, "-c", DO_NGUOI, path], capture_output=True, text=True, check=True).stdout
    return [float(x) for x in out.split()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goc", default="HEAD")
    ap.add_argument("--lan", type=int, default=15)
    ap.add_argument("--cho", type=float, default=0.5)
    a = ap.parse_args()
    path_goc, path_moi = ban_goc(a.goc), os.path.join(HERE, "server.py")
    ng, nm = do_nguoi(path_goc), do_nguoi(path_moi)
    goc = nap("server_goc", path_goc)
    moi = nap("server_moi", path_moi)

    # Phiên mẫu: vài phiên Claude (đang chạy trước) và phiên Codex trong khung 14 ngày.
    st = moi.build_state(336)
    ss = [s for p in st["projects"] for s in p["sessions"]]
    order = {"lam": 0, "cho": 1, "xem": 2, "ranh": 3, "nghi": 4}
    claude = sorted([s for s in ss if s.get("kind") != "codex"], key=lambda s: order[s["status"]])[:4]
    codex = [s for s in ss if s.get("kind") == "codex"][:2]
    mau = [s["id"] for s in claude + codex]

    viec = [("build_state 24 giờ", lambda m: m.build_state(24)),
            ("build_state 14 ngày", lambda m: m.build_state(336))]
    viec += [(f"session_detail {sid[:14]}", (lambda sid: lambda m: m.session_detail(sid))(sid)) for sid in mau]

    print(f"Gốc: {a.goc} · mới: bản đang sửa · thời gian CPU · {a.lan} lần đo ấm, chờ {a.cho}s\n")
    print(f"Lần đầu (đệm nguội): 24 giờ {ng[0]*1000:.0f} → {nm[0]*1000:.0f} ms · 14 ngày {ng[1]*1000:.0f} → {nm[1]*1000:.0f} ms\n")
    print(f"{'việc':34s} {'đầu ra':10s} {'gốc ấm':>9s} {'mới ấm':>9s} {'giảm':>6s}")
    tong_dung = True
    for ten, f in viec:
        ok, chi_tiet = so_dau_ra(ten, lambda: f(goc), lambda: f(moi))
        ga, ma = do_am(lambda: f(goc), lambda: f(moi), a.lan, a.cho)
        nhan = {True: "giống ✓", False: "KHÁC ✗", None: "chưa so"}[ok]
        tong_dung &= ok is not False
        print(f"{ten:34s} {nhan:10s} {ga*1000:7.1f}ms {ma*1000:7.1f}ms {(1 - ma / ga) * 100 if ga else 0:5.0f}%")
        for c in chi_tiet:
            print("    ", c)
    print("\nKẾT LUẬN:", "đầu ra giữ nguyên" if tong_dung else "CÓ KHÁC BIỆT, không được nhận bản mới")
    sys.exit(0 if tong_dung else 1)


if __name__ == "__main__":
    main()

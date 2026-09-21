#!/usr/bin/env python3
"""從正式站 Caddy JSON 存取日誌重建課堂點名系統的匿名化時間線＋漏斗彙總＋錯誤原因對照表。
純標準庫，不連任何伺服器——用法裡的 ssh 是抓日誌的方式，這支程式只吃 stdin。

用法：
  ssh your-server 'sudo zcat /var/log/caddy/attend*.gz; sudo cat /var/log/caddy/attend.log /var/log/caddy/attend.log.1' \\
    | python3 tools/log_funnel.py --date 2026-09-10 --from 15:00 --to 15:45

輸入：每行一個 Caddy access-log JSON 物件，用到的欄位＝ts、status、size、duration、
request.method、request.uri、request.headers（User-Agent、Cf-Connecting-Ip）、request.client_ip。
輸出三段：
  1. 匿名化時間線（IP 取 sha1 前四碼、UA 只留類別、路徑裡的簽章遮成 SIG、排除 /static／/healthz／
     Gatus／老師端輪詢）
  2. 漏斗彙總（裝置數、QR 落地 200/410、改走 /c、/api/checkin 各狀態碼、第一次落地→第一次簽到 200
     的秒數分佈、落地成功卻沒簽到、同裝置多次簽到疑似替人簽）
  3. 用回應大小反推錯誤原因：從同目錄上一層 app.py 抓所有 HTTPException(4xx/5xx, "字串") 建
     {"ok":false,"error":"…"} 的 UTF-8 位元組長度對照表，拿日誌裡的 4xx/5xx size 回查
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))  # Asia/Taipei，跟 app.py 自己用的一樣，不靠系統時區資料庫

EXCLUDE_EXACT_PATHS = {"/healthz"}
TEACHER_POLL_RE = re.compile(r"^/api/t/session/\d+/(qr|state)$")  # 投影頁每秒、老師頁每 3 秒輪詢
SIG_RE = re.compile(r"^(/s/\d+/\d+/)[0-9a-fA-F]+$")
LAND_MASKED_RE = re.compile(r"^/s/\d+/\d+/SIG$")
STUDENT_MASKED_RE = re.compile(r"^(/s/\d+/\d+/SIG|/c|/api/checkin|/api/whoami|/me|/help)$")


def is_gatus(ua: str) -> bool:
    return "gatus" in ua.lower()


def should_exclude(path: str, ua: str) -> bool:
    if path.startswith("/static/"):
        return True
    if path in EXCLUDE_EXACT_PATHS:
        return True
    if TEACHER_POLL_RE.match(path):
        return True
    if is_gatus(ua):
        return True
    return False


def classify_ua(ua: str) -> str:
    """回傳裝置類別：iOS-Safari／iOS-Chrome／iOS-INAPP／Android-<瀏覽器>[-WebView]／LINE／FB-IG／Mac／Win／other。"""
    if not ua:
        return "other"
    low = ua.lower()
    if "line/" in low or low.startswith("line") or " line/" in low:
        return "LINE"
    if "fban" in low or "fbav" in low or "instagram" in low:
        return "FB-IG"
    if "iphone" in low or "ipad" in low or "ipod" in low:
        if "crios" in low:
            return "iOS-Chrome"
        if "fxios" in low:
            return "iOS-Firefox"
        if "safari" in low:
            return "iOS-Safari"
        return "iOS-INAPP"  # 原生 WKWebView 通常不帶 Safari/ 版本號後綴
    if "android" in low:
        wv = "; wv)" in low or ";wv)" in low
        if "samsungbrowser" in low:
            name = "SamsungInternet"
        elif "vivobrowser" in low:
            name = "vivoBrowser"
        elif "miuibrowser" in low:
            name = "MIUIBrowser"
        elif "heytapbrowser" in low:
            name = "HeytapBrowser"
        elif "huaweibrowser" in low:
            name = "HuaweiBrowser"
        elif "edga/" in low or "edg/" in low:
            name = "Edge"
        elif "firefox" in low:
            name = "Firefox"
        elif "chrome" in low and "crios" not in low:
            name = "Chrome"
        else:
            name = None
        if name is None:
            return "Android-WebView" if wv else "Android-Browser"
        return f"Android-{name}-WebView" if wv else f"Android-{name}"
    if "macintosh" in low:
        return "Mac"
    if "windows" in low:
        return "Win"
    return "other"


def sha1_4(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:4]  # noqa: S324 — 匿名化用途，不是密碼雜湊


def get_header(headers: dict, name: str) -> str:
    if not headers:
        return ""
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            if isinstance(v, list):
                return v[0] if v else ""
            return v or ""
    return ""


def client_ip_of(entry: dict) -> str:
    req = entry.get("request") or {}
    headers = req.get("headers") or {}
    cf = get_header(headers, "Cf-Connecting-Ip")
    if cf:
        return cf
    return req.get("client_ip") or req.get("remote_ip") or ""


def mask_uri(path: str) -> str:
    return SIG_RE.sub(lambda m: m.group(1) + "SIG", path)


def local_dt(ts) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=TZ)


def read_entries(fh):
    for raw_line in fh:
        line = raw_line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ───────────────────────── 從 app.py 反推錯誤字串對照表 ─────────────────────────

HTTPEXC_RE = re.compile(r"HTTPException\(\s*(\d{3})\s*,\s*(.+)\)\s*(?:#.*)?$")


def _classify_arg(status: int, arg: str, lineno: int, out: list, note: str = "") -> None:
    arg = arg.strip()
    if re.match(r'^f["\']', arg):
        out.append({"status": status, "kind": "fstring", "raw": arg, "lineno": lineno, "note": note})
        return
    m = re.match(r'^"((?:[^"\\]|\\.)*)"$', arg) or re.match(r"^'((?:[^'\\]|\\.)*)'$", arg)
    if m:
        try:
            text = ast.literal_eval(arg)
        except (ValueError, SyntaxError):
            out.append({"status": status, "kind": "other", "raw": arg, "lineno": lineno, "note": note})
            return
        out.append({"status": status, "kind": "literal", "text": text, "lineno": lineno, "note": note})
        return
    if re.match(r"^[A-Za-z_]\w*$", arg):
        out.append({"status": status, "kind": "variable", "raw": arg, "lineno": lineno, "note": note})
        return
    out.append({"status": status, "kind": "other", "raw": arg, "lineno": lineno, "note": note})


def extract_http_exceptions(app_py_text: str) -> list:
    """逐行掃 raise HTTPException(4xx/5xx, ...)。跨行呼叫、非字面值一律標記略過，不猜內容。"""
    out: list = []
    for lineno, line in enumerate(app_py_text.splitlines(), 1):
        m = HTTPEXC_RE.search(line)
        if not m:
            continue
        status = int(m.group(1))
        if status < 400:
            continue
        arg = m.group(2).strip()
        if arg.count("(") != arg.count(")"):
            out.append({"status": status, "kind": "other", "raw": arg, "lineno": lineno, "note": "括號不平衡，可能跨行呼叫，人工核對原始碼"})
            continue
        if " if " in arg and " else " in arg:
            # 三元運算式（本檔目前只有一處，401 的 PIN 錯誤剩幾次訊息），拆兩支分別判斷
            try:
                then_part, rest = arg.split(" if ", 1)
                _cond, else_part = rest.split(" else ", 1)
                _classify_arg(status, then_part, lineno, out, note="條件運算式的 if 分支")
                _classify_arg(status, else_part, lineno, out, note="條件運算式的 else 分支")
            except ValueError:
                out.append({"status": status, "kind": "other", "raw": arg, "lineno": lineno, "note": "條件運算式拆解失敗，人工核對"})
            continue
        _classify_arg(status, arg, lineno, out)
    return out


def build_size_table(exceptions: list):
    table: dict = {}  # (status, size) -> [text, ...]
    notes: list = []
    for e in exceptions:
        if e["kind"] == "literal":
            payload = json.dumps({"ok": False, "error": e["text"]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            table.setdefault((e["status"], len(payload)), []).append(e["text"])
        elif e["kind"] == "fstring":
            extra = f"（{e['note']}）" if e.get("note") else ""
            notes.append(f"app.py:{e['lineno']} status={e['status']} 略過（f-string，內容依請求而變）{extra}：{e['raw']}")
        elif e["kind"] == "variable":
            notes.append(f"app.py:{e['lineno']} status={e['status']} 略過（引用變數 {e['raw']}，多半是別處組好的 f-string）")
        else:
            extra = f"（{e['note']}）" if e.get("note") else ""
            notes.append(f"app.py:{e['lineno']} status={e['status']} 略過（無法靜態解析）{extra}：{e['raw']}")
    return table, notes


# ───────────────────────── 主流程 ─────────────────────────


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--date", required=True, help="YYYY-MM-DD（Asia/Taipei）")
    p.add_argument("--from", dest="time_from", required=True, help="HH:MM（Asia/Taipei）")
    p.add_argument("--to", dest="time_to", required=True, help="HH:MM（Asia/Taipei）")
    p.add_argument("--app", default=str(Path(__file__).resolve().parent.parent / "app.py"), help="app.py 路徑（預設同 repo 上一層）")
    return p.parse_args()


def percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main() -> int:
    args = parse_args()
    start = datetime.strptime(f"{args.date} {args.time_from}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    end = datetime.strptime(f"{args.date} {args.time_to}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)

    rows: list = []
    for entry in read_entries(sys.stdin):
        ts = entry.get("ts")
        if ts is None:
            continue
        dt = local_dt(ts)
        if not (start <= dt <= end):
            continue
        req = entry.get("request") or {}
        headers = req.get("headers") or {}
        path = (req.get("uri") or "").split("?", 1)[0]
        ua = get_header(headers, "User-Agent")
        ip = client_ip_of(entry)
        rows.append(
            {
                "dt": dt,
                "ip": ip,
                "dev": sha1_4(ip) if ip else "----",
                "ua_cat": classify_ua(ua),
                "method": req.get("method", ""),
                "uri_masked": mask_uri(path),
                "status": entry.get("status"),
                "size": entry.get("size"),
                "excluded": should_exclude(path, ua),
            }
        )
    rows.sort(key=lambda r: r["dt"])

    # ── 1. 匿名化時間線 ──
    print(f"=== 匿名化時間線（{args.date} {args.time_from}–{args.time_to} Asia/Taipei）===")
    excluded_n = 0
    for r in rows:
        if r["excluded"]:
            excluded_n += 1
            continue
        status = r["status"] if r["status"] is not None else "?"
        size = r["size"] if r["size"] is not None else "?"
        print(f"{r['dt'].strftime('%H:%M:%S')}  {r['dev']}  {r['ua_cat']:<20} {r['method']:<5} {r['uri_masked']:<42} {status}  {size}B")
    print(f"（排除 /static、/healthz、Gatus、老師端輪詢共 {excluded_n} 筆；以上只列學生可能看到的請求）")
    print()

    kept = [r for r in rows if not r["excluded"]]

    # ── 2. 漏斗彙總 ──
    print("=== 漏斗彙總 ===")
    # B064：打過老師端路徑的出口就是老師的機器（老師自己開 /c 看畫面不算學生）；用 rows 不用 kept，輪詢也算數
    teacher_devs = {r["dev"] for r in rows if r["uri_masked"] == "/t" or r["uri_masked"].startswith(("/t/", "/api/t/"))}
    by_dev = defaultdict(list)
    for r in kept:
        if STUDENT_MASKED_RE.match(r["uri_masked"]) and r["dev"] not in teacher_devs:
            by_dev[r["dev"]].append(r)
    devices = sorted(by_dev)
    print(f"不同學生裝置數：{len(devices)}")

    land = [r for r in kept if r["method"] == "GET" and LAND_MASKED_RE.match(r["uri_masked"])]
    land_status = Counter(r["status"] for r in land)
    n200, n410 = land_status.get(200, 0), land_status.get(410, 0)
    pct410 = (n410 / len(land) * 100) if land else 0.0
    print(f"QR 落地（GET /s/…）：{len(land)} 次（200: {n200}、410: {n410}，過期比例 {pct410:.0f}%）")
    land_by_cat = defaultdict(Counter)
    for r in land:
        land_by_cat[r["ua_cat"]][r["status"]] += 1
    for cat, c in sorted(land_by_cat.items()):
        print("  " + cat + "：" + "、".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda kv: str(kv[0]))))

    # B064：只有送出六位數（POST /c）才算改走 /c；打開首頁會被轉到 /c，那只是路過
    c_devices = {r["dev"] for r in kept if r["uri_masked"] == "/c" and r["method"] == "POST" and r["dev"] not in teacher_devs}
    print(f"改走 /c（送出六位數）的裝置數：{len(c_devices)}")

    checkin = [r for r in kept if r["method"] == "POST" and r["uri_masked"] == "/api/checkin"]
    checkin_status = Counter(r["status"] for r in checkin)
    print("/api/checkin 各狀態碼數：" + ("、".join(f"{k}:{v}" for k, v in sorted(checkin_status.items(), key=lambda kv: str(kv[0]))) or "（無）"))

    deltas: list = []
    never_checkin: list = []
    proxy_suspect: list = []
    for dev in devices:
        entries = sorted(by_dev[dev], key=lambda r: r["dt"])
        first_landing = entries[0]["dt"]
        success = [r for r in entries if r["uri_masked"] == "/api/checkin" and r["status"] == 200]
        if success:
            first_success = min(r["dt"] for r in success)
            deltas.append((first_success - first_landing).total_seconds())
            extra = [r for r in entries if r["uri_masked"] == "/api/checkin" and r["dt"] > first_success]
            if extra:
                proxy_suspect.append((dev, len(extra)))
        else:
            # 「落地成功」＝GET /s/…/SIG 或 POST /c 拿到 200（不是失敗的 410/400），之後沒有任何 /api/checkin
            landed_ok = any(((r["uri_masked"] == "/c" and r["method"] == "POST") or LAND_MASKED_RE.match(r["uri_masked"]))
                            and r["status"] == 200 for r in entries)
            if landed_ok:
                never_checkin.append(dev)

    if deltas:
        secs = sorted(deltas)
        print(
            f"第一次落地→第一次簽到 200 秒數分佈（{len(secs)} 支）："
            f"min={secs[0]:.0f}s p50={percentile(secs, 0.5):.0f}s p90={percentile(secs, 0.9):.0f}s max={secs[-1]:.0f}s"
        )
    else:
        print("第一次落地→第一次簽到 200 秒數分佈：（這個時間窗沒有成功簽到）")

    print(f"落地成功卻從未送出簽到的裝置：{len(never_checkin)} 支" + (f"（{', '.join(never_checkin)}）" if never_checkin else ""))
    print(
        f"同一裝置成功後又送出其他簽到（疑似替人簽）：{len(proxy_suspect)} 支"
        + ("（" + "、".join(f"{d}×{n}" for d, n in proxy_suspect) + "）" if proxy_suspect else "")
    )
    print()

    # ── 3. 用回應大小反推錯誤原因 ──
    print("=== 用回應大小反推錯誤原因 ===")
    app_path = Path(args.app)
    if not app_path.is_file():
        print(f"找不到 {app_path}，略過對照表")
    else:
        exceptions = extract_http_exceptions(app_path.read_text(encoding="utf-8"))
        table, notes = build_size_table(exceptions)
        n_literal = sum(1 for e in exceptions if e["kind"] == "literal")
        print(f"從 {app_path.name} 抓到 {len(exceptions)} 條 4xx/5xx HTTPException，其中 {n_literal} 條是可算 byte 數的固定字串：")
        for (status, size), texts in sorted(table.items()):
            uniq = sorted(set(texts))
            print(f"  {size} bytes  {status}  " + " / ".join(f"「{t}」" for t in uniq))
        if notes:
            print("  略過（動態內容，byte 數依請求而變，靠這張表對不回去）：")
            for note in notes:
                print(f"    - {note}")
        print()
        print("日誌裡 4xx／5xx 的 size 對照：")
        log_4xx = Counter((r["status"], r["size"]) for r in kept if isinstance(r["status"], int) and r["status"] >= 400)
        if not log_4xx:
            print("  （這個時間窗沒有 4xx/5xx）")
        for (status, size), n in sorted(log_4xx.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0)):
            texts = table.get((status, size))
            if texts:
                print(f"  status={status} size={size}  ×{n}  → 「{'/'.join(sorted(set(texts)))}」")
            else:
                print(f"  status={status} size={size}  ×{n}  → 對照表沒命中（可能是動態內容、HTML 錯誤頁、或別的路由）")

    return 0


if __name__ == "__main__":
    sys.exit(main())

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
     的秒數分佈、落地成功卻沒簽到、送出但全被擋）
  3. 用回應大小反推錯誤原因：從同目錄上一層 app.py 抓所有 HTTPException／Reject(4xx/5xx, "字串"…) 建
     {"ok":false,"error":"…"} 的 UTF-8 位元組長度對照表，拿日誌裡的 4xx/5xx size 回查

讀這份輸出要知道的限制：
  - 日誌只有出口 IP，沒有裝置識別。「裝置」欄＝出口 IP 的雜湊，校園 NAT 與行動網路底下的多支手機會併成一個，
    所以這支工具不再做「同一裝置成功後又送出其他簽到（疑似替人簽）」的判斷（以 IP 當裝置不成立，只會產生假訊號）；
    誰替誰簽以 tools/session_report.py 的旗標（same_device、proxy_for 等，來自應用程式自己的事件）為準。
  - 老師的機器＝「UA 與出口 IP 都相同、而且打過老師端路徑（/t、/t/…、/api/t/…）」的那一組請求，只剔除這一組；
    不再把整個出口 IP 剔除（老師在教室網路上，同一個出口後面的學生要留著算）。
  - 「QR 落地 410」只算真的回 410 過期頁的請求；已簽到的人回到舊的 QR 分頁，伺服器回 200 收據（land_revisit），
    不含在 410 裡。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
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


# 從 app.py 反推錯誤字串對照表

ERROR_CALL_NAMES = ("HTTPException", "Reject")  # 簽到路徑 9/26 起用 Reject(狀態碼, 訊息, 原因碼)


LEFT_VALUES = [str(n) for n in range(0, 11)]  # 「剩 {left} 次」的 left 代入 0 到 10


def _status_label_values(tree: ast.AST) -> list[str] | None:
    """用語法樹從 app.py 讀 STATUS_LABEL 的字典字面值（不 import app），讀不到就回 None。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) \
                and any(isinstance(t, ast.Name) and t.id == "STATUS_LABEL" for t in node.targets):
            vals = [v.value for v in node.value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            return vals if len(vals) == len(node.value.values) else None
    return None


def _expand_fstring(node: ast.JoinedStr, labels: list[str] | None) -> list[str] | None:
    """f-string 的運算式只有兩種時展開成多個字面值——名稱 left（0 到 10）、對 STATUS_LABEL 取下標（它的每個值）；
    有兩個可展開欄位就做笛卡兒積。其餘（別的運算式、格式化規格）回 None，呼叫端照舊寫進略過清單。"""
    pools: list[list[str]] = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            pools.append([str(part.value)])
        elif isinstance(part, ast.FormattedValue) and part.format_spec is None and part.conversion == -1:
            expr = part.value
            if isinstance(expr, ast.Name) and expr.id == "left":
                pools.append(LEFT_VALUES)
            elif isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) and expr.value.id == "STATUS_LABEL" and labels:
                pools.append(labels)
            else:
                return None
        else:
            return None
    return ["".join(c) for c in itertools.product(*pools)]


def _classify_node(status: int, node: ast.expr, lineno: int, out: list, note: str = "", labels: list[str] | None = None) -> None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append({"status": status, "kind": "literal", "text": node.value, "lineno": lineno, "note": note})
    elif isinstance(node, ast.JoinedStr):
        out.append({"status": status, "kind": "fstring", "raw": ast.unparse(node), "lineno": lineno, "note": note,
                    "variants": _expand_fstring(node, labels)})
    elif isinstance(node, ast.Name):
        out.append({"status": status, "kind": "variable", "raw": node.id, "lineno": lineno, "note": note})
    elif isinstance(node, ast.IfExp):
        _classify_node(status, node.body, lineno, out, note="條件運算式的 if 分支", labels=labels)
        _classify_node(status, node.orelse, lineno, out, note="條件運算式的 else 分支", labels=labels)
    else:
        out.append({"status": status, "kind": "other", "raw": ast.unparse(node), "lineno": lineno, "note": note})


def extract_http_exceptions(app_py_text: str) -> list:
    """用語法樹找 HTTPException(4xx/5xx, 訊息) 與 Reject(4xx/5xx, 訊息, 原因碼) 的呼叫。
    舊版逐行用正規表示式只認 HTTPException，簽到路徑改用 Reject 之後一條都抓不到；跨行的
    相鄰字串（"第一段" "第二段"）在語法樹裡已經併成一個字面值，不必再自己拼。"""
    out: list = []
    tree = ast.parse(app_py_text)
    labels = _status_label_values(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        if name not in ERROR_CALL_NAMES or len(node.args) < 2:
            continue
        code = node.args[0]
        if not (isinstance(code, ast.Constant) and isinstance(code.value, int)) or code.value < 400:
            continue
        _classify_node(code.value, node.args[1], node.lineno, out, labels=labels)
    out.sort(key=lambda e: e["lineno"])
    return out


def build_size_table(exceptions: list):
    table: dict = {}  # (status, size) -> [text, ...]
    notes: list = []
    for e in exceptions:
        if e["kind"] == "literal":
            payload = json.dumps({"ok": False, "error": e["text"]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            table.setdefault((e["status"], len(payload)), []).append(e["text"])
        elif e["kind"] == "fstring" and e.get("variants"):  # left／STATUS_LABEL 展開後每一種組合都是固定字串
            for text in e["variants"]:
                payload = json.dumps({"ok": False, "error": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                table.setdefault((e["status"], len(payload)), []).append(text)
        elif e["kind"] == "fstring":
            extra = f"（{e['note']}）" if e.get("note") else ""
            notes.append(f"app.py:{e['lineno']} status={e['status']} 略過（f-string，內容依請求而變）{extra}：{e['raw']}")
        elif e["kind"] == "variable":
            notes.append(f"app.py:{e['lineno']} status={e['status']} 略過（引用變數 {e['raw']}，多半是別處組好的 f-string）")
        else:
            extra = f"（{e['note']}）" if e.get("note") else ""
            notes.append(f"app.py:{e['lineno']} status={e['status']} 略過（無法靜態解析）{extra}：{e['raw']}")
    return table, notes


# 主流程


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

    # 「裝置」欄是出口 IP 的雜湊，整個校園網路（NAT）後面的多支手機會併成同一個
    # 不再有「疑似替人簽」的判斷，提示也不再提它
    print("注意：「裝置」欄是出口 IP 的雜湊。同一個校園網路（NAT）後面的多支手機會併成同一個，"
          "被擋的人可能被併掉；誰被擋、誰替誰簽以 tools/session_report.py 為準。")
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
                "ua": ua,
                "ua_cat": classify_ua(ua),
                "method": req.get("method", ""),
                "uri_masked": mask_uri(path),
                "status": entry.get("status"),
                "size": entry.get("size"),
                "excluded": should_exclude(path, ua),
            }
        )
    rows.sort(key=lambda r: r["dt"])

    # 1. 匿名化時間線
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

    # 2. 漏斗彙總
    print("=== 漏斗彙總 ===")
    # 打過老師端路徑的機器就是老師的機器（老師自己開 /c 看畫面不算學生）；用 rows 不用 kept，輪詢也算數。
    # 機器＝（出口 IP、UA）這一組，不是整個出口 IP——老師與學生常在同一個校園出口後面，整個 IP 剔除會把同教室的學生一起丟掉
    teacher_machines = {(r["ip"], r["ua"]) for r in rows if r["uri_masked"] == "/t" or r["uri_masked"].startswith(("/t/", "/api/t/"))}
    by_dev = defaultdict(list)
    for r in kept:
        if STUDENT_MASKED_RE.match(r["uri_masked"]) and (r["ip"], r["ua"]) not in teacher_machines:
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

    # 只有送出六位數（POST /c）才算改走 /c；打開首頁會被轉到 /c，那只是路過
    c_devices = {r["dev"] for r in kept if r["uri_masked"] == "/c" and r["method"] == "POST" and (r["ip"], r["ua"]) not in teacher_machines}
    print(f"改走 /c（送出六位數）的裝置數：{len(c_devices)}")

    checkin = [r for r in kept if r["method"] == "POST" and r["uri_masked"] == "/api/checkin"]
    checkin_status = Counter(r["status"] for r in checkin)
    print("/api/checkin 各狀態碼數：" + ("、".join(f"{k}:{v}" for k, v in sorted(checkin_status.items(), key=lambda kv: str(kv[0]))) or "（無）"))

    deltas: list = []
    never_checkin: list = []
    rejected_only: list = []  # 送出過、但一次都沒成功（全被擋）——跟「落地後沒送出」是兩回事
    for dev in devices:
        entries = sorted(by_dev[dev], key=lambda r: r["dt"])
        first_landing = entries[0]["dt"]
        success = [r for r in entries if r["uri_masked"] == "/api/checkin" and r["status"] == 200]
        if success:
            first_success = min(r["dt"] for r in success)
            deltas.append((first_success - first_landing).total_seconds())
        else:
            # 「落地成功」＝GET /s/…/SIG 或 POST /c 拿到 200（不是失敗的 410/400）
            landed_ok = any(((r["uri_masked"] == "/c" and r["method"] == "POST") or LAND_MASKED_RE.match(r["uri_masked"]))
                            and r["status"] == 200 for r in entries)
            posts = [r for r in entries if r["uri_masked"] == "/api/checkin" and r["method"] == "POST"]
            if posts:
                rejected_only.append((dev, Counter(r["status"] for r in posts)))
            elif landed_ok:
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
    print(f"送出簽到但從未成功的裝置：{len(rejected_only)} 支" + (
        "（" + "、".join(f"{d} " + " ".join(f"{s}×{n}" for s, n in sorted(c.items(), key=lambda kv: str(kv[0])))
                        for d, c in rejected_only) + "）" if rejected_only else ""))
    print()

    # 3. 用回應大小反推錯誤原因
    print("=== 用回應大小反推錯誤原因 ===")
    app_path = Path(args.app)
    if not app_path.is_file():
        print(f"找不到 {app_path}，略過對照表")
    else:
        exceptions = extract_http_exceptions(app_path.read_text(encoding="utf-8"))
        table, notes = build_size_table(exceptions)
        n_literal = sum(1 for e in exceptions if e["kind"] == "literal")
        n_expanded = sum(1 for e in exceptions if e["kind"] == "fstring" and e.get("variants"))
        print(f"從 {app_path.name} 抓到 {len(exceptions)} 條 4xx/5xx HTTPException／Reject，其中 {n_literal} 條是可算 byte 數的固定字串、"
              f"{n_expanded} 條 f-string 已展開成固定字串：")
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

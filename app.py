"""課堂點名系統

輪換 QR（HMAC 時間片）＋裝置綁定＋個人 PIN＋座位號抽點＋IP 比對。
單檔 FastAPI，SQLite 存放；老師端單一密碼登入。
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mimetypes
import threading
import urllib.request
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, HTTPException, Path as PathParam, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
DB_PATH = os.environ.get("ATTEND_DB") or ("/data/attend.db" if Path("/data").is_dir() else str(BASE / "data" / "attend.db"))
SECRET = os.environ.get("ATTEND_SECRET") or ""
TEACHER_PW = os.environ.get("ATTEND_TEACHER_PASSWORD") or ""
BASE_URL = os.environ.get("ATTEND_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
if not SECRET or not TEACHER_PW:
    raise SystemExit("需要環境變數 ATTEND_SECRET 與 ATTEND_TEACHER_PASSWORD")

# C-S7：裝置 cookie 改用 __Host- 前綴（瀏覽器規則：__Host- 必須同時 Secure、無 Domain、path=/，
# 擋掉子網域寫同名 cookie 蓋過本站的招式）。本機與測試走 http，瀏覽器不收沒有 Secure 的
# __Host- cookie，所以 http 下維持舊名，不然本機測試整套會拿不到 cookie。
DK_COOKIE = "__Host-attend_dk" if BASE_URL.startswith("https") else "attend_dk"
LEGACY_DK_COOKIE = "attend_dk"  # 只有雜湊已綁在某個學生身上的舊名才採用，未綁的一律當沒有 cookie

TZ = timezone(timedelta(hours=8))
DEFAULT_SETTINGS = {
    "open_minutes": 3,      # 開放簽到（出席）
    "late_minutes": 15,     # 遲到上限（自開始起算）
    "qr_interval": 10,      # QR 輪換秒數
    "qr_grace": 30,         # 過期容許秒數（B015/B007/B040：2026-09-21 裁定 10／30）
    "spot_n": 3,            # 抽點人數
    "ip_check": True,       # 比對學生與投影頁公網 IP（只標記）
    "grant_seconds": 180,   # 掃碼後填表時限
    "pin_max_fail": 3,
    "allow_bind": True,     # 允許首次綁定（第二週後可關，新綁定要老師打開）
    "discord_channel": "",  # 結束點名時把缺席與遲到名單發到這個頻道（空＝不發）
    "bind_minutes": 10,     # B030：綁定場次的開放分鐘數（無遲到區，open_until=late_until）
}
STATUS_LABEL = {
    "present": "出席", "late": "遲到", "pending": "待確認",
    "absent": "缺席", "excused": "請假",
}
NA = "na"  # 報表用：該場次時學生還沒加選
PIN_MAX_FAIL_TOTAL = 20  # 同一場同一學號不分裝置的錯誤上限（擋換裝置識別重試）
FLAG_LABEL = {
    "same_device": "同裝置多人", "device_bound_other": "裝置已綁他人",
    "ip_differs": "IP 與教室不同", "pin_lock": "PIN 錯多次",
    "manual": "老師手動", "spot_absent": "抽點不在", "spot_ok": "抽點在場",
    "late_bind": "首次綁定", "same_fp": "疑似同手機", "proxy_for": "代簽連坐", "was_late": "",
    "bind_burst": "同來源多筆首綁", "qr_after_manual": "老師改後才掃碼",
    "name_variant": "姓名寫法不同",  # A4/B018：字數相同且第一個字相同，放行但標旗給老師看
}
SESSION_KIND_LABEL = {"normal": "", "bind": "綁定場次"}  # B030
KIND_DISPLAY = {"normal": "一般點名", "bind": "綁定場次"}  # C-T4：老師頁錯誤訊息用的完整說法（含「一般」二字）
# B030：計入出缺勤的場次＝一般場次（非綁定）且未作廢；報表／CSV／`/api/me` 出席率／attendance_rate／
# 老師首頁統計都要走這個共用定義，不要各處各寫一份判準。
COUNTED_SESSION_SQL = "kind='normal' AND voided_at IS NULL"
REJECT_REASON_LABEL = {  # B004/B026：老師頁即時漏斗與「卡住名單」用的中文說明
    "pin_format": "PIN 格式", "no_cookie": "沒有裝置識別", "throttle": "太頻繁",
    "grant_expired": "填表逾時", "grant_other_device": "別支手機的表單", "closed": "點名已結束",
    "not_match": "學號或姓名不符", "pin_locked": "PIN 鎖定", "bind_closed": "不開放首次綁定",
    "bind_revoked": "判不在後又想綁",
    "pin_mismatch2": "兩次 PIN 不同", "pin_wrong": "PIN 錯誤", "pin_exists": "已設過 PIN",
    "bound_other_device": "已綁別支手機",
}
DISCORD_AUTO_MIN_RATE = 0.5  # BUGLOG B027：自動結案時出席+遲到+待確認不到應到人數一半就不自動發 Discord

mimetypes.add_type("font/woff2", ".woff2")  # slim 映像的 mimetypes 沒有 woff2
app = FastAPI(title="attend", docs_url=None, redoc_url=None,
              openapi_url="/openapi.json" if os.environ.get("ATTEND_OPENAPI") else None)  # 只有測試時開，給 schemathesis 用
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# B062：字型網址帶內容雜湊。靜態檔快取七天（Cloudflare 邊緣與瀏覽器），重做子集後網址不變的話，新字要等七天才看得到
_FONT = BASE / "static" / "fonts" / "ZhuqueFangsong-subset.woff2"
FONT_V = hashlib.sha256(_FONT.read_bytes()).hexdigest()[:10] if _FONT.is_file() else "0"
templates.env.globals.update(STATUS_LABEL=STATUS_LABEL, FLAG_LABEL=FLAG_LABEL, REJECT_REASON_LABEL=REJECT_REASON_LABEL,
                             pct=lambda r: pct(r), BASE_HOST=BASE_URL.split("://", 1)[-1], FONT_V=FONT_V)  # noqa: PLW0108 pct 定義在下面，這裡直接傳函式名會撞 forward reference NameError，lambda 是刻意的延遲查找


# ───────────────────────── 工具 ─────────────────────────

def now() -> float:
    return time.time()


def fmt(ts: float | None, with_date: bool = False) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts, TZ)
    return d.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S")


templates.env.filters["fmt"] = fmt


def _subkey(purpose: str) -> bytes:
    """每個用途各自一把子鑰，token／裝置雜湊／指紋／時間片互不能當彼此的預言機。"""
    return hmac.new(SECRET.encode(), b"attend:" + purpose.encode(), hashlib.sha256).digest()


K_TOKEN, K_DEVICE, K_FP, K_SLOT = _subkey("token"), _subkey("device"), _subkey("fingerprint"), _subkey("slot")


def sign(msg: str) -> str:
    return hmac.new(K_TOKEN, msg.encode(), hashlib.sha256).hexdigest()[:24]


def make_token(*parts: str, exp: float) -> str:
    body = ":".join(list(parts) + [str(int(exp))])
    return f"{body}:{sign(body)}"


def read_token(token: str, nparts: int) -> list[str] | None:
    try:
        *parts, exp, sig = token.split(":")
    except ValueError:
        return None
    if len(parts) != nparts:
        return None
    body = ":".join(parts + [exp])
    if not hmac.compare_digest(sign(body), sig):
        return None
    if now() > int(exp):
        return None
    return parts


def hash_device(key: str) -> str:
    return hmac.new(K_DEVICE, key.strip().encode(), hashlib.sha256).hexdigest()


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 600_000).hex()


def _valid_ip(v: str) -> str:
    try:
        return str(ipaddress.ip_address(v.strip()))
    except ValueError:
        return ""


def client_ip(req: Request) -> str:
    """優先信 Cloudflare 的 CF-Connecting-IP（經 CF 一定被覆寫，客戶端偽造不了）；
    再退到 X-Forwarded-For 最後一段（Caddy 附加的那段）；一律驗 IP 格式，避免髒字串進資料庫與老師頁。"""
    cf = req.headers.get("cf-connecting-ip")  # 不信 X-Real-IP：Caddy 的 {client_ip} 在此設定下可被 XFF 第一段偽造（2026-09-10 實測）
    if cf and _valid_ip(cf):
        return _valid_ip(cf)
    xff = req.headers.get("x-forwarded-for", "")
    for part in reversed(xff.split(",")):
        if _valid_ip(part):
            return _valid_ip(part)
    return (_valid_ip(req.client.host) if req.client else "") or ""


def fingerprint(req: Request, hints: str = "") -> str:
    """伺服器端指紋：只用伺服器看得到的 IP＋UA＋語言（前端自報的 hints 可被任意變造，不納入偵測與限流）。"""
    raw = "|".join([client_ip(req) or "noip", req.headers.get("user-agent", ""), req.headers.get("accept-language", "")])
    return hmac.new(K_FP, raw.encode(), hashlib.sha256).hexdigest()[:32]


def classify_ua(ua: str) -> str:
    """User-Agent 只留裝置類別給事件記錄（B004/B026），不存完整字串、不存 IP、不存姓名。"""
    u = ua or ""
    if "Line/" in u or "Line;" in u:
        return "LINE"
    if "FBAN" in u or "FBAV" in u or "Instagram" in u:
        return "FB-IG"
    if "iPhone" in u or "iPad" in u or "iPod" in u:
        if "CriOS" in u:
            return "iOS-Chrome"
        if "Safari" in u and "Version/" in u:
            return "iOS-Safari"
        return "iOS-INAPP"
    if "Android" in u:
        if "SamsungBrowser" in u:
            return "Android-Samsung"
        if "; wv" in u or "Chrome/" not in u:
            return "Android-WebView"
        return "Android-Chrome"
    if "Macintosh" in u:
        return "Mac"
    if "Windows" in u:
        return "Win"
    return "other"


_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\ufeff")  # 零寬空白／不斷連字／連字／BOM：手機 IME 常夾帶


def norm_name(s: str) -> str:
    """姓名比對前的正規化（B018）：NFKC 收斂全形英數與相容漢字變體，再去零寬字元與空白。
    刻意不做異體字對照表（温/溫、峰/峯）——那張表要哪些字、誰維護，屬於待老師裁定的題目，不在這裡決定。"""
    s = unicodedata.normalize("NFKC", s)
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, "")
    return "".join(s.split())


def name_matches(given: str, roster: str) -> str:
    """A4/B018 姓名寬鬆比對：norm_name 後完全相同→'exact'；不同但字數相同且第一個字相同→'variant'
    （涵蓋温/溫這類異體字打法，也涵蓋同姓同字數的不同名字——這是刻意放寬的已知代價，不在這裡收斂）；
    其餘→''（不符，照舊拒絕）。名冊上的姓名本身不改。"""
    g, r = norm_name(given), norm_name(roster)
    if g == r:
        return "exact"
    if g and r and len(g) == len(r) and g[0] == r[0]:
        return "variant"
    return ""


_FULLWIDTH_ASCII = str.maketrans(
    "".join(chr(0xFF01 + i) for i in range(94)) + "　",
    "".join(chr(0x21 + i) for i in range(94)) + " ",
)


def to_halfwidth(s: str) -> str:
    """全形英數符號（含全形空白）轉半形，不動中文字（B019）。學號前後端要用同一套規則，
    否則前端濾掉、後端沒轉，兩邊看到的「合法字元」不一樣。"""
    return s.translate(_FULLWIDTH_ASCII)


# ───────────────────────── 資料庫 ─────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  settings TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL, student_id TEXT NOT NULL,
  name TEXT NOT NULL, cls TEXT DEFAULT '', pin_hash TEXT, pin_salt TEXT,
  device_hash TEXT, bound_at REAL,
  UNIQUE(course_id, student_id));
CREATE INDEX IF NOT EXISTS idx_students_device ON students(course_id, device_hash);
CREATE INDEX IF NOT EXISTS idx_students_device_only ON students(device_hash);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL, secret TEXT NOT NULL,
  opened_at REAL NOT NULL, open_until REAL NOT NULL, late_until REAL NOT NULL,
  closed_at REAL, teacher_ip TEXT, note TEXT DEFAULT '');
-- B030/B028：kind（normal/bind）與 voided_at 一律靠下面的 ALTER 遷移補上（沿用 fp／created_at 的既有寫法），
-- 這裡不直接寫進 CREATE TABLE——連全新資料庫也要走一次遷移路徑，遷移程式碼才有測試會實際執行到。
CREATE TABLE IF NOT EXISTS checkins(
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, student_pk INTEGER NOT NULL,
  ts REAL NOT NULL, seat TEXT DEFAULT '', status TEXT NOT NULL,
  flags TEXT NOT NULL DEFAULT '[]', ip TEXT DEFAULT '', device_hash TEXT DEFAULT '',
  source TEXT DEFAULT 'qr', note TEXT DEFAULT '',
  UNIQUE(session_id, student_pk));
CREATE TABLE IF NOT EXISTS pin_fail(
  session_id INTEGER NOT NULL, student_pk INTEGER NOT NULL, device_hash TEXT NOT NULL DEFAULT '',
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(session_id, student_pk, device_hash));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants(
  sig TEXT PRIMARY KEY, device_hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS spotchecks(
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, student_pk INTEGER NOT NULL,
  seat TEXT, result TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, session_id INTEGER, kind TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  device8 TEXT NOT NULL DEFAULT '', ua_class TEXT NOT NULL DEFAULT '', student_pk INTEGER);
CREATE INDEX IF NOT EXISTS idx_events_session_kind ON events(session_id, kind, ts);
CREATE INDEX IF NOT EXISTS idx_events_device ON events(session_id, device8, kind, ts);
CREATE INDEX IF NOT EXISTS idx_events_student ON events(session_id, student_pk, kind, ts);
"""


Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def log_event(session_id: int | None, kind: str, reason: str = "", device8: str = "",
              ua_class: str = "", student_pk: int | None = None) -> None:
    """事件記錄（B004/B026）：獨立連線、獨立 commit。
    🔴 只能在呼叫端目前沒有握著另一條還沒 commit 的寫入交易時呼叫——SQLite 一次只容許一個寫入者，
    另開連線硬寫會跟還沒 commit 的那條互鎖，等到 busy timeout 才炸「database is locked」
    （2026-09-21 實測：api_checkin 在 with db() as con 內、con 還沒 commit 前呼叫這支，
    導致簽到請求變慢甚至等到逾時；已改成那幾個路徑呼叫 log_event_on(con, ...) 共用同一條連線）。
    寫入失敗不得影響主流程，只印到 stderr（不拋例外）。"""
    try:
        con = connect()
        try:
            con.execute(
                "INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                (now(), session_id, kind, reason, device8, ua_class, student_pk))
            con.commit()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 事件記錄是旁路，任何失敗都不能拖累簽到／登入等主流程
        print(f"[events] log_event 失敗（kind={kind}）：{e}", file=sys.stderr)


def log_event_on(con: sqlite3.Connection, session_id: int | None, kind: str, reason: str = "",
                 device8: str = "", ua_class: str = "", student_pk: int | None = None) -> None:
    """事件記錄，跟呼叫端目前握著的連線共用同一次交易、同一次 commit——用在呼叫端還沒 commit
    就要記錄成功事件的路徑（例如 api_checkin 的早退回應），避免跟 log_event() 的獨立連線互鎖。
    跟主流程同進退：主流程 commit 才落地，主流程萬一之後還是失敗回滾，這筆事件也跟著滾掉（可接受，
    因為這條路徑本來就是「主流程本身要不要成功」的一部分，不是旁路）。寫入失敗不得拋出，只印 stderr。"""
    try:
        con.execute(
            "INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
            (now(), session_id, kind, reason, device8, ua_class, student_pk))
    except Exception as e:  # noqa: BLE001 事件記錄是旁路，任何失敗都不能拖累簽到主流程
        print(f"[events] log_event_on 失敗（kind={kind}）：{e}", file=sys.stderr)


with db() as _c:
    _c.executescript(SCHEMA)
    if "fp" not in [r["name"] for r in _c.execute("PRAGMA table_info(checkins)")]:
        _c.execute("ALTER TABLE checkins ADD COLUMN fp TEXT DEFAULT ''")
    if "device_hash" not in [r["name"] for r in _c.execute("PRAGMA table_info(pin_fail)")]:
        _c.execute("DROP TABLE pin_fail")  # 舊格式沒有裝置欄，重建（只是暫存的失敗次數）
        _c.execute("CREATE TABLE pin_fail(session_id INTEGER NOT NULL, student_pk INTEGER NOT NULL, "
                   "device_hash TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id, student_pk, device_hash))")
    if "created_at" not in [r["name"] for r in _c.execute("PRAGMA table_info(students)")]:
        # 既有名冊視為學期初就在（0），之後加選的人才有時間，早於加入的場次不算他缺席
        _c.execute("ALTER TABLE students ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
    if "kind" not in [r["name"] for r in _c.execute("PRAGMA table_info(sessions)")]:
        # B030：既有場次一律視為一般場次（正式站已有的資料照常計入出缺勤）
        _c.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'")
    if "voided_at" not in [r["name"] for r in _c.execute("PRAGMA table_info(sessions)")]:
        _c.execute("ALTER TABLE sessions ADD COLUMN voided_at REAL")  # B028：NULL＝有效，非 NULL＝已作廢


def course_settings(row: sqlite3.Row) -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(row["settings"] or "{}"))
    except json.JSONDecodeError:
        pass
    # 不管設定從哪來（表單、manage.py、舊資料），讀出來一律合法；壞值退回預設，不讓整個老師端 500
    for k in ("open_minutes", "late_minutes", "qr_interval", "qr_grace", "spot_n", "grant_seconds", "pin_max_fail", "bind_minutes"):
        try:
            s[k] = int(s[k])
        except (TypeError, ValueError):
            s[k] = DEFAULT_SETTINGS[k]
    for k in ("ip_check", "allow_bind"):
        s[k] = bool(s[k]) if isinstance(s[k], (bool, int)) else str(s[k]).lower() in ("1", "true", "yes")
    if not isinstance(s["discord_channel"], str) or not s["discord_channel"].isdigit():
        s["discord_channel"] = ""
    s["open_minutes"] = max(1, s["open_minutes"])
    s["late_minutes"] = max(s["open_minutes"], s["late_minutes"])
    s["qr_interval"] = max(5, s["qr_interval"])
    s["qr_grace"] = max(0, min(s["qr_grace"], 120))  # B015：不再夾在 qr_interval 以內，上限放到 120 秒
    s["spot_n"] = max(1, s["spot_n"])
    s["grant_seconds"] = max(30, min(s["grant_seconds"], 900))  # C-S8：grants 表一小時清除，憑證不該活得比它久
    s["pin_max_fail"] = max(1, s["pin_max_fail"])
    s["bind_minutes"] = max(3, min(s["bind_minutes"], 30))  # B030：3–30 分鐘
    return s


def get_course(con, cid: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到課程")
    return row


def get_session(con, sid: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到這次點名")
    return row


def session_phase(s: sqlite3.Row) -> str:
    """open（可出席）/ late（只能遲到）/ closed"""
    t = now()
    if s["closed_at"] or t > s["late_until"]:
        return "closed"
    if t <= s["open_until"]:
        return "open"
    return "late"


def counts_toward_attendance(s: sqlite3.Row) -> bool:
    """B030 共用定義：計入出缺勤的場次＝一般場次（非綁定）且未作廢；已結案與否由呼叫端另外判斷。
    報表、CSV、`/api/me` 出席率、attendance_rate、老師首頁統計都要走這支，不要各處各寫一份判準。"""
    return s["kind"] == "normal" and s["voided_at"] is None


def close_session_rows(con, s: sqlite3.Row, closed_at: float) -> None:
    """結案共用：寫 closed_at、沒簽的記缺席（加選生不算）、清掉本場 PIN 失敗計數。
    B030：綁定場次不寫自動缺席列（不計出缺勤，也就無所謂「沒簽的人缺席」）。"""
    con.execute("UPDATE sessions SET closed_at=? WHERE id=?", (closed_at, s["id"]))
    if s["kind"] == "normal":
        con.execute(
            "INSERT OR IGNORE INTO checkins(session_id, student_pk, ts, status, flags, source) "
            "SELECT ?, id, ?, 'absent', '[]', 'auto' FROM students WHERE course_id=? AND created_at<=?",
            (s["id"], closed_at, s["course_id"], s["opened_at"]))
    con.execute("DELETE FROM pin_fail WHERE session_id=?", (s["id"],))


def attendance_rate(con, s: sqlite3.Row) -> float | None:
    """出席＋遲到＋待確認 ÷（應到人數－請假）；應到排除加選前的 NA（BUGLOG B027）。
    B030：不計入出缺勤的場次（綁定／已作廢）沒有出席率可言，一律回 None。"""
    if not counts_toward_attendance(s):
        return None
    total = con.execute("SELECT COUNT(*) FROM students WHERE course_id=? AND created_at<=?",
                        (s["course_id"], s["opened_at"])).fetchone()[0]
    counts = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM checkins WHERE session_id=? GROUP BY status", (s["id"],)).fetchall()}
    ok = sum(counts.get(k, 0) for k in ("present", "late", "pending"))
    denom = total - counts.get("excused", 0)
    return ok / denom if denom > 0 else None


def finalize_expired(con) -> list[int]:
    """老師忘了按結束的場次：遲到窗過了就自動結案並把沒簽的記缺席，報表與自查才看得到；
    有設 Discord 且成功率過低（BUGLOG B027：出席+遲到+待確認不到應到人數一半）就不自動發，
    改在 sessions.note 記 discord_held，老師頁提示改用「重發通知」手動確認發送。"""
    t = now()
    done = []
    pending_posts = []
    for s in con.execute("SELECT * FROM sessions WHERE closed_at IS NULL AND late_until<?", (t,)).fetchall():
        won = con.execute("UPDATE sessions SET closed_at=? WHERE id=? AND closed_at IS NULL", (s["late_until"], s["id"])).rowcount
        if not won:  # 另一個輪詢先結了，通知交給它
            continue
        close_session_rows(con, s, s["late_until"])
        done.append(s["id"])
        if s["kind"] != "normal":  # B030：綁定場次不計出缺勤，結案不發 Discord、也不記 discord_held
            continue
        channel, text = session_summary(con, s["id"])
        rate = attendance_rate(con, s)
        if channel and rate is not None and rate < DISCORD_AUTO_MIN_RATE:
            con.execute("UPDATE sessions SET note='discord_held' WHERE id=?", (s["id"],))
        else:
            pending_posts.append((channel, text))
    if done:
        con.commit()  # 先落地再發：呼叫端之後就算 404 回滾，也不會出現「訊息發了、資料沒結案」
        for channel, text in pending_posts:
            if channel:
                threading.Thread(target=lambda ch=channel, tx=text: discord_post_long(ch, tx), daemon=True).start()
    elif con.in_transaction:
        # B059：全部搶輸（另一個輪詢先結案）時，rowcount=0 的 UPDATE 仍讓 sqlite3 開了隱式交易；
        # 不收掉的話，do_open_session 緊接的 BEGIN IMMEDIATE 會炸「cannot start a transaction within a transaction」
        con.commit()
    return done


# ───────────────────────── 輪換碼 ─────────────────────────

def slot_sig(session: sqlite3.Row, slot: int) -> str:
    return hmac.new(K_SLOT, f"{session['id']}:{session['secret']}:{slot}".encode(),
                    hashlib.sha256).hexdigest()[:10]


def code6(sig: str) -> str:
    return f"{int(sig, 16) % 1_000_000:06d}"


def current_slots(session: sqlite3.Row, settings: dict) -> list[int]:
    """回傳目前可接受的時間片：這一片，加上「該片結束後 grace 秒內」仍算數的前面幾片（B015）。
    grace 可以大於 interval——越舊的片子驗證條件越嚴，一旦某片不再有效，更舊的片子必然也不有效，
    故一遇到不符合就可以停止往前找。"""
    iv, grace = settings["qr_interval"], settings["qr_grace"]
    t = now()
    cur = int(t // iv)
    slots = [cur]
    j = 1
    while True:
        prev = cur - j
        if prev < 0:
            break
        slot_end = (prev + 1) * iv
        if t - slot_end < grace:
            slots.append(prev)
            j += 1
        else:
            break
    return slots


def qr_svg(url: str) -> str:
    # BUGLOG B032：靜區至少 4 模組（ISO/IEC 18004），border=1 在 1024×768 投影時解碼器可能找不到碼
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=20, border=4)
    return img.to_string(encoding="unicode")


# ───────────────────────── 老師登入 ─────────────────────────

_login_fail: dict[str, list[float]] = {}


def auth_epoch(con=None) -> str:
    q = "SELECT value FROM meta WHERE key='auth_epoch'"
    if con is not None:
        r = con.execute(q).fetchone()
    else:
        with db() as c:
            r = c.execute(q).fetchone()
    return r["value"] if r else "0"


def bump_auth_epoch() -> None:
    with db() as con:
        con.execute("INSERT INTO meta(key, value) VALUES('auth_epoch', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(int(auth_epoch(con)) + 1),))


def teacher_ok(req: Request) -> bool:
    tok = req.cookies.get("ta")
    parts = read_token(tok, 2) if tok else None
    return bool(parts and parts[0] == "teacher" and parts[1] == auth_epoch())


def require_teacher(req: Request):
    if not teacher_ok(req):
        raise HTTPException(401, "請先登入")


def teacher_redirect(req: Request):
    if not teacher_ok(req):
        return RedirectResponse(f"/t/login?next={req.url.path}", 303)
    return None


@app.get("/t/login", response_class=HTMLResponse)
def login_page(req: Request, next: str = "/t"):
    return templates.TemplateResponse(req, "login.html", {"next": next, "error": ""})


@app.post("/t/login")
def login(req: Request, password: str = Form(..., max_length=200), next: str = Form("/t", max_length=200)):
    ip = client_ip(req) or "noip"
    for k in [k for k, v in _login_fail.items() if all(now() - t >= 600 for t in v)]:
        _login_fail.pop(k, None)
    fails = [t for t in _login_fail.get(ip, []) if now() - t < 600]
    ok = hmac.compare_digest(password.encode(), TEACHER_PW.encode())
    if not ok:
        fails.append(now()); _login_fail[ip] = fails
        if len(fails) >= 8:  # 同一出口錯太多次：每次拖慢但仍以密碼為準，學生猜密碼不會把老師鎖在外面
            time.sleep(2)
            raise HTTPException(429, "嘗試太多次，10 分鐘後再試")
        return templates.TemplateResponse(req, "login.html", {"next": next, "error": "密碼錯誤"}, status_code=401)
    _login_fail.pop(ip, None)
    safe_next = next if (next.startswith("/") and not next.startswith("//") and "\\" not in next) else "/t"
    resp = RedirectResponse(safe_next, 303)
    resp.set_cookie("ta", make_token("teacher", auth_epoch(), exp=now() + 30 * 86400),
                    httponly=True, samesite="lax", secure=BASE_URL.startswith("https"), max_age=30 * 86400)
    return resp


@app.get("/t/logout")
def logout():
    bump_auth_epoch()  # 撤銷所有裝置上的老師 cookie（登出＝全部登出），不必換 SECRET
    resp = RedirectResponse("/t/login", 303)
    resp.delete_cookie("ta")
    return resp


# ───────────────────────── 老師頁面 ─────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/c", 303)


@app.get("/t", response_class=HTMLResponse)
def teacher_home(req: Request):
    if r := teacher_redirect(req):
        return r
    with db() as con:
        finalize_expired(con)
        courses = []
        for c in con.execute("SELECT * FROM courses ORDER BY id").fetchall():
            n = con.execute("SELECT COUNT(*) FROM students WHERE course_id=?", (c["id"],)).fetchone()[0]
            live = con.execute(
                "SELECT * FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>? ORDER BY id DESC LIMIT 1",
                (c["id"], now())).fetchone()
            recent = con.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=s.id AND k.status IN ('present','late')) AS n_ok "
                "FROM sessions s WHERE course_id=? ORDER BY id DESC LIMIT 6", (c["id"],)).fetchall()
            courses.append({"row": c, "n": n, "live": live, "recent": recent, "settings": course_settings(c)})
    return templates.TemplateResponse(req, "home.html", {"courses": courses})


def do_open_session(con: sqlite3.Connection, cid: int, kind: str) -> int:
    """開一場點名（或找到已經開著的那場，同課同時只會有一場 live）。
    B030：kind='bind' 是綁定場次，open_until=late_until=opened_at+bind_minutes*60（沒有遲到區）。
    C-T3：finalize_expired 會自己中途 commit，必須在 BEGIN IMMEDIATE 之前跑完，否則交易鎖在它
    commit 的當下就被放掉，「查 live → INSERT」之間會有一段沒鎖住的視窗，兩個執行緒能同時通過
    live 檢查各自開出一場（2026-09-21 bug-hunter r5/r5b 重現）。
    C-T4：若已有一場未結案場次但種類不同，不沿用也不新建，回 409 讓老師先結束那一場。"""
    finalize_expired(con)  # 自己 commit，不能包進下面那段交易鎖裡
    con.execute("BEGIN IMMEDIATE")  # 之後「查 live → INSERT」全程在同一個交易鎖裡，杜絕上述競態
    c = get_course(con, cid)
    st = course_settings(c)
    live = con.execute("SELECT id, kind FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>?",
                       (cid, now())).fetchone()
    if live:
        if live["kind"] != kind:
            raise HTTPException(409, f"這門課還有一場〈{KIND_DISPLAY.get(live['kind'], live['kind'])}〉在進行，先結束它")
        return live["id"]
    t = now()
    if kind == "bind":
        open_until = late_until = t + st["bind_minutes"] * 60
    else:
        open_until, late_until = t + st["open_minutes"] * 60, t + st["late_minutes"] * 60
    cur = con.execute(
        "INSERT INTO sessions(course_id, secret, opened_at, open_until, late_until, kind) VALUES(?,?,?,?,?,?)",
        (cid, secrets.token_hex(16), t, open_until, late_until, kind))
    assert cur.lastrowid is not None
    return cur.lastrowid


@app.post("/t/course/{cid}/open")
def open_session(req: Request, cid: int = PathParam(ge=1, le=2**53), kind: str = Form("normal")):
    require_teacher(req)
    if kind not in ("normal", "bind"):
        raise HTTPException(400, "場次種類不合法")
    with db() as con:
        sid = do_open_session(con, cid, kind)
    return RedirectResponse(f"/t/session/{sid}", 303)


@app.get("/t/session/{sid}", response_class=HTMLResponse)
def session_page(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    if r := teacher_redirect(req):
        return r
    with db() as con:
        s = get_session(con, sid)
        c = get_course(con, s["course_id"])
        st = course_settings(c)
    return templates.TemplateResponse(req, "session.html", {"s": s, "c": c, "st": st})


@app.get("/t/projector/{sid}", response_class=HTMLResponse)
def projector_page(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    if r := teacher_redirect(req):
        return r
    with db() as con:
        s = get_session(con, sid)
        c = get_course(con, s["course_id"])
        pass  # 教室 IP 由投影頁 JS 用 POST 回報（GET 不改狀態，避免手機 4G 開一下就蓋掉）
    return templates.TemplateResponse(req, "projector.html", {"s": s, "c": c})


@app.get("/t/course/{cid}/students", response_class=HTMLResponse)
def students_page(req: Request, cid: int = PathParam(ge=1, le=2**53)):
    if r := teacher_redirect(req):
        return r
    with db() as con:
        c = get_course(con, cid)
        rows = con.execute("SELECT * FROM students WHERE course_id=? ORDER BY student_id", (cid,)).fetchall()
    return templates.TemplateResponse(req, "students.html", {"c": c, "rows": rows, "st": course_settings(c)})


@app.post("/t/course/{cid}/settings")
def save_settings(req: Request, cid: int = PathParam(ge=1, le=2**53), open_minutes: int = Form(..., ge=0, le=600), late_minutes: int = Form(..., ge=0, le=600),
                  qr_interval: int = Form(..., ge=0, le=600), qr_grace: int = Form(..., ge=0, le=600), spot_n: int = Form(..., ge=0, le=200),
                  bind_minutes: int = Form(10, ge=1, le=999), ip_check: str = Form(""), discord_channel: str = Form("")):
    require_teacher(req)
    discord_channel = discord_channel.strip()
    if discord_channel and not (discord_channel.isascii() and discord_channel.isdigit() and 15 <= len(discord_channel) <= 22):
        raise HTTPException(400, "Discord 頻道 ID 要是 17 到 20 位數字（在頻道上按右鍵複製 ID），留空代表不發")
    with db() as con:
        st = course_settings(get_course(con, cid))  # 合併，不覆寫表單沒有的設定（allow_bind、grant_seconds…）
        om = max(1, open_minutes); iv = max(5, qr_interval)
        st.update({"open_minutes": om, "late_minutes": max(om, late_minutes),
                   "qr_interval": iv, "qr_grace": max(0, min(qr_grace, 120)),  # B015：上限 120 秒，不再綁 qr_interval
                   "spot_n": max(1, spot_n), "ip_check": bool(ip_check),
                   "bind_minutes": max(3, min(bind_minutes, 30)),  # C-T7：3–30 分鐘，同 course_settings 的範圍驗證
                   "discord_channel": discord_channel})
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps(st), cid))
    return RedirectResponse(f"/t/course/{cid}/students", 303)


def pct(rate) -> str:
    """出席率百分比，四捨五入（不用銀行家進位：62.5% 要顯示 63%）。"""
    if rate is None:
        return ""
    return f"{int(rate * 100 + 0.5)}%"


def csv_safe(v: str) -> str:
    """Excel 會把 = + - @ 開頭的儲存格當公式，前面補一個單引號。"""
    return "'" + v if v and v[0] in "=+-@" else v


def build_report(con, cid: int):
    finalize_expired(con)
    sessions = con.execute(
        f"SELECT * FROM sessions WHERE course_id=? AND closed_at IS NOT NULL AND {COUNTED_SESSION_SQL} ORDER BY opened_at",
        (cid,)).fetchall()
    students = con.execute("SELECT * FROM students WHERE course_id=? ORDER BY student_id", (cid,)).fetchall()
    grid = {}
    for k in con.execute("SELECT session_id, student_pk, status FROM checkins WHERE session_id IN "
                         "(SELECT id FROM sessions WHERE course_id=? AND closed_at IS NOT NULL)", (cid,)):
        grid[(k["session_id"], k["student_pk"])] = k["status"]
    rows = []
    for stu in students:
        cells = [grid.get((s["id"], stu["id"]), NA if s["opened_at"] < stu["created_at"] else "absent") for s in sessions]
        counts = {k: cells.count(k) for k in STATUS_LABEL}
        denom = len([c for c in cells if c != NA]) - counts["excused"]
        rate = (counts["present"] + counts["late"]) / denom if denom > 0 else None
        rows.append({"stu": stu, "cells": cells, "counts": counts, "rate": rate})
    return sessions, rows


@app.get("/t/course/{cid}/report", response_class=HTMLResponse)
def report_page(req: Request, cid: int = PathParam(ge=1, le=2**53)):
    if r := teacher_redirect(req):
        return r
    with db() as con:
        c = get_course(con, cid)
        sessions, rows = build_report(con, cid)
    return templates.TemplateResponse(req, "report.html", {"c": c, "sessions": sessions, "rows": rows})


@app.get("/t/course/{cid}/export/csv")  # 不用 .csv 副檔名：Cloudflare 會把靜態副檔名整份快取到邊緣
def report_csv(req: Request, cid: int = PathParam(ge=1, le=2**53)):
    require_teacher(req)
    with db() as con:
        c = get_course(con, cid)
        sessions, rows = build_report(con, cid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["學號", "姓名", "班級"] + [fmt(s["opened_at"], True) for s in sessions]
               + ["出席", "遲到", "缺席", "請假", "待確認", "出席率"])
    for r in rows:
        w.writerow([csv_safe(r["stu"]["student_id"]), csv_safe(r["stu"]["name"]), csv_safe(r["stu"]["cls"])]
                   + [STATUS_LABEL.get(x, "—") for x in r["cells"]]
                   + [r["counts"][k] for k in ("present", "late", "absent", "excused", "pending")]
                   + [pct(r["rate"])])
    data = "﻿" + buf.getvalue()
    return PlainTextResponse(data, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{c["code"]}-attendance.csv"',
                                      "Cache-Control": "private, no-store"})


# ───────────────────────── 老師 API ─────────────────────────

@app.get("/api/t/session/{sid}/qr")
def api_qr(req: Request, sid: int = PathParam(ge=1, le=2**53), have: str = ""):
    require_teacher(req)
    with db() as con:
        s = get_session(con, sid)
        c = get_course(con, s["course_id"])
        st = course_settings(c)
        n_ok = con.execute("SELECT COUNT(*) FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=? AND k.status IN ('present','late')",
                           (sid,)).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM students WHERE course_id=? AND (created_at<=? OR id IN (SELECT student_pk FROM checkins WHERE session_id=?))",
                            (s["course_id"], s["opened_at"], sid)).fetchone()[0]
    phase = session_phase(s)
    iv = st["qr_interval"]
    slot = int(now() // iv)
    sig = slot_sig(s, slot)
    url = f"{BASE_URL}/s/{sid}/{slot}/{sig}"
    return {
        "phase": phase, "svg": (qr_svg(url) if (phase != "closed" and code6(sig) != have) else ""), "code": code6(sig),
        "remain": round((slot + 1) * iv - now(), 1), "interval": iv,
        "n_ok": n_ok, "total": total,
        "open_until": s["open_until"], "late_until": s["late_until"], "now": now(),
        "kind": s["kind"],  # B030：投影頁要標「綁定場次」
    }


@app.post("/api/t/session/{sid}/teacher-ip")
def api_teacher_ip(req: Request, sid: int = PathParam(ge=1, le=2**53), force: str = Form("")):
    """投影頁載入時回報教室出口 IP（IP 比對標記的基準）。只記本場第一次；老師在點名頁按「以這台為教室 IP」才覆寫。"""
    require_teacher(req)
    with db() as con:
        s = get_session(con, sid)
        if session_phase(s) == "closed":
            raise HTTPException(409, "點名已結束")
        if s["teacher_ip"] and force != "1":
            return {"ok": True, "ip": s["teacher_ip"], "kept": True}
        con.execute("UPDATE sessions SET teacher_ip=? WHERE id=?", (client_ip(req), sid))
    return {"ok": True, "ip": client_ip(req)}


def compute_funnel(con: sqlite3.Connection, sid: int) -> dict:
    """B004/B026 即時漏斗：本場累計與最近 60 秒各一組。"""
    t = now()

    def group(since: float | None) -> dict:
        extra_sql = " AND ts>=?" if since is not None else ""

        def cnt(kind: str) -> int:
            params = (sid, kind) if since is None else (sid, kind, since)
            return con.execute(f"SELECT COUNT(*) FROM events WHERE session_id=? AND kind=?{extra_sql}", params).fetchone()[0]

        reject_params = (sid,) if since is None else (sid, since)
        reject_rows = con.execute(
            f"SELECT reason, COUNT(*) n FROM events WHERE session_id=? AND kind='checkin_reject'{extra_sql} GROUP BY reason",
            reject_params).fetchall()
        reject_by_reason = {r["reason"]: r["n"] for r in reject_rows}
        land_params = (sid,) if since is None else (sid, since)
        land_rows = con.execute(
            f"SELECT device8, MAX(ts) t0 FROM events WHERE session_id=? AND kind='land_ok' AND device8!=''{extra_sql} GROUP BY device8",
            land_params).fetchall()
        silent = 0
        for r in land_rows:
            if t - r["t0"] < 90:  # 還沒滿 90 秒，先不算沉默
                continue
            attempted = con.execute(
                "SELECT 1 FROM events WHERE session_id=? AND device8=? AND kind IN ('checkin_ok','checkin_reject') AND ts>? LIMIT 1",
                (sid, r["device8"], r["t0"])).fetchone()
            if not attempted:
                silent += 1
        return {
            "land_ok": cnt("land_ok"), "land_expired": cnt("land_expired"), "checkin_ok": cnt("checkin_ok"),
            "reject": sum(reject_by_reason.values()), "reject_by_reason": reject_by_reason,
            "silent": silent, "client_errors": cnt("client"),
        }

    return {"total": group(None), "recent60": group(t - 60)}


def compute_stuck(con: sqlite3.Connection, sid: int) -> list[dict]:
    """B004/B026：最近 3 分鐘內被拒 2 次以上、且尚未成功的學生，給老師走過去幫忙。"""
    t = now()
    rows = con.execute(
        "SELECT student_pk, COUNT(*) n, MAX(id) last_id FROM events "
        "WHERE session_id=? AND kind='checkin_reject' AND student_pk IS NOT NULL AND ts>? "
        "GROUP BY student_pk HAVING COUNT(*)>=2", (sid, t - 180)).fetchall()
    out = []
    for r in rows:
        k = con.execute("SELECT status FROM checkins WHERE session_id=? AND student_pk=?", (sid, r["student_pk"])).fetchone()
        if k and k["status"] in ("present", "late", "pending"):
            continue  # 已經成功（含老師手動放行），不算卡住
        stu = con.execute("SELECT student_id, name FROM students WHERE id=?", (r["student_pk"],)).fetchone()
        if not stu:
            continue
        last = con.execute("SELECT reason FROM events WHERE id=?", (r["last_id"],)).fetchone()
        reason = last["reason"] if last else ""
        out.append({"pk": r["student_pk"], "student_id": stu["student_id"], "name": stu["name"],
                    "reason": reason, "reason_label": REJECT_REASON_LABEL.get(reason, reason)})
    return out


def compute_ip_check_muted(out_rows: list[dict], teacher_ip: str) -> bool:
    """C-S6：多數人不是走教室網路時（FIELD_2026-09-10.md 第 18 行，9 支手機各自不同出口 IP），
    「IP 與教室不同」這個旗標對老師沒有參考性。判準：本場 source='qr' 且 ip 非空的簽到 ≥3 筆，
    其中與 teacher_ip 相同的不到一半 → 本場視為「多數人不走教室網路」。"""
    if not teacher_ip:
        return False
    qr_ips = [r["ip"] for r in out_rows if r["source"] == "qr" and r["ip"]]
    if len(qr_ips) < 3:
        return False
    matched = sum(1 for ip in qr_ips if ip == teacher_ip)
    return matched < len(qr_ips) / 2


@app.get("/api/t/session/{sid}/state")
def api_state(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    require_teacher(req)
    with db() as con:
        finalize_expired(con)
        s = get_session(con, sid)
        rows = con.execute(
            "SELECT st.id AS pk, st.student_id, st.name, st.cls, st.device_hash IS NOT NULL AS bound, "
            "k.ts, k.seat, k.status, k.flags, k.ip, k.source "
            "FROM students st LEFT JOIN checkins k ON k.student_pk=st.id AND k.session_id=? "
            "WHERE st.course_id=? ORDER BY st.student_id", (sid, s["course_id"])).fetchall()
        spots = con.execute("SELECT sc.*, st.name, st.student_id FROM spotchecks sc JOIN students st ON st.id=sc.student_pk "
                            "WHERE session_id=? ORDER BY sc.id", (sid,)).fetchall()
        st = course_settings(get_course(con, s["course_id"]))
        locked = {r["student_pk"] for r in con.execute(
            "SELECT student_pk FROM pin_fail WHERE session_id=? AND ((device_hash='' AND n>=?) OR (device_hash!='' AND n>=?))",
            (sid, PIN_MAX_FAIL_TOTAL, st["pin_max_fail"]))}
        funnel = compute_funnel(con, sid)
        stuck = compute_stuck(con, sid)
        auto_absent_n = con.execute(
            "SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).fetchone()[0]
    out = []
    for r in rows:
        out.append({
            "pk": r["pk"], "student_id": r["student_id"], "name": r["name"], "cls": r["cls"], "bound": bool(r["bound"]),
            "ts": fmt(r["ts"]) if (r["ts"] or 0) > 0 and r["source"] != "auto" else "", "seat": r["seat"] or "", "status": r["status"] or "",
            "locked": r["pk"] in locked,
            "flags": json.loads(r["flags"]) if r["flags"] else [], "ip": r["ip"] or "", "source": r["source"] or "",
        })
    ip_muted = compute_ip_check_muted(out, s["teacher_ip"])
    if ip_muted:  # C-S6：原始事實留在 DB（CSV 照舊），序列化給老師看的這份濾掉沒有參考性的旗標
        for r in out:
            if "ip_differs" in r["flags"]:
                r["flags"] = [f for f in r["flags"] if f != "ip_differs"]
    counts = {k: sum(1 for x in out if x["status"] == k) for k in STATUS_LABEL}
    counts["none"] = sum(1 for x in out if not x["status"])
    is_bind = s["kind"] == "bind"
    # C-T6：綁定場次不論課程的 allow_bind 設定，一律允許首次綁定（B030），老師頁的開關要照實顯示
    # 這場是強制開放，而不是誤導老師以為那顆開關真的在控制這一場
    allow_bind_out = True if is_bind else st["allow_bind"]
    return {"phase": session_phase(s), "rows": out, "counts": counts, "teacher_ip": s["teacher_ip"],
            "allow_bind": allow_bind_out, "bind_forced": is_bind, "course_id": s["course_id"],
            "discord": bool(st["discord_channel"]),
            "discord_held": s["note"] == "discord_held",
            "open_until": s["open_until"], "late_until": s["late_until"], "now": now(),
            "funnel": funnel, "stuck": stuck,
            "kind": s["kind"], "voided": s["voided_at"] is not None, "auto_absent_n": auto_absent_n,  # B030/B028
            "ip_check_muted": ip_muted,  # C-S6：多數人不走教室網路時靜音 ip_differs
            "bound_n": sum(1 for r in out if r["bound"]),  # C-T2：綁定場次的 counts 只呈現「已綁定 N 人」
            "spots": [{"id": x["id"], "pk": x["student_pk"], "name": x["name"], "student_id": x["student_id"],
                       "seat": x["seat"], "result": x["result"]} for x in spots]}


def do_extend(con: sqlite3.Connection, s: sqlite3.Row, minutes: int) -> str:
    """延長一場點名，回傳延長後的 phase。B030：綁定場次沒有遲到區，open_until／late_until 同步延長。"""
    if s["closed_at"] or now() > s["late_until"]:
        raise HTTPException(409, "這次點名已經結束，不能延長；要再點名請回主頁重新開始")
    add = max(1, minutes) * 60
    if s["kind"] == "bind":
        new_until = s["open_until"] + add
        con.execute("UPDATE sessions SET open_until=?, late_until=? WHERE id=?", (new_until, new_until, s["id"]))
        return "open"
    st = course_settings(get_course(con, s["course_id"]))
    if session_phase(s) == "open":
        new_open = s["open_until"] + add
        tail = max(60, (st["late_minutes"] - st["open_minutes"]) * 60)  # 遲到區長度照設定，不壓成 60 秒
        con.execute("UPDATE sessions SET open_until=?, late_until=MAX(late_until,?) WHERE id=?", (new_open, new_open + tail, s["id"]))
        return "open"
    # 已進遲到區：只把遲到窗往後延，不讓晚到的人反而拿到出席
    con.execute("UPDATE sessions SET late_until=late_until+? WHERE id=?", (add, s["id"]))
    return "late"


@app.post("/api/t/session/{sid}/extend")
def api_extend(req: Request, sid: int = PathParam(ge=1, le=2**53), minutes: int = Form(2, ge=1, le=120)):
    require_teacher(req)
    with db() as con:
        s = get_session(con, sid)
        phase = do_extend(con, s, minutes)
    return {"ok": True, "phase": phase}


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")


def discord_post(channel: str, content: str) -> tuple[bool, str]:
    if not (DISCORD_TOKEN and channel):
        return False, "沒有設定 Discord token 或頻道"
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel}/messages", data=body, method="POST",
        headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json",
                 "User-Agent": f"DiscordBot ({BASE_URL}, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 固定 https://discord.com 網址，非使用者可控 scheme
            return r.status < 300, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def discord_post_long(channel: str, content: str) -> tuple[bool, str]:
    """超過 Discord 2000 字上限就按行切成多則。"""
    chunks, cur = [], ""
    for raw_line in content.split("\n"):
        rest = raw_line
        while len(rest) > 1900:  # 單行超長硬切
            chunks.append(rest[:1900]); rest = rest[1900:]
        if len(cur) + len(rest) + 1 > 1900 and cur:
            chunks.append(cur); cur = ""
        cur += ("\n" if cur else "") + rest
    if cur:
        chunks.append(cur)
    ok, msgs = True, []
    for ch in chunks:
        ok2, msg = discord_post(channel, ch)
        ok = ok and ok2
        if not ok2:
            msgs.append(msg)
    return ok, "; ".join(msgs) if msgs else "OK"


def session_summary(con, sid: int) -> tuple[str, str]:
    """回傳（Discord 頻道, 訊息文字）。"""
    s = get_session(con, sid)
    c = get_course(con, s["course_id"])
    st = course_settings(c)
    rows = con.execute(
        "SELECT st.student_id, st.name, k.status FROM students st LEFT JOIN checkins k "
        "ON k.student_pk=st.id AND k.session_id=? WHERE st.course_id=? AND (st.created_at<=? OR k.id IS NOT NULL) ORDER BY st.student_id",
        (sid, c["id"], s["opened_at"])).fetchall()
    groups = {k: [r for r in rows if r["status"] == k] for k in STATUS_LABEL}
    d = datetime.fromtimestamp(s["opened_at"], TZ)
    lines = [f"【{c['name']}】{d.month}/{d.day} 點名結果：出席 {len(groups['present'])}、遲到 {len(groups['late'])}、"
             f"缺席 {len(groups['absent'])}、請假 {len(groups['excused'])}、待確認 {len(groups['pending'])}，全班 {len(rows)} 人"]
    for key, title in (("absent", "缺席"), ("late", "遲到"), ("pending", "待確認")):
        if groups[key]:
            lines.append(f"\n{title}：")
            lines += [f"{r['student_id']} {r['name']}" for r in groups[key]]
    if groups["absent"]:
        lines.append("\n有到課但沒簽到成功的，請下課直接找老師。")
    return st["discord_channel"], "\n".join(lines)


@app.post("/api/t/session/{sid}/close")
def api_close(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    require_teacher(req)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")  # 與 delete／finalize 互斥，不留孤兒列
        s = get_session(con, sid)
        if s["closed_at"]:
            return {"ok": True, "discord": False, "note": "已經結束過了"}
        close_session_rows(con, s, now())
        con.commit()
        if s["kind"] != "normal":  # B030：綁定場次不發 Discord
            return {"ok": True, "discord": False}
        channel, text = session_summary(con, sid)
    if channel:
        threading.Thread(target=lambda: discord_post_long(channel, text), daemon=True).start()
    return {"ok": True, "discord": bool(channel)}


def do_notify_prepare(con: sqlite3.Connection, sid: int) -> tuple[str, str]:
    """C-T1：組訊息前先跑一次 finalize_expired——遲到窗剛過、還沒人開過老師頁時，缺席列還沒寫入，
    這裡不補上就會漏掉沒簽到的人。綁定場次與已作廢場次不計出缺勤，沒有名單可發，一律 409。"""
    finalize_expired(con)  # 自己 commit
    s = get_session(con, sid)
    if s["kind"] != "normal":
        raise HTTPException(409, "綁定場次不計出缺勤，沒有名單可以發到 Discord")
    if s["voided_at"] is not None:
        raise HTTPException(409, "已作廢的場次不計出缺勤，沒有名單可以發到 Discord")
    if session_phase(s) != "closed":
        raise HTTPException(409, "點名還沒結束，結束後再發")
    return session_summary(con, sid)


@app.post("/api/t/session/{sid}/notify")
def api_notify(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    """手動（重新）把這次點名結果發到 Discord。"""
    require_teacher(req)
    with db() as con:
        channel, text = do_notify_prepare(con, sid)
    ok, msg = discord_post_long(channel, text)
    if not ok:
        raise HTTPException(502, f"Discord 發送失敗：{msg}")
    with db() as con:  # B041：老師確認後手動發出，暫緩提示就該消掉
        con.execute("UPDATE sessions SET note='' WHERE id=? AND note='discord_held'", (sid,))
    return {"ok": True}


@app.post("/api/t/course/{cid}/bind")
def api_toggle_bind(req: Request, cid: int = PathParam(ge=1, le=2**53), allow: str = Form(...)):
    """點名頁上的「首次綁定」開關。"""
    require_teacher(req)
    with db() as con:
        c = get_course(con, cid)
        st = course_settings(c)
        st["allow_bind"] = allow == "1"
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps(st), cid))
    return {"ok": True, "allow_bind": st["allow_bind"]}


@app.post("/api/t/session/{sid}/delete")
def api_delete_session(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    """整筆刪掉這次點名（誤開、測試用）。已結束的也能刪，報表不再計入。"""
    require_teacher(req)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        get_session(con, sid)
        for t in ("checkins", "pin_fail", "spotchecks", "events"):
            con.execute(f"DELETE FROM {t} WHERE session_id=?", (sid,))
        con.execute("DELETE FROM sessions WHERE id=?", (sid,))
    return {"ok": True}


def do_void_session(con: sqlite3.Connection, sid: int, void: bool) -> None:
    """B028：本場作廢但保留紀錄。只有已結案的場次可作廢／取消作廢；紀錄不動，只標記 voided_at，
    報表／CSV／`/api/me`（走共用定義 counts_toward_attendance）就會自動不再算這一場。"""
    s = get_session(con, sid)
    if s["closed_at"] is None:
        raise HTTPException(409, "只有已經結束的場次可以作廢或取消作廢")
    con.execute("UPDATE sessions SET voided_at=? WHERE id=?", (now() if void else None, sid))
    if void:
        # C-T1：作廢的場次不計出缺勤，暫緩發送 Discord 的提示（discord_held）也該跟著消掉，
        # 否則老師頁會一直叫他去按一顆現在按了也是 409 的「發到 Discord」
        con.execute("UPDATE sessions SET note='' WHERE id=? AND note='discord_held'", (sid,))


@app.post("/api/t/session/{sid}/void")
def api_void_session(req: Request, sid: int = PathParam(ge=1, le=2**53), void: str = Form(...)):
    require_teacher(req)
    with db() as con:
        do_void_session(con, sid, void == "1")
    return {"ok": True}


BULK_STATUS_CHOICES = ("present", "late", "excused")  # C-T5：拿掉 absent——缺席等於沒改狀態卻把 source='auto' 弄丟


def do_bulk_status(con: sqlite3.Connection, sid: int, to_status: str) -> int:
    """B028：只把本場「自動缺席」（source='auto' AND status='absent'）的列改成 to_status，
    不動老師手動設的缺席、也不動學生自己簽的列。只有已結案的場次可用，回傳更正的筆數。"""
    s = get_session(con, sid)
    if s["closed_at"] is None:
        raise HTTPException(409, "只有已經結束的場次可以整批更正")
    if to_status not in BULK_STATUS_CHOICES:
        raise HTTPException(400, "狀態不合法")
    rows = con.execute("SELECT id, flags FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).fetchall()
    for r in rows:
        flags = json.loads(r["flags"])
        if "manual" not in flags:
            flags.append("manual")
        con.execute("UPDATE checkins SET status=?, source='manual', flags=?, ts=0 WHERE id=?",
                    (to_status, json.dumps(flags), r["id"]))
    return len(rows)


@app.post("/api/t/session/{sid}/bulk-status")
def api_bulk_status(req: Request, sid: int = PathParam(ge=1, le=2**53), to: str = Form(...)):
    require_teacher(req)
    with db() as con:
        n = do_bulk_status(con, sid, to)
    return {"ok": True, "n": n}


@app.post("/api/t/session/{sid}/spotcheck")
def api_spotcheck(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    require_teacher(req)
    with db() as con:
        s = get_session(con, sid)
        if session_phase(s) == "closed":
            raise HTTPException(409, "點名已結束，不能再抽點")
        # C-S2：B030「綁定場次不開放抽點」作廢，改為開放（叫到不在的人會撤回綁定）
        st = course_settings(get_course(con, s["course_id"]))
        already = {r["student_pk"] for r in con.execute("SELECT student_pk FROM spotchecks WHERE session_id=?", (sid,))}
        cands = [r for r in con.execute(
            "SELECT k.student_pk, k.seat, st.name, st.student_id FROM checkins k JOIN students st ON st.id=k.student_pk "
            "WHERE k.session_id=? AND k.status IN ('present','late','pending') AND k.source!='auto'", (sid,))
                 if r["student_pk"] not in already]
        picks = random.sample(cands, min(st["spot_n"], len(cands)))
        t = now()
        out = []
        for p in picks:
            cur = con.execute("INSERT INTO spotchecks(session_id, student_pk, seat, ts) VALUES(?,?,?,?)",
                              (sid, p["student_pk"], p["seat"], t))
            out.append({"id": cur.lastrowid, "pk": p["student_pk"], "seat": p["seat"], "name": p["name"],
                        "student_id": p["student_id"], "result": None})
        any_qr = con.execute("SELECT 1 FROM checkins WHERE session_id=? AND source='qr' LIMIT 1", (sid,)).fetchone()
    return {"ok": True, "picks": out, "note": "" if out else ("已簽到的人都抽過了" if any_qr else "還沒有人簽到，沒有可抽的人")}


@app.post("/api/t/spotcheck/{scid}")
def api_spot_result(req: Request, scid: int = PathParam(ge=1, le=2**53), present: str = Form(...)):
    require_teacher(req)
    ok = present == "1"
    with db() as con:
        sc = con.execute("SELECT * FROM spotchecks WHERE id=?", (scid,)).fetchone()
        if not sc:
            raise HTTPException(404)
        ses = get_session(con, sc["session_id"])
        if session_phase(ses) == "closed":
            raise HTTPException(409, "點名已結束，抽點結果不能再改")  # 先檢查再寫，不靠回滾
        con.execute("UPDATE spotchecks SET result=? WHERE id=?", ("ok" if ok else "absent", scid))
        unbound = False
        # C-S2：綁定場次抽到不在場，那筆綁定很可能不是本人做的，撤回讓本人重新掃碼
        if not ok and ses["kind"] == "bind":
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL, pin_hash=NULL, pin_salt=NULL WHERE id=?",
                        (sc["student_pk"],))
            unbound = True
        k = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?",
                        (sc["session_id"], sc["student_pk"])).fetchone()
        if k:
            flags = [f for f in json.loads(k["flags"]) if f not in ("spot_ok", "spot_absent")]
            flags.append("spot_ok" if ok else "spot_absent")
            status = k["status"]
            if not ok:
                status = "absent"
                # 幫他簽的那支手機（同裝置的其他簽到）一併記缺席
                if k["device_hash"]:
                    for o in con.execute("SELECT id, flags FROM checkins WHERE session_id=? AND device_hash=? AND id!=? AND source='qr'",
                                         (sc["session_id"], k["device_hash"], k["id"])).fetchall():
                        of = json.loads(o["flags"])
                        if "spot_ok" in of:  # 老師已當場確認在場的人不連坐
                            continue
                        if "proxy_for" not in of:
                            of.append("proxy_for")
                        con.execute("UPDATE checkins SET status='absent', flags=? WHERE id=?", (json.dumps(of), o["id"]))
            elif status == "pending":
                status = "late" if "was_late" in flags else "present"  # 簽到當下記的窗口
            con.execute("UPDATE checkins SET flags=?, status=? WHERE id=?", (json.dumps(flags), status, k["id"]))
    return {"ok": True, "unbound": True} if unbound else {"ok": True}


@app.post("/api/t/session/{sid}/status")
def api_set_status(req: Request, sid: int = PathParam(ge=1, le=2**53), pk: int = Form(..., ge=1, le=2**53), status: str = Form(...), seat: str = Form("", max_length=8)):
    """老師手動設定某學生本次狀態（含手動簽到）。"""
    require_teacher(req)
    if status not in STATUS_LABEL:
        raise HTTPException(400, "狀態不合法")
    with db() as con:
        s = get_session(con, sid)
        if s["kind"] != "normal":  # C-T2：綁定場次不計出缺勤，手動簽到走的是同一支端點，一併擋下
            raise HTTPException(409, "綁定場次不計出缺勤，要補登請另開一般點名")
        stu = con.execute("SELECT * FROM students WHERE id=? AND course_id=?", (pk, s["course_id"])).fetchone()
        if not stu:
            raise HTTPException(404, "學生不在名冊")
        k = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()
        if k:
            flags = [f for f in json.loads(k["flags"]) if f not in ("spot_ok", "spot_absent", "proxy_for")]
            if "manual" not in flags:
                flags.append("manual")
            new_ts = 0 if k["source"] == "auto" else k["ts"]  # 自動缺席列改成請假／出席：沒有真正的簽到時間
            con.execute("UPDATE checkins SET status=?, flags=?, source='manual', ts=?, seat=COALESCE(NULLIF(?,''), seat) WHERE id=?",
                        (status, json.dumps(flags), new_ts, seat, k["id"]))
        else:
            con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, source) "
                        "VALUES(?,?,?,?,?,?,'manual')", (sid, pk, 0, seat, status, json.dumps(["manual"])))
    return {"ok": True}


@app.post("/api/t/student/{pk}/unbind")
def api_unbind(req: Request, pk: int = PathParam(ge=1, le=2**53), reset_pin: str = Form("")):
    require_teacher(req)
    with db() as con:
        if not con.execute("SELECT 1 FROM students WHERE id=?", (pk,)).fetchone():
            raise HTTPException(404, "找不到這位學生")
        if reset_pin:
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL, pin_hash=NULL, pin_salt=NULL WHERE id=?", (pk,))
        else:
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL WHERE id=?", (pk,))
    return {"ok": True}


# ───────────────────────── 學生端 ─────────────────────────

def set_device_cookie(resp, key: str):
    """C-S7：所有下發裝置識別 cookie 的地方收斂到這一支，path 固定「/」、不設 domain。"""
    resp.set_cookie(DK_COOKIE, key, path="/", httponly=True, samesite="lax",
                    secure=BASE_URL.startswith("https"), max_age=400 * 86400)
    return resp


def _migrate_legacy_cookie(req: Request, resp, resolved_key: str):
    """C-S7：這次回應如果是靠舊名 attend_dk 才認出裝置的（新名讀不到、舊名剛好等於解出的值），
    順便補發新名、刪掉舊名——9/10 已綁的那幾支手機不必重綁，之後請求就只剩新名。"""
    if DK_COOKIE != LEGACY_DK_COOKIE and not _valid_device_cookie(req.cookies.get(DK_COOKIE, "")) \
            and req.cookies.get(LEGACY_DK_COOKIE, "") == resolved_key:
        set_device_cookie(resp, resolved_key)
        resp.delete_cookie(LEGACY_DK_COOKIE, path="/")
    return resp


def with_device_cookie(req: Request, payload: dict, device_key: str) -> JSONResponse:
    resp = JSONResponse(payload)
    set_device_cookie(resp, device_key)  # 每次簽到續期
    return _migrate_legacy_cookie(req, resp, device_key)


def grant_for(sid: int, settings: dict, dh16: str) -> str:
    # 帶隨機 nonce：兩個學生同一秒掃碼不能拿到同一個 grant（grant 會綁第一支用它的手機）
    # 帶發證時間（A3/B029）：出席／遲到判定看「掃到」這一刻，不是送出表單那一刻
    # C-S5：第五段帶掃到當下那支裝置的雜湊前 16 碼——憑證在下發時就綁裝置，轉貼給別支手機
    # 第一次使用就會被擋，不必等到 grants 表在「第一次使用」才記錄
    return make_token("g", str(sid), secrets.token_hex(6), str(now()), dh16, exp=now() + settings["grant_seconds"])


def render_student(req: Request, con, s: sqlite3.Row, c: sqlite3.Row, st: dict, error: str = ""):
    """C-S5：這次回應要下發的裝置識別，跟塞進 grant 第五段的裝置雜湊必須是同一個值——
    既有 cookie 就用它，沒有就這裡當場產生一個，讓 grant 與稍後 ensure_device_cookie_value
    下發的 cookie 用同一支裝置，不能先建 grant 再讓 cookie 各自隨機一個。"""
    device_key = effective_device_key(req, con) or secrets.token_urlsafe(24)
    dh16 = hash_device(device_key)[:16]
    phase = session_phase(s)
    if phase == "closed":
        resp = templates.TemplateResponse(req, "student.html",
            {"closed": True, "expired": False, "c": c, "error": error})  # B021：結束頁也要下發裝置 cookie
        return ensure_device_cookie_value(req, resp, device_key)
    resp = templates.TemplateResponse(req, "student.html", {
        "closed": False, "expired": False, "c": c, "s": s, "phase": phase,
        "grant": grant_for(s["id"], st, dh16), "error": error, "grant_seconds": st["grant_seconds"],
    })
    return ensure_device_cookie_value(req, resp, device_key)


@app.get("/s/{sid}/{slot}/{sig}", response_class=HTMLResponse)
def student_entry(req: Request, sid: int = PathParam(ge=1, le=2**53), slot: int = PathParam(ge=0, le=2**53), sig: str = PathParam(max_length=64)):
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    with db() as con:
        dev_key = effective_device_key(req, con)  # C-S7：只查一次，供落地事件與後續共用
        device8 = hash_device(dev_key)[:8] if dev_key else ""  # B004：掃碼落地事件
        s = get_session(con, sid)
        c = get_course(con, s["course_id"])
        st = course_settings(c)
        if not re.fullmatch(r"[0-9a-f]{10}", sig) or slot not in current_slots(s, st) \
                or not hmac.compare_digest(slot_sig(s, slot), sig):
            log_event(sid, "land_expired", device8=device8, ua_class=ua_class)
            return ensure_device_cookie(req, templates.TemplateResponse(req, "student.html", {
                "closed": False, "c": c, "expired": True, "error": "這個 QR 已經過期，請重新掃描投影幕上的 QR"},
                status_code=410))  # B021：過期頁也要下發裝置 cookie，否則沒 cookie 的人一路退到共用的指紋限流桶
        log_event(sid, "land_ok", device8=device8, ua_class=ua_class)
        return render_student(req, con, s, c, st)


@app.get("/me", response_class=HTMLResponse)
def me_page(req: Request):
    return ensure_device_cookie(req, templates.TemplateResponse(req, "me.html", {}))


@app.post("/api/me")
def api_me(req: Request, device_key: str = Form("", max_length=200)):
    """學生自查：用綁定的手機看自己整學期的出缺勤。只讀，不結案；有限流。"""
    device_key = effective_device_key(req)
    if not device_key:
        return {"courses": []}
    if _throttle(_checkin_hits, "me:" + device_key, 30, 60):  # 以裝置計，整班同一出口不會互相鎖
        raise HTTPException(429, "太頻繁，等一分鐘")
    dh = hash_device(device_key)
    out = []
    with db() as con:
        for stu in con.execute("SELECT * FROM students WHERE device_hash=?", (dh,)).fetchall():
            c = get_course(con, stu["course_id"])
            sessions = con.execute(
                f"SELECT * FROM sessions WHERE course_id=? AND (closed_at IS NOT NULL OR late_until<?) AND {COUNTED_SESSION_SQL} "
                "ORDER BY opened_at", (c["id"], now())).fetchall()  # 逾時但老師還沒開頁結案的也算已結束
            items = []
            for ss in sessions:
                k = con.execute("SELECT status, ts, source FROM checkins WHERE session_id=? AND student_pk=?",
                                (ss["id"], stu["id"])).fetchone()
                if not k and ss["opened_at"] < stu["created_at"]:
                    continue  # 加選前又沒補登的場次不列
                status = k["status"] if k else "absent"
                items.append({"date": fmt(ss["opened_at"], True), "status": status, "label": STATUS_LABEL[status],
                              "ts": fmt(k["ts"]) if k and (k["ts"] or 0) > 0 and k["source"] != "auto" else ""})
            counts = {k: sum(1 for i in items if i["status"] == k) for k in STATUS_LABEL}
            denom = len(items) - counts["excused"]
            rate = (counts["present"] + counts["late"]) / denom if denom > 0 else None
            out.append({"course": c["name"], "student_id": stu["student_id"], "name": stu["name"],
                        "items": items, "counts": counts, "rate": rate})
    return {"courses": out}


@app.get("/help", response_class=HTMLResponse)
def help_page(req: Request):
    return templates.TemplateResponse(req, "help.html", {})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """B012：沒有這支路由時每支手機都會多打一個 404。204＋長快取，不是個人化內容，不受全站 no-store 影響。"""
    return Response(status_code=204, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/c", response_class=HTMLResponse)
def code_page(req: Request):
    return ensure_device_cookie(req, templates.TemplateResponse(req, "code.html", {"error": ""}))


_code_fail: dict[str, list[float]] = {}
_checkin_hits: dict[str, list[float]] = {}


def _throttle(bucket: dict, key: str, limit: int, window: float) -> bool:
    """回 True 代表超過限制。行程內記憶體，前提是單一 worker。"""
    t = now()
    for k in [k for k, v in bucket.items() if all(t - x >= window for x in v)]:
        bucket.pop(k, None)
    hits = [x for x in bucket.get(key, []) if t - x < window]
    if len(hits) >= limit:
        return True
    hits.append(t); bucket[key] = hits
    return False


@app.post("/c", response_class=HTMLResponse)
def code_submit(req: Request, code: str = Form(..., max_length=32)):
    # 全形數字轉半形；只留 ASCII 0-9（str.isdigit 會放過「²」這類 Unicode 數字，進 compare_digest 會炸）
    code = "".join(ch for ch in code.translate(str.maketrans("０１２３４５６７８９", "0123456789")) if ch in "0123456789")
    ip = client_ip(req) or "noip"
    device_key = effective_device_key(req)
    has_device = bool(device_key)
    device8 = hash_device(device_key)[:8] if device_key else ""  # B004：六位數落地事件
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    fpk = device_key or ("fp:" + fingerprint(req))  # 有裝置 cookie 就以裝置計；沒有才退到 IP＋UA 指紋
    # 每支手機 10 次/分（只算失敗）、整個出口 300 次/分：整班同一校園 NAT 不會互相鎖，機器連猜仍被擋
    # B021：沒有 cookie 時大家都共用同一把 fp 桶（同 IP 同 UA），額度要放寬到跟每 IP 一樣，
    # 否則全班第一次落地（還沒拿到 cookie）就會互相把彼此鎖住
    b_limit = 10 if has_device else 300
    if len([t for t in _code_fail.get("b:" + fpk, []) if now() - t < 60]) >= b_limit or \
            len([t for t in _code_fail.get("ip:" + ip, []) if now() - t < 60]) >= 300:
        return ensure_device_cookie(req, templates.TemplateResponse(
            req, "code.html", {"error": "輸錯太多次，等一分鐘，或直接掃投影幕的 QR、找老師手動簽到"}, status_code=429))
    with db() as con:
        live = con.execute("SELECT * FROM sessions WHERE closed_at IS NULL AND late_until>?", (now(),)).fetchall()
        if not live:
            return ensure_device_cookie(req, templates.TemplateResponse(
                req, "code.html", {"error": "目前沒有進行中的點名，等老師開始再輸"}, status_code=400))
        for s in live:
            c = get_course(con, s["course_id"])
            st = course_settings(c)
            for slot in current_slots(s, st):
                if hmac.compare_digest(code6(slot_sig(s, slot)), code):
                    log_event(s["id"], "code_ok", device8=device8, ua_class=ua_class)
                    return render_student(req, con, s, c, st)
    for key in ("b:" + fpk, "ip:" + ip):
        _throttle(_code_fail, key, 10**9, 60)  # 只記錄失敗次數（上限交給前面的判斷）
    log_event(None, "code_bad", device8=device8, ua_class=ua_class)  # 沒對到任一場次，不確定是哪一場
    return ensure_device_cookie(req, templates.TemplateResponse(
        req, "code.html", {"error": "代碼不對或已過期，請看投影幕重新輸入"}, status_code=400))


def _valid_device_cookie(ck: str) -> bool:
    return bool(ck) and len(ck) >= 16 and ck.isascii() and ck.replace("-", "").replace("_", "").isalnum()


def effective_device_key(req: Request, con: sqlite3.Connection | None = None) -> str:
    """只認伺服器下發的裝置 cookie（httponly，Safari 不會七天清掉）。前端自報的值一律不採：
    否則不帶 cookie 就能每次換身分替全班簽到。沒有 cookie 就回空字串。
    C-S7：新名（DK_COOKIE）優先；讀不到時，只有在雙名模式（https，新舊名不同）才退回舊名
    attend_dk，且只有它的雜湊已經綁在某個學生身上才採用（9/10 已綁的那幾支手機不必重綁）——
    未綁的舊名一律忽略、當作沒有 cookie。con 有給就沿用呼叫端的連線查舊名有沒有綁；沒給
    才臨時開一條（呼叫端每次請求只想查一次時，把手上現成的連線傳進來）。"""
    ck = req.cookies.get(DK_COOKIE, "")
    if _valid_device_cookie(ck):
        return ck
    if DK_COOKIE == LEGACY_DK_COOKIE:
        return ""  # http 模式沒有新舊名之分，沒有就是沒有
    legacy = req.cookies.get(LEGACY_DK_COOKIE, "")
    if not _valid_device_cookie(legacy):
        return ""
    dh = hash_device(legacy)
    if con is not None:
        bound = con.execute("SELECT 1 FROM students WHERE device_hash=? LIMIT 1", (dh,)).fetchone() is not None
    else:
        with db() as c2:
            bound = c2.execute("SELECT 1 FROM students WHERE device_hash=? LIMIT 1", (dh,)).fetchone() is not None
    return legacy if bound else ""


def ensure_device_cookie(req: Request, resp):
    """學生頁面第一次載入就下發裝置識別（C-S7：或把已綁定的舊名裝置補發成新名）。"""
    existing = effective_device_key(req)
    if existing:
        return _migrate_legacy_cookie(req, resp, existing)
    return set_device_cookie(resp, secrets.token_urlsafe(24))


def ensure_device_cookie_value(req: Request, resp, device_key: str):
    """同 ensure_device_cookie，但沿用呼叫端已經決定好的 device_key（C-S5：render_student 要讓
    grant 裡塞的裝置雜湊跟這裡下發的 cookie 是同一個值，不能各自各生一個隨機值）。"""
    existing = effective_device_key(req)
    if existing:
        return _migrate_legacy_cookie(req, resp, existing)
    return set_device_cookie(resp, device_key)


@app.post("/api/whoami")
def api_whoami(req: Request, course_id: int = Form(..., ge=1, le=2**53), device_key: str = Form("", max_length=200)):
    device_key = effective_device_key(req)
    if not device_key or _throttle(_checkin_hits, "who:" + device_key, 30, 60):
        return {"known": False}
    with db() as con:
        rs = con.execute("SELECT student_id, name FROM students WHERE course_id=? AND device_hash=?",
                         (course_id, hash_device(device_key))).fetchall()
    r = rs[0] if len(rs) == 1 else None  # 同一支手機綁到兩個學號（歷史資料）就不猜
    return {"known": bool(r), "student_id": r["student_id"] if r else "", "name": r["name"] if r else ""}


_beacon_hits: dict[str, list[float]] = {}


@app.post("/api/beacon", status_code=204)
def api_beacon(req: Request, kind: str = Form(""), msg: str = Form("", max_length=2000), sid: str = Form("", max_length=32)):
    """前端錯誤回報（B004/B026）：navigator.sendBeacon 打這支，故意不回錯誤內容、一律 204，
    不得因為回報失敗又讓學生端多一次例外。超過限流就靜默丟掉，不寫事件也不噴錯。"""
    if kind not in ("jserror", "slow"):
        return Response(status_code=204)
    device_key = effective_device_key(req)
    ip = client_ip(req) or "noip"
    if device_key and _throttle(_beacon_hits, "d:" + device_key, 6, 60):
        return Response(status_code=204)
    if _throttle(_beacon_hits, "ip:" + ip, 60, 60):
        return Response(status_code=204)
    device8 = hash_device(device_key)[:8] if device_key else ""
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    sid_int = int(sid) if sid.isdigit() else None
    reason = f"{kind}:{msg}"[:200]
    log_event(sid_int, "client", reason=reason, device8=device8, ua_class=ua_class)
    return Response(status_code=204)


class Reject(HTTPException):
    """簽到被拒（B004/B026）：多帶一個 slug 給事件記錄用，回應內容與狀態碼跟原本的 HTTPException 完全一樣，
    一個位元組都不變——slug 只在 api_checkin 內部攔截記錄時讀，不會出現在回應裡。"""

    def __init__(self, status_code: int, detail: str, slug: str):
        super().__init__(status_code=status_code, detail=detail)
        self.slug = slug


def _grant_sid_hint(grant: str) -> int | None:
    """最佳努力從 grant 字串猜場次 id，只給事件記錄用（例如簽章過期時仍想知道是哪一場）；
    正式驗證完全靠 read_token，這裡猜錯也不影響任何業務邏輯。"""
    try:
        parts = grant.split(":")
        if len(parts) >= 2 and parts[0] == "g":
            return int(parts[1])
    except (ValueError, IndexError):
        return None
    return None


@app.post("/api/checkin")
def api_checkin(req: Request, grant: str = Form(..., max_length=200), device_key: str = Form("", max_length=200),
                student_id: str = Form(..., max_length=32), name: str = Form("", max_length=64), pin: str = Form("", max_length=16),
                pin2: str = Form("", max_length=16), seat: str = Form("", max_length=8), hints: str = Form("", max_length=300)):
    # 薄殼：主邏輯在 do_checkin。mutmut 3 會跳過帶裝飾器的函式，留在這裡就永遠進不了突變測試
    return do_checkin(req, grant, student_id, name, pin, pin2, seat, hints)


def do_checkin(req: Request, grant: str, student_id: str, name: str, pin: str, pin2: str, seat: str, hints: str):
    device_key = effective_device_key(req)
    ip = client_ip(req)
    dh = hash_device(device_key) if device_key else ""
    device8 = dh[:8]
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    sid = _grant_sid_hint(grant)
    stu_pk: int | None = None

    def _ok(status: str, con: sqlite3.Connection | None = None) -> None:
        """con 有給就跟主流程共用同一條連線、同一次 commit——早退路徑此時 con 還握著沒 commit 的寫入，
        另開一條連線會跟它互鎖等到 busy timeout（2026-09-21 event log 造成 database is locked 的回報）。
        con 沒給（with 區塊已經跑完、con 已關閉）才用獨立連線，這種情況不會撞鎖。"""
        if con is not None:
            log_event_on(con, sid, "checkin_ok", reason=status, device8=device8, ua_class=ua_class, student_pk=stu_pk)
        else:
            log_event(sid, "checkin_ok", reason=status, device8=device8, ua_class=ua_class, student_pk=stu_pk)

    try:
        # C-S5：g, sid, nonce, issued, dh16（四段式與更舊的三段式憑證都沒有裝置雜湊那一段，一律視為無效不相容）
        parts = read_token(grant, 5)
        if not parts or parts[0] != "g":
            raise Reject(410, "填表時間已到，請重新掃描 QR", "grant_expired")
        sid = int(parts[1])
        try:
            issued = float(parts[3])
        except ValueError:
            raise Reject(410, "填表時間已到，請重新掃描 QR", "grant_expired") from None
        grant_dh16 = parts[4]
        if not device_key:
            raise Reject(400, "瀏覽器沒有保存簽到識別（無痕模式或封鎖 cookie），請改用一般模式重新掃描 QR", "no_cookie")
        # C-S5：憑證下發時就綁裝置，這是第一層防護——比 grants 表在「第一次使用」才記錄快一步，
        # 擋住教室內把 GRANT 字串轉貼給場外的人這種攻擊（那份表單本來就是這支裝置掃到的）
        if not hmac.compare_digest(grant_dh16, dh[:16]):
            raise Reject(410, "這份簽到表單是別支手機掃到的，請自己重新掃描 QR", "grant_other_device")
        seat = seat.strip()[:8]  # 學生沒有固定座位，此欄保留但不強制
        # A2：PIN 格式檢查搬到「確定需要 PIN」之後（已綁裝置的裝置 cookie 對上時完全不驗 PIN）——
        # 這裡不能再無條件擋格式，得先知道是不是同一支已綁裝置才能判斷要不要驗。
        fp = fingerprint(req, hints)
        # 一支手機一分鐘最多 12 次：擋亂試 PIN，開資料庫之前先擋
        if _throttle(_checkin_hits, "d:" + dh, 12, 60):
            raise Reject(429, "送太多次了，等一分鐘再試", "throttle")
        with db() as con:
            # C-S3：每出口 IP 一分鐘 240 次只算未綁裝置——已綁裝置的回簽不因整班同一個校園 NAT
            # 被攻擊者灌爆而連坐擋下；查詢放在 BEGIN IMMEDIATE 之前，不佔寫入鎖
            bound_elsewhere = con.execute("SELECT 1 FROM students WHERE device_hash=? LIMIT 1", (dh,)).fetchone() is not None
            if not bound_elsewhere and _throttle(_checkin_hits, "ip:" + ip, 240, 60):
                raise Reject(429, "送太多次了，等一分鐘再試", "throttle")
            # grant 第一次用就綁定那支手機：掃到的表單貼給別人，別支手機用不了（第二層防護，見上面
            # 第五段的第一層）——這段仍在 BEGIN IMMEDIATE 之前，它自己的 commit 落地就算數
            gsig = grant.rsplit(":", 1)[-1]
            g = con.execute("SELECT device_hash FROM grants WHERE sig=?", (gsig,)).fetchone()
            if g and g["device_hash"] != dh:
                raise Reject(410, "這份簽到表單已經在別支手機用過，請自己重新掃描 QR", "grant_other_device")
            if not g:
                con.execute("INSERT OR IGNORE INTO grants(sig, device_hash, created) VALUES(?,?,?)", (gsig, dh, now()))
                con.execute("DELETE FROM grants WHERE created<?", (now() - 3600,))
                con.commit()
                g2 = con.execute("SELECT device_hash FROM grants WHERE sig=?", (gsig,)).fetchone()  # 回讀：兩支手機同時送，只有先寫進去的那支算
                if g2 and g2["device_hash"] != dh:
                    raise Reject(410, "這份簽到表單已經在別支手機用過，請自己重新掃描 QR", "grant_other_device")
            # C-S4：讀後寫放進同一個寫入交易——grant 處理（含它自己的 commit）之後、讀 sessions／
            # students／other／same 之前才 BEGIN IMMEDIATE，其後的讀與寫全程在這個交易鎖裡，
            # 兩個執行緒不會都讀到「這支裝置還沒被別人綁走」的舊結果（2026-09-21 bug-hunter t2/t2b 重現）
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise Reject(410, "老師已取消這次點名，等重新開始再掃", "closed")
            s = get_session(con, sid)
            c = get_course(con, s["course_id"])
            st = course_settings(c)
            phase = session_phase(s)
            if phase == "closed":
                raise Reject(410, "點名已結束", "closed")
            stu = con.execute("SELECT * FROM students WHERE course_id=? AND student_id=?",
                              (c["id"], to_halfwidth(student_id).strip().upper())).fetchone()  # B019：與前端同一套全形→半形規則
            NOT_MATCH = f"學號或姓名與【{c['name']}】的名冊不符。若你不是這門課的學生，代表六位數輸到別班的，請回上一頁重輸；剛加選的請找老師"
            if not stu:
                raise Reject(400, NOT_MATCH, "not_match")  # 與姓名不符同一句：外人猜不出哪些學號存在
            stu_pk = stu["id"]  # B004：student_pk 只有從名冊查到人才填，這裡起就填得到
            pf = con.execute("SELECT n FROM pin_fail WHERE session_id=? AND student_pk=? AND device_hash=?", (sid, stu["id"], dh)).fetchone()
            pf_all = con.execute("SELECT n FROM pin_fail WHERE session_id=? AND student_pk=? AND device_hash=''", (sid, stu["id"])).fetchone()
            # A2/B044：本人已綁的那支手機平日不驗 PIN，PIN 鎖定也就不該擋到它——
            # 否則別人從別支裝置亂猜到全場上限，就能讓本人這堂課簽不了到
            same_device_as_bound = stu["device_hash"] is not None and hmac.compare_digest(stu["device_hash"], dh)
            if not same_device_as_bound and (
                    (pf and pf["n"] >= st["pin_max_fail"]) or (pf_all and pf_all["n"] >= PIN_MAX_FAIL_TOTAL)):
                raise Reject(423, "PIN 錯太多次，本次請找老師手動簽到", "pin_locked")
            flags: list[str] = []
            first_time = stu["pin_hash"] is None
            # B030：不論課程的 allow_bind 設定，綁定場次一律允許首次綁定
            if (first_time or stu["device_hash"] is None) and not st["allow_bind"] and s["kind"] != "bind":
                raise Reject(403, "這門課目前不開放首次綁定手機，請找老師當場打開後再簽", "bind_closed")
            # B060（使用者 2026-09-21 裁定）：綁定場次抽點判不在、綁定被撤回（C-S2）之後，這個學號本場不能再綁——
            # 否則代綁者同一支手機馬上重掃就綁回去，而這個學號本場已抽過、不會再被抽到。看學號不看裝置，
            # 換 cookie 繞不過；下一場照常可綁（老師按錯「不在」的代價＝那位學生下次上課再綁）。
            if s["kind"] == "bind" and stu["device_hash"] is None and con.execute(
                    "SELECT 1 FROM spotchecks WHERE session_id=? AND student_pk=? AND result='absent' LIMIT 1",
                    (sid, stu["id"])).fetchone():
                raise Reject(403, "老師剛才叫名時這個學號不在場，本場不能再綁定。人在教室請直接找老師，下次上課再綁", "bind_revoked")
            # B022：這支手機是不是已經是別人的、或本場已經簽過別的學號，要在「要不要寫 PIN、要不要綁裝置」
            # 之前就先知道——首次綁定被這兩種情況擋下時，兩者都不能做（不然代簽者的自訂 PIN 會側寫進被代簽者帳號）
            other = con.execute("SELECT id FROM students WHERE device_hash=? AND id!=? AND student_id!=?",
                                (dh, stu["id"], stu["student_id"])).fetchone()  # 跨課也算：這支手機已是別人的（同一人修兩門課不算）
            same = con.execute("SELECT id, flags FROM checkins WHERE session_id=? AND device_hash=? AND student_pk!=?",
                               (sid, dh, stu["id"])).fetchone()
            if first_time:
                if not (pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 6):
                    raise Reject(400, "PIN 要 4 到 6 位數字", "pin_format")
                name_match = name_matches(name, stu["name"])  # A4/B018：完全相同或「字數相同且第一個字相同」都放行
                if not name_match:
                    raise Reject(400, NOT_MATCH, "not_match")
                if pin != pin2:
                    raise Reject(400, "兩次 PIN 不一樣", "pin_mismatch2")
                if not (other or same):  # B022：手機是別人的、或本場已簽過別的學號，不寫 PIN、也不綁裝置
                    salt = secrets.token_hex(8)
                    con.execute("UPDATE students SET pin_hash=?, pin_salt=? WHERE id=?", (hash_pin(pin, salt), salt, stu["id"]))
                flags.append("late_bind")
                if name_match == "variant":
                    flags.append("name_variant")
            else:
                # A2/題三 A：送出請求的裝置 cookie 雜湊＝該學號已綁的 device_hash 時，平日回簽不驗 PIN——
                # 不要求格式、不比對、多餘的 pin 一律忽略、不計失敗次數。其餘情況（換手機、解綁後、
                # 手機是別人的）PIN 規則不變。
                if not same_device_as_bound:
                    if not (pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 6):
                        raise Reject(400, "PIN 要 4 到 6 位數字", "pin_format")
                    if not hmac.compare_digest(hash_pin(pin, stu["pin_salt"]), stu["pin_hash"]):
                        n = (pf["n"] if pf else 0) + 1
                        for key in (dh, ""):  # 每裝置計一次，本場該生總計也計一次（換裝置識別重試也會累加）
                            con.execute("INSERT INTO pin_fail(session_id, student_pk, device_hash, n) VALUES(?,?,?,1) "
                                        "ON CONFLICT(session_id, student_pk, device_hash) DO UPDATE SET n=n+1", (sid, stu["id"], key))
                        con.commit()  # 例外會略過 context manager 的 commit，失敗次數要先落地；events 走獨立連線，不受這裡影響
                        if pin2:  # B016：前端以為是首次（帶了第二個 PIN 欄），但這學號其實已經設過 PIN
                            raise Reject(401, "這個學號已經設過 PIN，請輸入原本的 PIN；忘記了請找老師", "pin_exists")
                        left = st["pin_max_fail"] - n
                        raise Reject(401, f"PIN 錯誤，剩 {left} 次。忘記了請找老師重設" if left > 0 else "PIN 錯太多次，本次請找老師手動簽到", "pin_wrong")
                    con.execute("DELETE FROM pin_fail WHERE session_id=? AND student_pk=? AND device_hash IN (?, '')", (sid, stu["id"], dh))
            # 裝置綁定放在回收據之前：老師重設後學生重掃，PIN 與手機要一起綁好
            # 這支手機已經是別人的就不綁（否則代簽者的手機會認成被代簽者，還能看他的自查）
            if stu["device_hash"] is None and not other and not same:
                con.execute("UPDATE students SET device_hash=?, bound_at=? WHERE id=?", (dh, now(), stu["id"]))
            # PIN 對了才告訴他「這個學號綁在別支手機」：沒過 PIN 的人看不出名冊裡有誰
            if stu["device_hash"] is not None and stu["device_hash"] != dh:
                raise Reject(409, "這個學號已綁定另一支手機。換了手機、或瀏覽器清過資料，都請找老師解除綁定後再簽", "bound_other_device")
            # PIN 與裝置都驗過了，才回「已簽過」的收據
            already = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?", (sid, stu["id"])).fetchone()
            if already and already["source"] != "auto":
                af = json.loads(already["flags"])
                add = []
                if already["source"] == "manual" and "qr_after_manual" not in af:  # 老師先記了狀態，學生之後才掃到：讓老師看得到
                    add.append("qr_after_manual")
                # B022/B037：這一列不是這次寫的（已存在），但這次的裝置本身仍可能撞衝突——照樣標旗給老師看，
                # 但「狀態不改」：老師手動設的狀態是既定不變式，不能因為別人拿別的手機來簽而被動掉
                if other and "device_bound_other" not in af:
                    add.append("device_bound_other")
                if same and "same_device" not in af:
                    add.append("same_device")
                if add:
                    con.execute("UPDATE checkins SET flags=? WHERE id=?", (json.dumps(af + add), already["id"]))
                _ok(already["status"], con)
                return with_device_cookie(req, {"ok": True, "again": True, "name": stu["name"], "student_id": stu["student_id"],
                                           "ts": fmt(already["ts"]), "seat": already["seat"], "status": STATUS_LABEL[already["status"]],
                                           "kind": s["kind"]}, device_key)
            if other:
                flags.append("device_bound_other")
            if same:
                flags.append("same_device")
                sf = json.loads(same["flags"])  # 先簽的那位也標、也轉待確認：兩個人都要老師當場看
                if "same_device" not in sf:
                    con.execute("UPDATE checkins SET flags=?, status=CASE WHEN status IN ('present','late') THEN 'pending' ELSE status END WHERE id=?",
                                (json.dumps(sf + ["same_device"]), same["id"]))
            if first_time:
                # BUGLOG B036：首綁潮期間 same_fp／bind_burst 幾乎人人亮紅，老師會學會忽略旗標。
                # 本場首次綁定滿 5 人之後，同型號手機同校園網路必撞指紋，兩個旗標都不再標（只在人少的正常時段才有偵測意義）。
                mass_bind = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND flags LIKE '%\"late_bind\"%'",
                                        (sid,)).fetchone()[0] >= 5
                # 同一來源（IP＋UA＋語言）兩分鐘內第三筆以上首次綁定：可能是一台機器在替人綁，也可能是同型號手機集體綁定，只標記給老師看
                burst = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND fp=? AND flags LIKE '%\"late_bind\"%' AND ABS(ts-?)<120",
                                    (sid, fp, now())).fetchone()[0]
                if burst >= 2 and not mass_bind:
                    flags.append("bind_burst")
                twin = con.execute(
                    "SELECT id FROM checkins WHERE session_id=? AND fp=? AND student_pk!=? AND source='qr' "
                    "AND flags LIKE '%\"late_bind\"%' AND ABS(ts-?)<90", (sid, fp, stu["id"], now())).fetchone()
                if twin and not mass_bind:
                    flags.append("same_fp")
                    tf = json.loads(con.execute("SELECT flags FROM checkins WHERE id=?", (twin["id"],)).fetchone()["flags"])
                    if "same_fp" not in tf:
                        con.execute("UPDATE checkins SET flags=? WHERE id=?", (json.dumps(tf + ["same_fp"]), twin["id"]))
            if st["ip_check"] and s["teacher_ip"] and ip and ip != s["teacher_ip"]:
                flags.append("ip_differs")
            # A3/B029：出席／遲到看「掃到」（grant 發證時間）是否落在開放窗內，不是送出表單的時間；
            # 用送出當下讀到的 open_until（延長過就是延長後的值）。
            status = "present" if issued <= s["open_until"] else "late"
            if status == "late":
                flags.append("was_late")  # 待確認被判在場時，照簽到當下的窗口還原，不受之後延長影響
            if "same_device" in flags or "device_bound_other" in flags or "name_variant" in flags:
                status = "pending"  # C-S1：姓名寫法不同的首綁跟 same_device 同一套待確認機制
            t = now()
            if already:  # 防禦：若有 auto 缺席列殘留（正常流程結案後就 410，走不到這裡）
                con.execute("UPDATE checkins SET ts=?, seat=?, status=?, flags=?, ip=?, device_hash=?, fp=?, source='qr' WHERE id=?",
                            (t, seat, status, json.dumps(flags), ip, dh, fp, already["id"]))
            else:
                try:
                    con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, ip, device_hash, fp, source) "
                                "VALUES(?,?,?,?,?,?,?,?,?,'qr')", (sid, stu["id"], t, seat, status, json.dumps(flags), ip, dh, fp))
                except sqlite3.IntegrityError:  # 手機重複送出、網路重試：同一筆已經在了
                    dup = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?", (sid, stu["id"])).fetchone()
                    _ok(dup["status"], con)
                    return with_device_cookie(req, {"ok": True, "again": True, "name": stu["name"], "student_id": stu["student_id"],
                                               "ts": fmt(dup["ts"]), "seat": dup["seat"], "status": STATUS_LABEL[dup["status"]],
                                               "kind": s["kind"]}, device_key)
        _ok(status)
        return with_device_cookie(req, {"ok": True, "again": False, "name": stu["name"], "student_id": stu["student_id"], "ts": fmt(t),
                                   "seat": seat, "status": STATUS_LABEL[status], "first_time": first_time, "kind": s["kind"]}, device_key)
    except Reject as exc:
        log_event(sid, "checkin_reject", reason=exc.slug, device8=device8, ua_class=ua_class, student_pk=stu_pk)
        raise


@app.middleware("http")
async def no_store(req: Request, call_next):
    """所有頁面與 API 都是個人化內容，禁止 Cloudflare 與瀏覽器快取。"""
    resp = await call_next(req)
    if req.url.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=604800")
    else:
        resp.headers.setdefault("Cache-Control", "private, no-store")
    return resp


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    with db() as con:
        con.execute("SELECT 1")
    return "ok"


def _err_page(req: Request, status: int, detail: str):
    if req.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": detail}, status_code=status)
    if status == 401 and req.url.path.startswith("/t"):
        return RedirectResponse(f"/t/login?next={req.url.path}", 303)
    if status == 404:
        detail = "沒有這個頁面"
    return templates.TemplateResponse(req, "error.html", {"status": status, "detail": detail}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def http_exc(req: Request, exc: StarletteHTTPException):
    return _err_page(req, exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_exc(req: Request, exc: RequestValidationError):
    return _err_page(req, 400, "網址或表單內容不對")


@app.get("/api/ip", response_class=PlainTextResponse)
def api_ip(req: Request):
    """診斷用：系統看到的你的 IP（IP 比對標記就是拿這個比）。"""
    return client_ip(req)

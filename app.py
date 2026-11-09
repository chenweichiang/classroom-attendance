"""課堂點名系統

輪換 QR（HMAC 時間片）＋裝置綁定＋個人 PIN＋座位號抽點＋IP 比對。
單檔 FastAPI，SQLite 存放；老師端單一密碼登入。
"""
from __future__ import annotations

import collections
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
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, HTTPException, Path as PathParam, Request
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
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

# 裝置 cookie 改用 __Host- 前綴（瀏覽器規則：__Host- 必須同時 Secure、無 Domain、path=/，
# 擋掉子網域寫同名 cookie 蓋過本站的招式）。本機與測試走 http，瀏覽器不收沒有 Secure 的
# __Host- cookie，所以 http 下維持舊名，不然本機測試整套會拿不到 cookie。
DK_COOKIE = "__Host-attend_dk" if BASE_URL.startswith("https") else "attend_dk"
LEGACY_DK_COOKIE = "attend_dk"  # 只有雜湊已綁在某個學生身上的舊名才採用，未綁的一律當沒有 cookie

TZ = timezone(timedelta(hours=8))
DEFAULT_SETTINGS = {
    "open_minutes": 3,      # 開放簽到（出席）
    "late_minutes": 15,     # 遲到上限（自開始起算）
    "qr_interval": 10,      # QR 輪換秒數
    "qr_grace": 30,         # 過期容許秒數
    "spot_n": 3,            # 抽點人數
    "ip_check": True,       # 比對學生與投影頁公網 IP（只標記）
    "grant_seconds": 180,   # 掃碼後填表時限
    "pin_max_fail": 3,
    "allow_bind": True,     # 允許首次綁定（第二週後可關，新綁定要老師打開）
    "discord_channel": "",  # 結束點名時把缺席與遲到名單發到這個頻道（空＝不發）
    "bind_minutes": 10,     # 綁定場次的開放分鐘數（無遲到區，open_until=late_until）
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
    "name_variant": "姓名寫法不同",  # 字數相同且第一個字相同，放行但標旗給老師看
    "rebind": "換瀏覽器改綁",  # 已綁定的學號在另一個瀏覽器打對 PIN，自動改綁
}
SESSION_KIND_LABEL = {"normal": "", "bind": "綁定場次"}
KIND_DISPLAY = {"normal": "一般點名", "bind": "綁定場次"}  # 老師頁錯誤訊息用的完整說法（含「一般」二字）
# 計入出缺勤的場次＝一般場次（非綁定）且未作廢；報表／CSV／`/api/me` 出席率／attendance_rate／
# 老師首頁統計都要走這個共用定義，不要各處各寫一份判準。
COUNTED_SESSION_SQL = "kind='normal' AND voided_at IS NULL"
REJECT_REASON_LABEL = {  # 老師頁即時漏斗與「卡住名單」用的中文說明
    "pin_format": "PIN 格式", "no_cookie": "沒有裝置識別", "throttle": "太頻繁",
    "grant_expired": "填表逾時", "grant_other_device": "別支手機的表單", "closed": "點名已結束",
    "not_match": "學號或姓名不符", "pin_locked": "PIN 鎖定", "bind_closed": "不開放首次綁定",
    "bind_revoked": "判不在後又想綁",
    "pin_mismatch2": "兩次 PIN 不同", "pin_wrong": "PIN 錯誤", "pin_exists": "已設過 PIN",
    "already_recorded": "本場已有紀錄（打對 PIN）",  # 本場已有這個學號的非 auto 紀錄時的原因碼
    # 下面這個碼只會出現在舊事件裡，現在不再產生；留著只為了讀得懂舊紀錄
    "bound_other_device": "PIN 對但綁在別支（10/17 以前的舊紀錄）",
}
# 一般點名結束後老師馬上開「只做綁定」，舊表單／舊分頁看到的說明。
# templates/student.html 的 410 分支用同一句字串比對（markClosed(false)），兩邊必須完全一致。
BIND_ONLY_CLOSED_MSG = "這一場已經結束。老師現在開的是只做綁定的場次，不算今天的出缺勤；有到課但沒簽到成功的，請直接找老師補登。"
# 鎖定與「剩 0 次」三句共用；老師手動記完，學生的 PIN 仍然不知道，下次換環境又卡
PIN_LOCKED_MSG = "PIN 錯太多次，這一場請找老師手動簽到。忘了 PIN 的話，請老師在名冊頁按「重設」，重設後重新掃 QR 設一組新的。"
EXTEND_CLOSED_MSG = ("這次點名已經結束，不能延長。晚到的人請在名單上改狀態，或用點名頁的「手動簽到」補登；"
                     "不要另開一場（另開的那一場，準時簽到的人會被記缺席）。")
DISCORD_AUTO_MIN_RATE = 0.5  # 自動結案時出席+遲到+待確認不到應到人數一半就不自動發 Discord

mimetypes.add_type("font/woff2", ".woff2")  # slim 映像的 mimetypes 沒有 woff2
app = FastAPI(title="attend", docs_url=None, redoc_url=None,
              openapi_url="/openapi.json" if os.environ.get("ATTEND_OPENAPI") else None)  # 只有測試時開，給 schemathesis 用
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# 字型網址帶內容雜湊。靜態檔快取七天（Cloudflare 邊緣與瀏覽器），重做子集後網址不變的話，新字要等七天才看得到
_FONT = BASE / "static" / "fonts" / "ZhuqueFangsong-subset.woff2"
FONT_V = hashlib.sha256(_FONT.read_bytes()).hexdigest()[:10] if _FONT.is_file() else "0"
APP_VERSION = "1.1.0"  # 發版時跟 CHANGELOG.md 最上面那一版一起改（tests/test_version.py 會比對）
templates.env.globals.update(STATUS_LABEL=STATUS_LABEL, FLAG_LABEL=FLAG_LABEL, REJECT_REASON_LABEL=REJECT_REASON_LABEL,
                             APP_VERSION=APP_VERSION, BASE_HOST=BASE_URL.split("://", 1)[-1], FONT_V=FONT_V,
                             pct=lambda r: pct(r))  # noqa: PLW0108 pct 定義在下面，這裡直接傳函式名會撞 forward reference NameError，lambda 是刻意的延遲查找


# 工具

def now() -> float:
    return time.time()


def today_start(ts: float | None = None) -> float:
    """台灣時間（TZ）當日 00:00 的 epoch 秒——「今天不能再綁」看的是這條線，不是任何一場的開場時間。"""
    d = datetime.fromtimestamp(ts if ts is not None else now(), TZ)
    return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()


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


def read_token_ignore_expiry(token: str, nparts: int) -> list[str] | None:
    """驗簽章、但不驗時效——只給「已經簽過的人重送」這條路徑判斷 sid／裝置雜湊用，
    讀出的欄位不能單獨當作合法憑證使用；核心業務邏輯（要不要收這次簽到）仍全部走 read_token。"""
    try:
        *parts, exp, sig = token.split(":")
    except ValueError:
        return None
    if len(parts) != nparts:
        return None
    body = ":".join(parts + [exp])
    if not hmac.compare_digest(sign(body), sig):
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
    cf = req.headers.get("cf-connecting-ip")  # 不信 X-Real-IP：Caddy 的 {client_ip} 在此設定下可被 XFF 第一段偽造
    if cf and _valid_ip(cf):
        return _valid_ip(cf)
    xff = req.headers.get("x-forwarded-for", "")
    for part in reversed(xff.split(",")):
        if _valid_ip(part):
            return _valid_ip(part)
    return (_valid_ip(req.client.host) if req.client else "") or ""


def rate_ip(req: Request) -> str:
    """限流專用的鍵：IPv6 一律以 /64 為單位（同一個家用或行動網路用戶端拿得到整段 /64，換位址就能繞過每 IP 限額）。
    client_ip 與寫進資料庫的 IP 不變。"""
    ip = client_ip(req)
    if not ip:
        return "noip"
    a = ipaddress.ip_address(ip)
    if a.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return ip


def fingerprint(req: Request, hints: str = "") -> str:
    """伺服器端指紋：只用伺服器看得到的 IP＋UA＋語言（前端自報的 hints 可被任意變造，不納入偵測與限流）。"""
    raw = "|".join([client_ip(req) or "noip", req.headers.get("user-agent", ""), req.headers.get("accept-language", "")])
    return hmac.new(K_FP, raw.encode(), hashlib.sha256).hexdigest()[:32]


def classify_ua(ua: str) -> str:
    """User-Agent 只留裝置類別給事件記錄，不存完整字串、不存 IP、不存姓名。"""
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


_NAME_DOTS = ("\u00b7", "\u2027", "\u30fb", "\u2022", ".")  # · ‧ ・ • .（NFKC 後 ・ 仍是 U+30FB）
_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\ufeff")  # 零寬空白／不斷連字／連字／BOM：手機 IME 常夾帶


def norm_name(s: str) -> str:
    """姓名比對前的正規化：NFKC 收斂全形英數與相容漢字變體，再去零寬字元與空白。
    刻意不做異體字對照表（温/溫、峰/峯）——那張表要哪些字、誰維護，屬於待老師裁定的題目，不在這裡決定。
    去空白之後再 casefold（英文姓名不分大小寫），並去掉間隔號（· ‧ ・ • 與 .）——外籍生、原住民族姓名名冊常帶，手機打不出來。"""
    s = unicodedata.normalize("NFKC", s)
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, "")
    s = "".join(s.split()).casefold()
    for ch in _NAME_DOTS:
        s = s.replace(ch, "")
    return s


def name_matches(given: str, roster: str) -> str:
    """姓名寬鬆比對：norm_name 後完全相同→'exact'；不同但字數相同且第一個字相同→'variant'
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
    """全形英數符號（含全形空白）轉半形，不動中文字。學號前後端要用同一套規則，
    否則前端濾掉、後端沒轉，兩邊看到的「合法字元」不一樣。"""
    return s.translate(_FULLWIDTH_ASCII)


def normalize_student_id(raw: str) -> str:
    """學號查名冊與匯入名冊共用的正規化——去零寬字元（手機 IME 與試算表常夾帶）、全形轉半形、strip、轉大寫。
    簽到、/api/me 等所有「拿使用者輸入的學號去查 students」的地方都走這一支，manage.py 匯入也呼叫它，兩端不能各寫一套。"""
    s = raw
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, "")
    return to_halfwidth(s).strip().upper()


# 資料庫

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
-- kind（normal/bind）與 voided_at 一律靠下面的 ALTER 遷移補上（沿用 fp／created_at 的既有寫法），
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


def connect(timeout: float = 10) -> sqlite3.Connection:
    """timeout：busy timeout 秒數。預設 10 秒給主流程（簽到／老師端等真正的業務寫入）；
    log_event 這種旁路診斷記錄要用短很多的逾時（見 log_event 內部呼叫），不能讓事件記錄
    陪著等到主流程的 10 秒。"""
    con = sqlite3.connect(DB_PATH, timeout=timeout)
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


_event_backlog: collections.deque = collections.deque(maxlen=500)
_event_backlog_lock = threading.Lock()

# SQLite 的 INTEGER 欄位是 64 位元有號整數，超出這個範圍寫入會直接丟
# `OverflowError`，不是「拿不到鎖」那種暫時性失敗。_grant_sid_hint／`/api/beacon` 的 sid 都是
# 從使用者可控的字串直接 int() 轉來，沒有上限——偽造一個場次 id 30 個 9 就能做到。
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _event_row(session_id: int | None, kind: str, reason: str = "", device8: str = "",
              ua_class: str = "", student_pk: int | None = None) -> tuple:
    """組出要寫進 events 的一筆（含 `now()`），順便洗掉會讓 INSERT 本身丟例外的壞資料：
    `session_id`／`student_pk` 不是 int，或超出 SQLite INTEGER 範圍（-2**63～2**63-1），
    一律改成 None——寧可事件缺一個外鍵般的參照，也不能讓這筆事件本身變成寫不進去的壞資料。
    字串欄位一律用 `str(x).encode("utf-8","replace").decode("utf-8")` 洗過，擋掉任何無法編碼
    的字元（同樣的道理：診斷事件不能因為內容本身就寫不進去）。"""
    def _safe_id(x):
        return x if isinstance(x, int) and not isinstance(x, bool) and _SQLITE_INT_MIN <= x <= _SQLITE_INT_MAX else None

    def _safe_str(x):
        return str(x).encode("utf-8", "replace").decode("utf-8")

    return (now(), _safe_id(session_id), _safe_str(kind), _safe_str(reason),
            _safe_str(device8), _safe_str(ua_class), _safe_id(student_pk))


def log_event(session_id: int | None, kind: str, reason: str = "", device8: str = "",
              ua_class: str = "", student_pk: int | None = None) -> None:
    """事件記錄：獨立連線、獨立 commit。
    🔴 只能在呼叫端目前沒有握著另一條還沒 commit 的寫入交易時呼叫——SQLite 一次只容許一個寫入者，
    另開連線硬寫會跟還沒 commit 的那條互鎖，等到 busy timeout 才炸「database is locked」
    （例如 api_checkin 在 with db() as con 內、con 還沒 commit 前呼叫這支，會讓簽到請求變慢甚至等到逾時；
    那幾個路徑改呼叫 log_event_on(con, ...) 共用同一條連線）。
    寫入失敗不得影響主流程，只印到 stderr（不拋例外）。
    連線逾時用 0.5 秒（不是主流程共用的 10 秒）——事件是診斷用，資料庫真的被鎖住時，
    落地頁／簽到只呼叫這支來記事件，不能讓學生的請求陪著等到 10 秒的 busy timeout；
    最多多等 0.5 秒就放棄，事件可能沒寫到，但不能拖累主流程。
    只縮短逾時還不夠：這會讓「資料庫真的被鎖住的那段時間」永遠寫不進事件——鎖住的正是同一個
    檔案，0.5 秒等不到鎖放開就放棄，`do_checkin` 的 `except Exception` 想補記的 checkin_error
    因此也跟著遺失，跟原本要解決的問題（漏斗誤判成落地未送出）矛盾。所以失敗時把這一筆（連同
    失敗當下的 `now()`，不是之後補寫的時間）放進模組層 `_event_backlog`（deque，上限 500 筆，
    單一 worker 前提下多執行緒可能同時失敗，用鎖保護但鎖內只做記憶體操作、不做任何 I/O）；
    下一次任何 `log_event` 呼叫拿到鎖成功寫入時，先把 backlog 裡積壓的事件跟這一筆一起用同一次
    交易補寫、一次 commit（不可遺失、不可重複——backlog 只在寫入成功後才清空，寫入失敗會連同
    這一筆一起放回去）。行程重啟會遺失還沒補寫的事件（事件是診斷用，可接受）。
    「失敗就整批放回 backlog」不能一概而論：寫入失敗如果是因為
    這批資料本身有問題（例如 `session_id` 超出 SQLite INTEGER 範圍丟 `OverflowError`，不是
    「暫時拿不到鎖」），放回去只會讓同一批壞資料在下一次呼叫繼續失敗、繼續放回去，形同卡死：
    之後所有事件都會被迫跟這筆壞資料一起 executemany、一起失敗、一起放回去，永遠寫不進去。
    所以只有 `sqlite3.OperationalError`（鎖住、忙碌、I/O 這類暫時性失敗）才放回 backlog；
    其他任何例外一律把這批（pending 加這一筆）整批丟棄，只印 stderr 說明丟了幾筆與例外型別——
    寧可少幾筆診斷事件，也不能讓一筆壞資料癱瘓後面所有正常事件。真正的防線在 `_event_row`：
    先把已知會讓 SQLite 炸掉的壞值洗成 None／安全字串，這裡的丟棄分支是最後一道保險。"""
    row = _event_row(session_id, kind, reason, device8, ua_class, student_pk)
    with _event_backlog_lock:
        pending = list(_event_backlog)
        _event_backlog.clear()
    try:
        con = connect(timeout=0.5)
        try:
            con.executemany(
                "INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
                pending + [row])
            con.commit()
        finally:
            con.close()
    except sqlite3.OperationalError as e:  # 鎖住／忙碌／I/O：暫時性，值得放回去等下次補寫
        print(f"[events] log_event 失敗（kind={kind}）：{e}", file=sys.stderr)
        with _event_backlog_lock:
            _event_backlog.extendleft(reversed(pending))
            _event_backlog.append(row)
    except Exception as e:  # noqa: BLE001 資料本身有問題（例如 OverflowError），放回去只會
        # 卡死後面所有事件——整批丟棄，不留在 backlog 裡繼續毒害下一次呼叫。
        print(f"[events] log_event 失敗（kind={kind}）：{e}；因為不是暫時性失敗，"
              f"丟棄這批 {len(pending) + 1} 筆事件（不放回 backlog）", file=sys.stderr)


def log_event_on(con: sqlite3.Connection, session_id: int | None, kind: str, reason: str = "",
                 device8: str = "", ua_class: str = "", student_pk: int | None = None) -> None:
    """事件記錄，跟呼叫端目前握著的連線共用同一次交易、同一次 commit——用在呼叫端還沒 commit
    就要記錄成功事件的路徑（例如 api_checkin 的早退回應），避免跟 log_event() 的獨立連線互鎖。
    跟主流程同進退：主流程 commit 才落地，主流程萬一之後還是失敗回滾，這筆事件也跟著滾掉（可接受，
    因為這條路徑本來就是「主流程本身要不要成功」的一部分，不是旁路）。寫入失敗不得拋出，只印 stderr。
    跟 log_event 共用 `_event_row` 洗欄位——這支是跟呼叫端共用同一條連線、
    同一個交易，壞資料（例如超出 SQLite INTEGER 範圍的 `session_id`）在這裡丟例外會讓呼叫端整個
    請求跟著炸 500，比 log_event 那邊獨立連線失敗更嚴重，不能不洗就塞給 SQLite。"""
    try:
        con.execute(
            "INSERT INTO events(ts, session_id, kind, reason, device8, ua_class, student_pk) VALUES(?,?,?,?,?,?,?)",
            _event_row(session_id, kind, reason, device8, ua_class, student_pk))
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
        # 既有場次一律視為一般場次（正式站已有的資料照常計入出缺勤）
        _c.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal'")
    if "voided_at" not in [r["name"] for r in _c.execute("PRAGMA table_info(sessions)")]:
        _c.execute("ALTER TABLE sessions ADD COLUMN voided_at REAL")  # NULL＝有效，非 NULL＝已作廢


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
    s["qr_grace"] = max(0, min(s["qr_grace"], 120))  # 不再夾在 qr_interval 以內，上限放到 120 秒
    s["spot_n"] = max(1, s["spot_n"])
    s["grant_seconds"] = max(30, min(s["grant_seconds"], 900))  # grants 表一小時清除，憑證不該活得比它久
    s["pin_max_fail"] = max(1, s["pin_max_fail"])
    s["bind_minutes"] = max(3, min(s["bind_minutes"], 30))  # 3–30 分鐘
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
    """共用定義：計入出缺勤的場次＝一般場次（非綁定）且未作廢；已結案與否由呼叫端另外判斷。
    報表、CSV、`/api/me` 出席率、attendance_rate、老師首頁統計都要走這支，不要各處各寫一份判準。"""
    return s["kind"] == "normal" and s["voided_at"] is None


def close_session_rows(con, s: sqlite3.Row, closed_at: float) -> None:
    """結案共用：寫 closed_at、沒簽的記缺席（加選生不算）、清掉本場 PIN 失敗計數。
    綁定場次不寫自動缺席列（不計出缺勤，也就無所謂「沒簽的人缺席」）。"""
    con.execute("UPDATE sessions SET closed_at=? WHERE id=?", (closed_at, s["id"]))
    if s["kind"] == "normal":
        con.execute(
            "INSERT OR IGNORE INTO checkins(session_id, student_pk, ts, status, flags, source) "
            "SELECT ?, id, ?, 'absent', '[]', 'auto' FROM students WHERE course_id=? AND created_at<=?",
            (s["id"], closed_at, s["course_id"], s["opened_at"]))
    con.execute("DELETE FROM pin_fail WHERE session_id=?", (s["id"],))
    # 去重記憶體也要一起清——取消結束後 pin_fail 從零算，記憶體裡卻還留著結案前最後一次錯 PIN 的次數，
    # 60 秒內原樣重送同一組會沿用舊次數（「錯太多次」），其實沒鎖。先 list() 取鍵再刪（字典邊走邊改會丟 RuntimeError）。
    for key in [k for k in list(_pin_fail_recent) if k[0] == s["id"]]:
        _pin_fail_recent.pop(key, None)


def attendance_rate(con, s: sqlite3.Row) -> float | None:
    """出席＋遲到＋待確認 ÷（應到人數－請假）；應到排除加選前的 NA。
    不計入出缺勤的場次（綁定／已作廢）沒有出席率可言，一律回 None。"""
    if not counts_toward_attendance(s):
        return None
    total = con.execute("SELECT COUNT(*) FROM students WHERE course_id=? AND created_at<=?",
                        (s["course_id"], s["opened_at"])).fetchone()[0]
    # 分子與請假跟分母同一群——只算開場前就在名冊的人（開場後才加選的人不進分子）
    counts = {r["status"]: r["n"] for r in con.execute(
        "SELECT k.status, COUNT(*) n FROM checkins k JOIN students st ON st.id=k.student_pk "
        "WHERE k.session_id=? AND st.created_at<=? GROUP BY k.status", (s["id"], s["opened_at"])).fetchall()}
    ok = sum(counts.get(k, 0) for k in ("present", "late", "pending"))
    denom = total - counts.get("excused", 0)
    return ok / denom if denom > 0 else None


def should_hold_discord(con: sqlite3.Connection, s: sqlite3.Row, channel: str) -> bool:
    """出席＋遲到＋待確認不到應到人數一半就暫緩發 Discord——
    手動結束（api_close）與自動結案（finalize_expired）共用這一支判斷，兩處行為要一致；
    只有自動結案看是不夠的：投影壞掉、老師手動結束時，全班缺席名單會立刻公開且撤不回。
    沒有頻道，或這場本來就不計出缺勤（attendance_rate 回 None）都不算暫緩。
    老師在點名頁按了「這場先不要自動發 Discord」（`note=='discord_held_manual'`）時，不論出席率多高都要暫緩——這是老師主動的判斷，
    跟出席率無關；呼叫端要記得：這支只回「該不該暫緩」，不覆寫 note，是否要把 note
    改寫成 'discord_held' 由呼叫端自己判斷（見 finalize_expired／api_close 那兩處，
    已經是 'discord_held_manual' 就不能被蓋掉，否則暫緩原因就分不出是手動還是出席率低）。"""
    if not channel:
        return False
    if s["note"] == "discord_held_manual":
        return True
    rate = attendance_rate(con, s)
    return rate is not None and rate < DISCORD_AUTO_MIN_RATE


def finalize_expired(con) -> list[int]:
    """老師忘了按結束的場次：遲到窗過了就自動結案並把沒簽的記缺席，報表與自查才看得到；
    有設 Discord 且成功率過低（出席+遲到+待確認不到應到人數一半）就不自動發，
    改在 sessions.note 記 discord_held，老師頁提示改用「重發通知」手動確認發送。"""
    t = now()
    done = []
    pending_posts = []
    for s0 in con.execute("SELECT * FROM sessions WHERE closed_at IS NULL AND late_until<?", (t,)).fetchall():
        # 上面的 SELECT 在交易外，等到拿下寫入鎖時，老師可能剛延長（late_until 變大）或剛按「先不要自動發」。
        # UPDATE 再確認一次 late_until<t（延長過了就搶不到），closed_at 用資料庫裡「現在的」late_until；
        # 搶到之後在同一個交易重讀整列，下面的 note／暫緩判斷都用這份新的，不用 SELECT 時的舊列。
        won = con.execute("UPDATE sessions SET closed_at=late_until WHERE id=? AND closed_at IS NULL AND late_until<?",
                          (s0["id"], t)).rowcount
        if not won:  # 另一個輪詢先結了、或老師剛延長，通知交給它
            continue
        s = con.execute("SELECT * FROM sessions WHERE id=?", (s0["id"],)).fetchone()
        close_session_rows(con, s, s["closed_at"])
        done.append(s["id"])
        if s["kind"] != "normal":  # 綁定場次不計出缺勤，結案不發 Discord、也不記 discord_held
            continue
        channel, text = session_summary(con, s["id"])
        if should_hold_discord(con, s, channel):
            # note 已經是 'discord_held_manual'（老師自己按的暫緩）就不要蓋成 'discord_held'——
            # 蓋掉的話 api_state 的 #heldMsg 就分不出這場是出席率低還是老師自己按的，紅字講不出原因。
            if s["note"] != "discord_held_manual":
                con.execute("UPDATE sessions SET note='discord_held' WHERE id=?", (s["id"],))
        else:
            pending_posts.append((s["id"], channel, text))
    if done:
        con.commit()  # 先落地再發：呼叫端之後就算 404 回滾，也不會出現「訊息發了、資料沒結案」
        for psid, channel, text in pending_posts:
            if channel:
                threading.Thread(target=lambda i=psid, ch=channel, tx=text: _bg_discord_post(i, ch, tx), daemon=True).start()
    elif con.in_transaction:
        # 全部搶輸（另一個輪詢先結案）時，rowcount=0 的 UPDATE 仍讓 sqlite3 開了隱式交易；
        # 不收掉的話，do_open_session 緊接的 BEGIN IMMEDIATE 會炸「cannot start a transaction within a transaction」
        con.commit()
    return done


# 輪換碼

def slot_sig(session: sqlite3.Row, slot: int) -> str:
    return hmac.new(K_SLOT, f"{session['id']}:{session['secret']}:{slot}".encode(),
                    hashlib.sha256).hexdigest()[:10]


def code6(sig: str) -> str:
    return f"{int(sig, 16) % 1_000_000:06d}"


def current_slots(session: sqlite3.Row, settings: dict) -> list[int]:
    """回傳目前可接受的時間片：這一片，加上「該片結束後 grace 秒內」仍算數的前面幾片。
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
    # 靜區至少 4 模組（ISO/IEC 18004），border=1 在 1024×768 投影時解碼器可能找不到碼
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=20, border=4)
    return img.to_string(encoding="unicode")


# 老師登入

_login_fail: dict[str, list[float]] = {}
# `_throttle` 與這裡（登入失敗次數）共用的行為模式——走訪整個字典找出可以清掉的舊鍵、
# 再讀寫某一個 key 的清單——本身不是原子操作。單一 worker 但多執行緒（背景發 Discord、
# log_event 等都跑在自己的執行緒）時，一條執行緒還在走訪 `bucket.items()`，另一條同時插入
# 新鍵，會撞 `RuntimeError: dictionary changed size during iteration`。持鎖時間必須極短
# （只有字典操作、沒有任何 I/O），`time.sleep(2)`（登入錯太多次的拖慢）不可以在鎖內。
_throttle_lock = threading.Lock()


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


def _login_redirect_next(req: Request) -> tuple[str, bool]:
    """算出跳轉登入頁要帶的 next、以及要不要加 redo=1。GET 照舊用這次請求本身的路徑——
    都是老師頁面，登入後 GET 同一個網址完全正常。POST（老師 cookie 已經失效但還按了操作按鈕，
    例如「開始點名」）此時 req.url.path 是那個 POST-only 的路由本身，登入後 GET 它會 405、
    操作也沒送出、什麼都沒說——改用 Referer 的路徑＋查詢字串（那才是老師原本看著、按下按鈕的
    那一頁）；Referer 的主機要等於這次請求的主機、路徑要以 /t 開頭且不是 /t/login 才採用，
    否則退回 /t，避免被不可信的 Referer 亂轉址。"""
    if req.method == "GET":
        return req.url.path, False
    referer = req.headers.get("referer", "")
    if referer:
        ref = urlparse(referer)
        if ref.netloc == req.url.netloc and ref.path.startswith("/t") and not ref.path.startswith("/t/login"):
            next_path = ref.path + (f"?{ref.query}" if ref.query else "")
            return next_path, True
    return "/t", True


def _login_redirect_url(req: Request) -> str:
    """next 放進網址要 URL 編碼——它可能夾帶查詢字串（例如 Referer 帶著 ?foo=bar），不編碼會
    跟外層的 query string 解析混在一起；safe='/' 只是不必要地把路徑分隔符也編碼掉，不影響解碼
    後的值，純粹讓網址讀起來、既有測試斷言起來都不用改。"""
    next_path, redo = _login_redirect_next(req)
    url = f"/t/login?next={quote(next_path, safe='/')}"
    if redo:
        url += "&redo=1"
    return url


def teacher_redirect(req: Request):
    if not teacher_ok(req):
        return RedirectResponse(_login_redirect_url(req), 303)
    return None


@app.get("/t/login", response_class=HTMLResponse)
def login_page(req: Request, next: str = "/t", redo: str = ""):
    return templates.TemplateResponse(req, "login.html", {"next": next, "error": "", "redo": redo == "1"})


@app.post("/t/login")
def login(req: Request, password: str = Form(..., max_length=200), next: str = Form("/t", max_length=200)):
    ip = rate_ip(req)
    ok = hmac.compare_digest(password.encode(), TEACHER_PW.encode())
    # 清舊鍵、算 fails、寫回字典要跟 _throttle 共用同一把鎖（同一種「走訪整個字典＋
    # 讀寫某個 key」寫法，多執行緒下有同樣的 RuntimeError 風險）；time.sleep(2) 是 I/O 等待，
    # 不放進鎖裡，只鎖到 fails 算完、寫回字典為止。
    with _throttle_lock:
        for k in [k for k, v in _login_fail.items() if all(now() - t >= 600 for t in v)]:
            _login_fail.pop(k, None)
        fails = [t for t in _login_fail.get(ip, []) if now() - t < 600]
        if not ok:
            fails.append(now()); _login_fail[ip] = fails
        else:
            _login_fail.pop(ip, None)
    if not ok:
        if len(fails) >= 8:  # 同一出口錯太多次：每次拖慢但仍以密碼為準，學生猜密碼不會把老師鎖在外面
            time.sleep(2)
            raise HTTPException(429, "嘗試太多次，10 分鐘後再試")
        return templates.TemplateResponse(req, "login.html", {"next": next, "error": "密碼錯誤", "redo": False}, status_code=401)
    safe_next = next if (next.startswith("/") and not next.startswith("//") and "\\" not in next) else "/t"
    resp = RedirectResponse(safe_next, 303)
    resp.set_cookie("ta", make_token("teacher", auth_epoch(), exp=now() + 30 * 86400),
                    httponly=True, samesite="lax", secure=BASE_URL.startswith("https"), max_age=30 * 86400)
    return resp


_LOGOUT_PAGE = (
    "{% extends 'base.html' %}{% block title %}課堂點名｜登出{% endblock %}"
    "{% block body %}<div class=\"mast\"><span class=\"k\"><b>課堂點名</b> · {{ BASE_HOST }}</span></div>"
    "<div style=\"max-width:380px;margin:8vh auto 0\"><h1>登出</h1>"
    "<form method=\"post\" action=\"/t/logout\"><p><button class=\"big\" type=\"submit\">登出所有裝置</button></p></form>"
    "<p class=\"muted sm\">按下去，所有裝置上的老師登入都會失效。</p></div>{% endblock %}"
)


@app.get("/t/logout", response_class=HTMLResponse)
def logout_page(req: Request):
    # GET 不能有副作用（別的網站嵌一張 <img> 就能觸發）；只顯示一顆 POST 按鈕
    if not teacher_ok(req):
        return RedirectResponse("/t/login", 303)
    return HTMLResponse(templates.env.from_string(_LOGOUT_PAGE).render(request=req))


@app.post("/t/logout")
def logout(req: Request):
    if not teacher_ok(req):  # 直接回 401：不走 _err_page（它會把 /t 底下的 401 轉成登入頁 303）
        return PlainTextResponse("請先登入", status_code=401)
    bump_auth_epoch()  # 撤銷所有裝置上的老師 cookie（登出＝全部登出），不必換 SECRET
    resp = RedirectResponse("/t/login", 303)
    resp.delete_cookie("ta")
    return resp


# 老師頁面

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
            recent_rows = con.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=s.id AND k.status IN ('present','late')) AS n_ok "
                "FROM sessions s WHERE course_id=? ORDER BY id DESC LIMIT 6", (c["id"],)).fetchall()
            # 出席率（同一個 attendance_rate 定義）算好傳給樣板，不在樣板裡自己用 n_ok / c.n 算
            # （兩個定義不同：這裡的 n_ok 不含待確認、分母也沒扣請假與加選）
            recent = []
            for s2 in recent_rows:
                low = False
                if s2["closed_at"] is not None:
                    rate2 = attendance_rate(con, s2)
                    low = rate2 is not None and rate2 < DISCORD_AUTO_MIN_RATE
                # 分母跟投影幕 `total` 同一個母體（開場前就在名冊、或本場有簽到列的人，加選生不算），
                # 分子仍是出席＋遲到（同投影幕與 Discord 摘要），待確認另外標出來，數字才對得上
                total2 = con.execute(
                    "SELECT COUNT(*) FROM students WHERE course_id=? AND (created_at<=? OR id IN (SELECT student_pk FROM checkins WHERE session_id=?))",
                    (c["id"], s2["opened_at"], s2["id"])).fetchone()[0]
                pend2 = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=? AND status='pending'", (s2["id"],)).fetchone()[0]
                recent.append({"id": s2["id"], "opened_at": s2["opened_at"], "closed_at": s2["closed_at"],
                               "kind": s2["kind"], "voided_at": s2["voided_at"], "n_ok": s2["n_ok"], "low": low,
                               "total": total2, "n_pending": pend2,
                               # 按了「先不要自動發」之後自動結案，名單沒發出去——主頁要看得到
                               "held": bool(course_settings(c)["discord_channel"]) and s2["voided_at"] is None
                               and s2["note"] in ("discord_held", "discord_held_manual"),
                               "discord_failed": s2["note"] == "discord_failed"})  # 主頁也看得到背景發送失敗
            # 最新一場剛結束（誤按結束的救法還在時效內）而且這門課現在沒有進行中的場次——主頁按「開始點名」
            # 前先問一次，因為開了新的一場，上一場就不能取消結束了
            reopen_mins = 0
            if live is None and recent_rows:
                last = recent_rows[0]
                if last["closed_at"] is not None and last["voided_at"] is None and now() - last["closed_at"] <= REOPEN_WINDOW_SECONDS:
                    reopen_mins = max(1, round((now() - last["closed_at"]) / 60))
            # N ＝報表頁實際列出的場次欄數（已結束、計出缺勤、未作廢，同 build_report 的母體）
            n_sessions = con.execute(f"SELECT COUNT(*) FROM sessions WHERE course_id=? AND closed_at IS NOT NULL AND {COUNTED_SESSION_SQL}",
                                     (c["id"],)).fetchone()[0]
            courses.append({"row": c, "n": n, "live": live, "recent": recent, "settings": course_settings(c),
                            "reopen_mins": reopen_mins, "n_sessions": n_sessions})
    return templates.TemplateResponse(req, "home.html", {"courses": courses})


def do_open_session(con: sqlite3.Connection, cid: int, kind: str) -> int:
    """開一場點名（或找到已經開著的那場，同課同時只會有一場 live）。
    kind='bind' 是綁定場次，open_until=late_until=opened_at+bind_minutes*60（沒有遲到區）。
    finalize_expired 會自己中途 commit，必須在 BEGIN IMMEDIATE 之前跑完，否則交易鎖在它
    commit 的當下就被放掉，「查 live → INSERT」之間會有一段沒鎖住的視窗，兩個執行緒能同時通過
    live 檢查各自開出一場。
    若已有一場未結案場次但種類不同，不沿用也不新建，回 409 讓老師先結束那一場。"""
    finalize_expired(con)  # 自己 commit，不能包進下面那段交易鎖裡
    con.execute("BEGIN IMMEDIATE")  # 之後「查 live → INSERT」全程在同一個交易鎖裡，杜絕上述競態
    c = get_course(con, cid)
    st = course_settings(c)
    live = con.execute("SELECT id, kind FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>?",
                       (cid, now())).fetchone()
    if live:
        if live["kind"] != kind:
            # 兩個方向的建議動作正好相反（誤開一般點名想改開綁定 vs. 綁定還沒收尾想開一般點名），
            # 含糊的「先結束它」會把第一種人導向「按結束」——結束會把當時沒簽的人全記缺席，這正是誤開時最不該做的事。
            if live["kind"] == "normal":
                raise HTTPException(409, "這門課還有一場〈一般點名〉在進行。如果是誤按開的，到那一場按「刪除此次」再開；"
                                    "不要按結束（結束會把沒簽的人全記缺席）。")
            raise HTTPException(409, "這門課的〈只做綁定〉還在進行，先到那一場按「結束點名」，再開一般點名。")
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
                   "qr_interval": iv, "qr_grace": max(0, min(qr_grace, 120)),  # 上限 120 秒，不再綁 qr_interval
                   "spot_n": max(1, spot_n), "ip_check": bool(ip_check),
                   "bind_minutes": max(3, min(bind_minutes, 30)),  # 3–30 分鐘，同 course_settings 的範圍驗證
                   "discord_channel": discord_channel})
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps(st), cid))
    return RedirectResponse(f"/t/course/{cid}/students", 303)


def pct(rate) -> str:
    """出席率百分比，四捨五入（不用銀行家進位：62.5% 要顯示 63%）。"""
    if rate is None:
        return ""
    return f"{int(rate * 100 + 0.5)}%"


def csv_safe(v: str) -> str:
    """Excel 會把 = + - @ 開頭的儲存格當公式（Tab、CR 開頭也會被當成公式前導），前面補一個單引號。"""
    return "'" + v if v and v[0] in "=+-@\t\r" else v


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


# 老師 API

# 投影頁最後一次成功取碼的時間（場次 id → now()）。只存在行程記憶體，跟既有限流一樣靠單
# worker；重啟後清空，屬可接受——老師頁看得到投影頁是不是還在取碼，這是這支字典存在的唯一目的。
# 這場同時開了兩個投影頁（例如教室大投影＋老師自己手機再開一個）時，只要其中一個
# 還連得上，PROJECTOR_SEEN 就會被那一個持續刷新，老師頁看到的仍是「連線正常」，即使教室那個
# 投影頁其實已經斷線。這支字典是 sid 對一個時間戳，天生分不出是哪個分頁——刻意不處理
# 這個情況，教室廣播用單一投影頁是預期用法。
PROJECTOR_SEEN: dict[int, float] = {}


@app.get("/api/t/session/{sid}/qr")
def api_qr(req: Request, sid: int = PathParam(ge=1, le=2**53), have: str = ""):
    require_teacher(req)
    with db() as con:
        s = get_session(con, sid)
        c = get_course(con, s["course_id"])
        st = course_settings(c)
        iv = st["qr_interval"]
        slot = int(now() // iv)
        # 「投影頁連線正常」以前只看「伺服器處理過這次 qr 請求」，這在請求到了、回應
        # 在回程遺失時是假訊號——投影頁其實整片顯示「連不到伺服器」，老師頁卻說正常。改成只有
        # `have`（投影頁畫面上目前顯示的碼）對得上這一格、前一格或前兩格時才更新 PROJECTOR_SEEN，
        # 因為對得上才代表上一次的回應真的送到了投影頁、被畫面收下、下一輪帶著它來問。`have`
        # 空白或對不上（例如第一次取碼、或投影頁還停在更舊的碼）都不算。`have` 是使用者可控的查詢
        # 參數；hmac.compare_digest 對含非 ASCII 字元的 str 會直接丟 TypeError（不是回傳不相等），
        # fuzz 測試餵過奇怪字元就打出 500——code6() 一定是剛好 6 位數字，先擋掉不像的值再比對。
        if have and len(have) == 6 and have.isascii() and have.isdigit() \
                and any(g >= 0 and hmac.compare_digest(have, code6(slot_sig(s, g))) for g in (slot, slot - 1, slot - 2)):
            PROJECTOR_SEEN[sid] = now()
        n_ok = con.execute("SELECT COUNT(*) FROM checkins k JOIN students st ON st.id=k.student_pk WHERE k.session_id=? AND k.status IN ('present','late')",
                           (sid,)).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM students WHERE course_id=? AND (created_at<=? OR id IN (SELECT student_pk FROM checkins WHERE session_id=?))",
                            (s["course_id"], s["opened_at"], sid)).fetchone()[0]
        # 這一場已結束時，順便查同課有沒有另一場正在進行——投影頁與舊表單靠這個判斷是不是
        # 該老師開了新的一場（不是網路問題），沒有就是 None。
        nrow = con.execute("SELECT id FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>? ORDER BY id DESC LIMIT 1",
                           (s["course_id"], now())).fetchone()
        next_sid = nrow["id"] if nrow else None
    phase = session_phase(s)
    sig = slot_sig(s, slot)
    url = f"{BASE_URL}/s/{sid}/{slot}/{sig}"
    return {
        "phase": phase, "svg": (qr_svg(url) if (phase != "closed" and code6(sig) != have) else ""), "code": code6(sig),
        "remain": round((slot + 1) * iv - now(), 1), "interval": iv,
        "n_ok": n_ok, "total": total,
        "open_until": s["open_until"], "late_until": s["late_until"], "now": now(),
        "kind": s["kind"],  # 投影頁要標「綁定場次」
        "grace": st["qr_grace"],  # staleAt 要改算「這張 QR 的換碼時間點＋grace」，不能只用 interval
        "next_sid": next_sid,  # 只有 phase=='closed' 時前端才需要用它
        "opened_at": s["opened_at"],  # 刪除後重開沿用同一個場次號，投影頁靠這個值變了才知道要重送教室 IP
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


def compute_funnel(con: sqlite3.Connection, sid: int, opened_at: float) -> dict:
    """即時漏斗：本場累計與最近 60 秒各一組。
    所有查詢一律再加 ts>=opened_at 這道底——場次刪除後重開會沿用同一個 id，
    刪除前留下的舊事件（land_ok／checkin_reject…）不能算進重開後的新場次，`opened_at`
    就是這次真正開場的起點，比任何一組 since 都更早時取這個底線。"""
    t = now()

    def group(since: float | None) -> dict:
        floor = opened_at if since is None else max(since, opened_at)

        def cnt(kind: str) -> int:
            return con.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND kind=? AND ts>=?", (sid, kind, floor)).fetchone()[0]

        reject_rows = con.execute(
            "SELECT reason, COUNT(*) n FROM events WHERE session_id=? AND kind='checkin_reject' AND ts>=? GROUP BY reason",
            (sid, floor)).fetchall()
        reject_by_reason = {r["reason"]: r["n"] for r in reject_rows}
        land_rows = con.execute(
            "SELECT device8, MAX(ts) t0 FROM events WHERE session_id=? AND kind='land_ok' AND device8!='' AND ts>=? GROUP BY device8",
            (sid, floor)).fetchall()
        # 送出時網路中斷（前端回報 client／neterr:…）、之後同一支裝置還沒有成功簽到的，另算「送出失敗（網路）」，
        # 不再混在「落地未送出」裡
        net_devs = {r["device8"] for r in con.execute(
            "SELECT DISTINCT e.device8 FROM events e WHERE e.session_id=? AND e.kind='client' AND e.reason LIKE 'neterr:%' "
            "AND e.device8!='' AND e.ts>=? AND NOT EXISTS (SELECT 1 FROM events o WHERE o.session_id=e.session_id "
            "AND o.device8=e.device8 AND o.kind='checkin_ok' AND o.ts>=e.ts)", (sid, floor)).fetchall()}
        silent = 0
        for r in land_rows:
            if r["device8"] in net_devs:
                continue
            if t - r["t0"] < 90:  # 還沒滿 90 秒，先不算沉默
                continue
            # checkin_error（伺服器炸了，例如 database is locked）也算「有送出過」——
            # 那是伺服器的問題，不是學生沉默不動作，不能被算進「落地未送出」。
            attempted = con.execute(
                "SELECT 1 FROM events WHERE session_id=? AND device8=? AND kind IN "
                "('checkin_ok','checkin_reject','checkin_error') AND ts>? LIMIT 1",
                (sid, r["device8"], r["t0"])).fetchone()
            if not attempted:
                silent += 1
        return {
            "land_ok": cnt("land_ok"), "land_expired": cnt("land_expired"), "checkin_ok": cnt("checkin_ok"),
            "reject": sum(reject_by_reason.values()), "reject_by_reason": reject_by_reason,
            "silent": silent, "client_errors": cnt("client"), "server_error": cnt("checkin_error"),
            "code_bad": cnt("code_bad"), "net_fail": len(net_devs),
        }

    return {"total": group(None), "recent60": group(t - 60)}


def _live_sid(con: sqlite3.Connection, cid: int) -> int | None:
    """這門課目前進行中的場次（closed_at IS NULL AND late_until>now()，取 id 最大的），沒有就 None——
    L3／L8 事件的 session_id 用（老師操作與 whoami 都不帶場次號）。"""
    row = con.execute("SELECT id FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>? ORDER BY id DESC LIMIT 1",
                      (cid, now())).fetchone()
    return row["id"] if row else None


CONFLICT_REJECT_REASONS = ("pin_exists", "pin_wrong", "pin_locked", "already_recorded")  # 第四個原碼由舊的 bound_other_device 改名
# 老師手動處理過的列，不看 already_recorded——PIN 已驗過才會回這個原因碼，
# 多半是本人沒注意到老師已經記了；PIN 被猜的三種（真的有人在試這個學號）照舊要提醒。
PIN_REJECT_REASONS = ("pin_locked", "pin_exists", "pin_wrong")
CONFLICT_REJECT_REASONS_MANUAL = tuple(r for r in CONFLICT_REJECT_REASONS if r != "already_recorded")


def _last_unbind_ts(con: sqlite3.Connection, student_pk: int) -> float:
    """這位學生最後一次被老師解除綁定或重設的時間（沒有就 0）。"""
    return con.execute("SELECT MAX(ts) FROM events WHERE kind='unbind' AND student_pk=?", (student_pk,)).fetchone()[0] or 0


def compute_stuck(con: sqlite3.Connection, sid: int, opened_at: float) -> list[dict]:
    """最近 3 分鐘內被拒 2 次以上、且尚未成功的學生，給老師走過去幫忙。
    底線同樣不早於 opened_at，理由同 compute_funnel。
    搶綁檢查：已經簽到成功（出席／遲到／待確認）的學號，若另一支手機（裝置雜湊跟這筆簽到列不同）
    最近 3 分鐘內用同一個學號被拒（原因是 pin_exists/pin_wrong/pin_locked/already_recorded 之一），
    門檻降到 1 筆——可能是被搶先綁定，也可能單純有人拿這個學號亂試，都值得老師當場確認本人。
    帶 conflict=True 讓老師頁標紅字；同一支已綁裝置自己送出的拒絕（device8 跟這筆簽到列相同）不算。
    already_recorded（陌生的新環境打對 PIN，多半是本人換環境再掃）改輸出灰字提示（info=True、conflict=False），
    除非 device8 就是這筆改綁前的舊裝置（機主發現被改綁走了），或同時有 PIN 打錯的拒絕——那兩種照舊紅字。
    只算「這筆簽到之後」才發生的拒絕——底線用 max(floor, 這筆簽到列的 ts)：拒絕發生在簽到之前
    （例如先試錯幾次才簽到成功）是正常過程，不是搶綁，不該算進來。"""
    t = now()
    floor = max(t - 180, opened_at)
    # 每位學生的底線再取「最後一筆 unbind 事件（解綁或重設）的時間」，那之前的拒絕不算；
    # unbind 事件的 session_id 可能是 None 或別場（見 api_unbind），所以只用 student_pk 查
    rows = con.execute(
        "SELECT e.student_pk, COUNT(*) n, MAX(e.id) last_id FROM events e "
        "WHERE e.session_id=? AND e.kind='checkin_reject' AND e.student_pk IS NOT NULL AND e.ts>? "
        "AND e.ts>COALESCE((SELECT MAX(u.ts) FROM events u WHERE u.kind='unbind' AND u.student_pk=e.student_pk), 0) "
        "GROUP BY e.student_pk HAVING COUNT(*)>=2", (sid, floor)).fetchall()
    out = []
    for r in rows:
        k = con.execute("SELECT status, source FROM checkins WHERE session_id=? AND student_pk=?", (sid, r["student_pk"])).fetchone()
        if k and (k["status"] in ("present", "late", "pending") or k["source"] == "manual"):
            continue  # 已經成功（含老師手動放行）——這種情況改由下面的搶綁檢查接手；老師手動記請假／缺席也算處理完
        stu = con.execute("SELECT student_id, name FROM students WHERE id=?", (r["student_pk"],)).fetchone()
        if not stu:
            continue
        last = con.execute("SELECT reason FROM events WHERE id=?", (r["last_id"],)).fetchone()
        reason = last["reason"] if last else ""
        out.append({"pk": r["student_pk"], "student_id": stu["student_id"], "name": stu["name"],
                    "reason": reason, "reason_label": REJECT_REASON_LABEL.get(reason, reason), "conflict": False})
    checked = con.execute(
        "SELECT student_pk, device_hash, flags, ts, source FROM checkins WHERE session_id=? AND status IN ('present','late','pending')",
        (sid,)).fetchall()
    for c in checked:
        dev8 = (c["device_hash"] or "")[:8]
        c_floor = max(floor, c["ts"] or 0, _last_unbind_ts(con, c["student_pk"]))
        reasons = CONFLICT_REJECT_REASONS
        if c["source"] == "manual":
            # 手動列 ts 寫 0，底線改用最後一次 manual_status 事件的時間；查不到（舊資料）就不列紅字
            m = con.execute("SELECT MAX(ts) FROM events WHERE session_id=? AND kind='manual_status' AND student_pk=?",
                            (sid, c["student_pk"])).fetchone()[0]
            if m is None:
                continue
            c_floor = max(c_floor, m)
            reasons = CONFLICT_REJECT_REASONS_MANUAL
        placeholders = ",".join("?" * len(reasons))
        ev = con.execute(
            f"SELECT device8, reason, ts FROM events WHERE session_id=? AND kind='checkin_reject' AND student_pk=? "
            f"AND ts>? AND reason IN ({placeholders})",
            (sid, c["student_pk"], c_floor, *reasons)).fetchall()
        mismatched = [e for e in ev if e["device8"] != dev8]
        if not mismatched:
            continue
        stu = con.execute("SELECT student_id, name FROM students WHERE id=?", (c["student_pk"],)).fetchone()
        if not stu:
            continue
        flags = json.loads(c["flags"]) if c["flags"] else []  # flags 一律經 json.dumps 寫入，不會壞（同 api_state 的寫法）
        # 本人換環境再掃一次（iPhone 每掃一次一個新 cookie 罐）打對 PIN 會被
        # already_recorded 擋下，這種不是搶綁。old8＝這筆是改綁來的時候，改綁前那支的 device_hash[:8]（見 do_checkin
        # 的 rebind 事件）；機主用原本那支來才是「發現被改綁走了」。
        old8 = None
        if "rebind" in flags:
            rb = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='rebind' AND student_pk=? ORDER BY id DESC LIMIT 1",
                             (sid, c["student_pk"])).fetchone()
            old8 = (rb["reason"] or None) if rb else None  # 解除綁定後靠 PIN 綁上的 rebind 事件 reason 是空字串，不能當「原本那支」
        red = [e for e in mismatched if e["reason"] != "already_recorded" or e["device8"] == old8]
        # 老師剛處理過這位同學（本場手動記狀態，或任何一場解綁、重設），之後 10 分鐘內的 PIN 類拒絕多半是
        # 本人又試了一次，灰字提示；超過 10 分鐘照舊紅字
        mk = con.execute("SELECT MAX(ts) FROM events WHERE session_id=? AND kind='manual_status' AND student_pk=?",
                         (sid, c["student_pk"])).fetchone()[0]
        handled = max(mk or 0, _last_unbind_ts(con, c["student_pk"]))
        if red and handled:
            fresh = [e for e in red if e["reason"] in PIN_REJECT_REASONS and 0 <= e["ts"] - handled <= 600]
            if fresh and len(fresh) == len(red):
                out.append({"pk": c["student_pk"], "student_id": stu["student_id"], "name": stu["name"], "reason": red[0]["reason"],
                            "reason_label": "剛處理過的同學又試了一次", "conflict": False, "info": True})
                continue
            red = [e for e in red if e not in fresh]
        if not red:  # 只剩陌生的新環境打對了 PIN：灰字提示，不亮紅
            out.append({"pk": c["student_pk"], "student_id": stu["student_id"], "name": stu["name"], "reason": "already_recorded",
                        "reason_label": "本人可能又掃了一次（新瀏覽器打對 PIN，本場已有紀錄）", "conflict": False, "info": True})
            continue
        if "rebind" in flags:  # 改綁的人是不是本人，要老師當場看
            label = "本場剛改綁，另一個瀏覽器又用這個學號簽：可能被別人用 PIN 搶先改綁，請當場確認本人"
        elif "late_bind" in flags:
            label = "本場剛綁定，另一支手機又用這個學號簽：可能被別人搶先綁定，請當場確認本人"
        else:
            label = "已經簽到，另一支手機又用這個學號簽：請當場確認本人"
        out.append({"pk": c["student_pk"], "student_id": stu["student_id"], "name": stu["name"],
                    "reason": red[0]["reason"], "reason_label": label, "conflict": True})
    return out


def compute_ip_check_muted(out_rows: list[dict], teacher_ip: str) -> bool:
    """多數人不是走教室網路時（實測 9 支手機各自不同出口 IP），
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
            "k.ts, k.seat, k.status, k.flags, k.ip, k.source, st.created_at "
            "FROM students st LEFT JOIN checkins k ON k.student_pk=st.id AND k.session_id=? "
            "WHERE st.course_id=? ORDER BY st.student_id", (sid, s["course_id"])).fetchall()
        spots = con.execute("SELECT sc.*, st.name, st.student_id FROM spotchecks sc JOIN students st ON st.id=sc.student_pk "
                            "WHERE session_id=? ORDER BY sc.id", (sid,)).fetchall()
        st = course_settings(get_course(con, s["course_id"]))
        locked = {r["student_pk"] for r in con.execute(
            "SELECT student_pk FROM pin_fail WHERE session_id=? AND ((device_hash='' AND n>=?) OR (device_hash!='' AND n>=?))",
            (sid, PIN_MAX_FAIL_TOTAL, st["pin_max_fail"]))}
        funnel = compute_funnel(con, sid, s["opened_at"])
        stuck = compute_stuck(con, sid, s["opened_at"])
        # 本場已結束、沒作廢、沒有更晚的場次、結束不到 10 分鐘＝還能取消結束，回剩餘秒數，否則 0
        reopen_left = 0
        if s["closed_at"] and s["voided_at"] is None and now() - s["closed_at"] <= REOPEN_WINDOW_SECONDS and not _later_session_exists(con, s):
            reopen_left = max(1, int(REOPEN_WINDOW_SECONDS - (now() - s["closed_at"])))
        # 名單發出去之後又改了幾筆狀態（Discord 上的還是舊名單）；沒發過就 0
        last_sent = con.execute("SELECT MAX(ts) FROM events WHERE session_id=? AND kind='discord_sent'", (sid,)).fetchone()[0]
        discord_stale = 0 if last_sent is None else con.execute(
            "SELECT COUNT(*) FROM events WHERE session_id=? AND kind='manual_status' AND ts>?", (sid, last_sent)).fetchone()[0]
        fail_text = discord_fail_text(con, s)
        # 每位學生本場最後一筆被擋／出錯的事件（不受 compute_stuck 的 180 秒窗口限制，結案後也留著）
        # 重設／解除綁定之前的拒絕不算（每位學生取最後一筆 unbind 事件的時間當底線）
        unbound_at = {u["student_pk"]: u["t"] for u in con.execute(
            "SELECT student_pk, MAX(ts) AS t FROM events WHERE kind='unbind' AND student_pk IS NOT NULL GROUP BY student_pk")}
        last_try: dict[int, str] = {}
        for ev in con.execute(
                "SELECT student_pk, kind, reason, ts FROM events WHERE session_id=? AND student_pk IS NOT NULL "
                "AND kind IN ('checkin_reject','checkin_error') ORDER BY id", (sid,)):
            if ev["ts"] <= unbound_at.get(ev["student_pk"], 0):
                continue
            last_try[ev["student_pk"]] = "error" if ev["kind"] == "checkin_error" else (ev["reason"] or "")
        auto_absent_n = con.execute(
            "SELECT COUNT(*) FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).fetchone()[0]
        rate = attendance_rate(con, s)
        # 三分鐘（grant_seconds）內掃到、本場還沒簽到成功的手機數——老師提早按「結束點名」前要讓他知道
        # 落地事件也算六位數進來的（code_ok）；扣掉兩種「不是還在填」：這支裝置落地之後被擋了 already_recorded／pin_locked
        # （他是本人又掃一次或鎖住了，不會再填），和落地事件帶了 student_pk、而這個學號本場已經有人記好了（老師手動記、
        # 別支瀏覽器簽的）。新環境落地時還不知道是誰（沒帶 student_pk），之後被擋的事件才帶 student_pk（例如 pin_wrong），
        # 所以也看這支裝置落地之後的 checkin_reject：帶了 student_pk、而該學號本場已有 source!='auto' 的簽到列，同樣不算。
        # 被擋事件沒有 student_pk 的（不歸戶那些）照算，因為不知道是誰。
        # 還算不到的情況：iPhone 先開 A 沒送、改用 B 簽成功，A 仍會被算到三分鐘。
        filling = con.execute(
            "SELECT COUNT(DISTINCT e.device8) FROM events e WHERE e.session_id=? AND e.kind IN ('land_ok','code_ok') AND e.ts>? AND e.device8!='' "
            "AND (e.reason!='nocookie' OR EXISTS (SELECT 1 FROM events l WHERE l.session_id=e.session_id AND l.device8=e.device8 "
            "AND l.id>e.id AND l.kind IN ('whoami','checkin_ok','checkin_reject','client'))) "
            "AND NOT EXISTS (SELECT 1 FROM events c WHERE c.session_id=e.session_id AND c.kind='checkin_ok' AND c.device8=e.device8) "
            "AND NOT EXISTS (SELECT 1 FROM events r WHERE r.session_id=e.session_id AND r.kind='checkin_reject' AND r.device8=e.device8 "
            "AND r.ts>=e.ts AND r.reason IN ('already_recorded','pin_locked')) "
            "AND NOT (e.student_pk IS NOT NULL AND EXISTS (SELECT 1 FROM checkins k WHERE k.session_id=e.session_id "
            "AND k.student_pk=e.student_pk AND k.source!='auto')) "
            "AND NOT EXISTS (SELECT 1 FROM events r2 JOIN checkins k2 ON k2.session_id=r2.session_id AND k2.student_pk=r2.student_pk "
            "AND k2.source!='auto' WHERE r2.session_id=e.session_id AND r2.kind='checkin_reject' AND r2.device8=e.device8 "
            "AND r2.ts>=e.ts AND r2.student_pk IS NOT NULL)",
            (sid, now() - st["grant_seconds"])).fetchone()[0]
    out = []
    for r in rows:
        out.append({
            "pk": r["pk"], "student_id": r["student_id"], "name": r["name"], "cls": r["cls"], "bound": bool(r["bound"]),
            "ts": fmt(r["ts"]) if (r["ts"] or 0) > 0 and r["source"] != "auto" else "", "seat": r["seat"] or "", "status": r["status"] or "",
            # 已經簽到成功的人不再掛「PIN 鎖定」（只改顯示，不清 pin_fail——清掉等於讓旁人繼續試）
            "locked": r["pk"] in locked and r["status"] not in ("present", "late", "pending"),
            "flags": json.loads(r["flags"]) if r["flags"] else [], "ip": r["ip"] or "", "source": r["source"] or "",
            # 開場後才加選、本場沒有任何簽到列＝這場不適用，不算「未簽到」
            "na": r["status"] is None and r["created_at"] > s["opened_at"],
            # 被擋過、還沒簽到（沒有簽到列，或只有結案自動補的缺席列）才標；已簽到與老師手動處理過的不標
            "tried": last_try.get(r["pk"], "") if (r["status"] is None or r["source"] == "auto") else "",
        })
    ip_muted = compute_ip_check_muted(out, s["teacher_ip"])
    if ip_muted:  # 原始事實留在 DB（CSV 照舊），序列化給老師看的這份濾掉沒有參考性的旗標
        for r in out:
            if "ip_differs" in r["flags"]:
                r["flags"] = [f for f in r["flags"] if f != "ip_differs"]
    counts = {k: sum(1 for x in out if x["status"] == k) for k in STATUS_LABEL}
    counts["none"] = sum(1 for x in out if not x["status"] and not x["na"])
    is_bind = s["kind"] == "bind"
    # 綁定場次不論課程的 allow_bind 設定，一律允許首次綁定，老師頁的開關要照實顯示
    # 這場是強制開放，而不是誤導老師以為那顆開關真的在控制這一場
    allow_bind_out = True if is_bind else st["allow_bind"]
    # 出席不到一半的提示與 Discord 脫鉤——不看 note（那只有設了頻道才會寫），
    # 手動按結束與自動結案一視同仁，只看 session_phase 是否已結束＋attendance_rate 是否過低
    low_rate = session_phase(s) == "closed" and rate is not None and rate < DISCORD_AUTO_MIN_RATE
    # 投影頁最後一次成功取碼是多久前，None＝還沒有任何一次
    proj_seen = PROJECTOR_SEEN.get(sid)
    projector_age = round(now() - proj_seen) if proj_seen is not None else None
    opened_age = round(now() - s["opened_at"])  # 開場多久了，判「還沒收到投影頁消息」是剛開始還是拖太久
    return {"phase": session_phase(s), "rows": out, "counts": counts, "teacher_ip": s["teacher_ip"],
            "low_rate": low_rate, "rate": rate, "projector_age": projector_age, "opened_age": opened_age,
            "allow_bind": allow_bind_out, "bind_forced": is_bind, "course_id": s["course_id"],
            "discord": bool(st["discord_channel"]),
            # 作廢不再清 note，改成讀這個提示時一律要求 voided_at IS NULL——
            # 取消作廢後場次重新計入出缺勤，提示自然回來，不用另外找地方補寫 note
            # note 有兩種暫緩原因（'discord_held'＝出席率低、'discord_held_manual'＝老師手動按），
            # discord_held 兩種都算真（進行中就可能是真，不必等結案）；discord_held_manual 另外標出來，
            # 讓 #heldMsg 換句話講對原因。
            "discord_held": s["note"] in ("discord_held", "discord_held_manual") and s["voided_at"] is None,
            "discord_held_manual": s["note"] == "discord_held_manual",
            # 背景發送失敗（token 失效、bot 被移除、頻道錯…）留下線索，讓老師頁能顯示紅字
            # 提醒重按「發到 Discord」；同樣要求 voided_at IS NULL（讀取時判斷的規則）。
            "discord_failed": s["note"] == "discord_failed" and s["voided_at"] is None,
            "discord_fail_text": fail_text,  # 紅字照失敗原因說（逾時＝可能已經發出）
            "open_until": s["open_until"], "late_until": s["late_until"], "now": now(),
            "funnel": funnel, "stuck": stuck, "filling": filling,
            "reopen_left": reopen_left, "discord_stale": discord_stale,
            "kind": s["kind"], "voided": s["voided_at"] is not None, "auto_absent_n": auto_absent_n,
            "ip_check_muted": ip_muted,  # 多數人不走教室網路時靜音 ip_differs
            "bound_n": sum(1 for r in out if r["bound"]),  # 綁定場次的 counts 只呈現「已綁定 N 人」
            "spots": [{"id": x["id"], "pk": x["student_pk"], "name": x["name"], "student_id": x["student_id"],
                       "seat": x["seat"], "result": x["result"]} for x in spots]}


def do_extend(con: sqlite3.Connection, s: sqlite3.Row, minutes: int, target: str | None = None) -> tuple[str, str]:
    """延長一場點名，回傳（延長後的 phase, note）。綁定場次沒有遲到區，open_until／late_until 同步延長。
    `target`是老師按下當時按鈕寫的字（'open'／'late'），只有已進遲到區、老師其實想延長出席窗
    （按鈕當下還寫著「延長出席窗」，代表請求還在路上時才被伺服器判定成遲到區）時才有意義；
    `target` 不給（例如還開著舊版點名頁的老師）就照現行只看伺服器目前 phase 的行為，不是新規則。"""
    if s["closed_at"] or now() > s["late_until"]:
        raise HTTPException(409, EXTEND_CLOSED_MSG)
    add = max(1, minutes) * 60

    def _update(sql: str, args: tuple) -> None:
        # 判定「還沒結束」到寫入之間，另一個分頁的輪詢可能已經自動結案；每句 UPDATE 都帶
        # AND closed_at IS NULL，落在這個縫隙裡就回跟函式開頭同一句 409，不能把已結案的場次改回去。
        if con.execute(sql + " AND closed_at IS NULL", args).rowcount == 0:
            raise HTTPException(409, EXTEND_CLOSED_MSG)

    if s["kind"] == "bind":
        # 從「現在」與原結束時間取大者起算，結束時間不會被算到過去
        new_until = max(s["open_until"], s["late_until"], now()) + add
        _update("UPDATE sessions SET open_until=?, late_until=? WHERE id=?", (new_until, new_until, s["id"]))
        return "open", ""
    st = course_settings(get_course(con, s["course_id"]))
    phase = session_phase(s)
    if phase == "open":
        new_open = s["open_until"] + add
        tail = max(60, (st["late_minutes"] - st["open_minutes"]) * 60)  # 遲到區長度照設定，不壓成 60 秒
        _update("UPDATE sessions SET open_until=?, late_until=MAX(late_until,?) WHERE id=?", (new_open, new_open + tail, s["id"]))
        return "open", ""
    # 已進遲到區：只把遲到窗往後延，不讓晚到的人反而拿到出席——除非老師按下當時出席窗其實還沒關
    # （target='open'），且請求送到伺服器只慢了一點點。60 秒這個寬限值：9/24–26 Cloudflare 亞太事故
    # 實測請求延遲每次約 0.5 秒、卡住時可到數十秒，60 秒足以吃掉這類延遲又不至於讓太久之後才點的
    # 延長被誤判成「其實出席窗還沒關」。
    if target == "open" and now() - s["open_until"] <= 60:
        new_open = s["open_until"] + add
        tail = max(60, (st["late_minutes"] - st["open_minutes"]) * 60)
        _update("UPDATE sessions SET open_until=?, late_until=MAX(late_until,?) WHERE id=?", (new_open, new_open + tail, s["id"]))
        # 出席窗等於沒關過——請求晚到的空檔裡送出、被記遲到的人，改回出席（否則比他晚掃到的人反而記出席）。
        # 只動學生自己掃的列（source='qr'）；待確認的列只去旗標，確認後照 was_late 有無決定出席或遲到；老師手動記的不動。
        for k in con.execute("SELECT id, status, flags FROM checkins WHERE session_id=? AND source='qr'", (s["id"],)).fetchall():
            kf = json.loads(k["flags"] or "[]")
            if "was_late" not in kf:
                continue
            con.execute("UPDATE checkins SET flags=?, status=CASE WHEN status='late' THEN 'present' ELSE status END WHERE id=?",
                        (json.dumps([f for f in kf if f != "was_late"]), k["id"]))
        return "open", ""
    _update("UPDATE sessions SET late_until=late_until+? WHERE id=?", (add, s["id"]))
    note = "出席窗已經關了，這次延長的是遲到窗；晚到的人會記遲到。" if target == "open" else ""
    return "late", note


@app.post("/api/t/session/{sid}/extend")
def api_extend(req: Request, sid: int = PathParam(ge=1, le=2**53), minutes: int = Form(2, ge=1, le=120), target: str = Form("")):
    require_teacher(req)
    with db() as con:
        s = get_session(con, sid)
        phase, note = do_extend(con, s, minutes, target or None)
        log_event_on(con, sid, "extend", reason=f"{minutes}:{target}:{phase}")  # L8
    out = {"ok": True, "phase": phase}
    if note:
        out["note"] = note
    return out


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")


def _discord_fail_msg(status: int | None = None, exc: BaseException | None = None) -> str:
    """Discord 失敗一律換成老師看得懂的中文，不把 urllib 的英文原文（例如 urlopen error timed out）端給他。
    逾時最要緊：請求可能其實已送達，直接重發會把名單貼兩遍。"""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "Discord 沒有回應，名單可能已經發出；先到頻道看一下再決定要不要重發"
    if status in (401, 403):
        return f"Discord 機器人權限不足（HTTP {status}）"
    if status == 429:
        return "Discord 限流中，稍後再發"
    return f"Discord 發送失敗（HTTP {status}）" if status is not None else f"Discord 發送失敗（{type(exc).__name__}）"


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
            if r.status < 300:
                return True, f"HTTP {r.status}"
            return False, _discord_fail_msg(status=r.status)
    except urllib.error.HTTPError as e:
        return False, _discord_fail_msg(status=e.code)
    except Exception as e:  # noqa: BLE001
        return False, _discord_fail_msg(exc=e)


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
    return ok, "; ".join(dict.fromkeys(msgs)) if msgs else "OK"  # 幾則都同一個原因就只說一次


def _bg_discord_post(sid: int, channel: str, text: str) -> None:
    """背景執行緒發 Discord（api_close／finalize_expired 兩處都走這支，不再直接呼叫
    discord_post_long）。失敗（回 False 或丟例外）不能悶聲吞掉——印一行 stderr、記一筆
    discord_fail 事件、note 為空字串時寫成 discord_failed，讓老師頁看得到、能重按「發到
    Discord」。note 已經是 discord_held／discord_held_manual（老師自己按過暫緩、或出席率低
    暫緩）就不動——這兩種情況背景發送本來就不會被呼叫到（should_hold_discord 已經擋在前面），
    這裡的 WHERE note='' 只是多一道防呆，不會真的蓋掉什麼。"""
    raw = ""
    try:
        ok, msg = discord_post_long(channel, text)
    except Exception as e:  # noqa: BLE001 背景執行緒的例外不能悶聲死掉、什麼線索都不留
        # 事件與老師頁只放中文訊息；例外原文只印到伺服器日誌，線索仍查得到
        ok, msg, raw = False, _discord_fail_msg(exc=e), f"{type(e).__name__}: {e}"[:200]
    if ok:
        log_event(sid, "discord_sent", reason="auto")  # api_state 的 discord_stale 以這筆為界
        with db() as con:  # 取消結束後再結束、這次發成功了，上一次失敗留下的紅字要跟著清掉
            con.execute("UPDATE sessions SET note='' WHERE id=? AND note='discord_failed'", (sid,))
        return
    reason = msg[:200]
    print(f"[discord] 場次 {sid} 背景發送失敗：{reason}" + (f"（{raw}）" if raw else ""), file=sys.stderr)
    log_event(sid, "discord_fail", reason=reason)
    with db() as con:
        con.execute("UPDATE sessions SET note='discord_failed' WHERE id=? AND note=''", (sid,))


def discord_fail_text(con, s) -> str:
    """背景發送失敗時老師頁紅字照原因說。逾時的請求可能其實已經送達，直接叫老師重發會在頻道貼兩份，
    所以逾時改叫他先看頻道；其他原因把中文原因接在後面。事件原因若不是中文訊息（例如較早的資料裡記的英文例外原文）就不顯示原文。"""
    if s["note"] != "discord_failed" or s["voided_at"] is not None:
        return ""
    r = con.execute("SELECT reason FROM events WHERE session_id=? AND kind='discord_fail' ORDER BY id DESC LIMIT 1",
                    (s["id"],)).fetchone()
    reason = (r["reason"] if r else "") or ""
    if "Discord 沒有回應" in reason:
        return ("自動發到 Discord 時對方沒有回應，名單可能已經發出。先到頻道看一下：沒看到名單再按「發到 Discord」；"
                "看到了就不用再按，再按會貼兩份。")
    detail = f"：{reason}" if reason.startswith("Discord") else "（可能是機器人權限或頻道設定有問題）"
    return f"自動發到 Discord 沒有成功{detail}。請按「發到 Discord」重發；還是失敗就把這行訊息記下來。"


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
        if s["kind"] != "normal":  # 綁定場次不發 Discord
            return {"ok": True, "discord": False}
        channel, text = session_summary(con, sid)
        # 手動結束也套用「出席不到一半就暫緩發送」——只有 finalize_expired 看不夠，
        # 投影壞掉、老師手動按結束時，全班缺席名單會立刻公開且撤不回。
        # 老師自己按過「這場先不要自動發」（note=='discord_held_manual'）時，should_hold_discord
        # 一樣回真，但不論出席率——不蓋掉 note，留在 discord_held_manual，held 一律回 true。
        if should_hold_discord(con, s, channel):
            if s["note"] != "discord_held_manual":
                con.execute("UPDATE sessions SET note='discord_held' WHERE id=?", (sid,))
            return {"ok": True, "discord": False, "held": True}
    if channel:
        threading.Thread(target=lambda: _bg_discord_post(sid, channel, text), daemon=True).start()
    return {"ok": True, "discord": bool(channel)}


REOPEN_WINDOW_SECONDS = 600  # 結束後 10 分鐘內可以取消結束


def _later_session_exists(con: sqlite3.Connection, s: sqlite3.Row) -> bool:
    return con.execute("SELECT 1 FROM sessions WHERE course_id=? AND opened_at>? LIMIT 1", (s["course_id"], s["opened_at"])).fetchone() is not None


def do_reopen_session(con: sqlite3.Connection, sid: int) -> dict:
    """誤按「結束點名」的救法——10 分鐘內取消結束。
    核心邏輯放在這支不帶裝飾器的函式（mutmut 跳過帶裝飾器的函式），路由只做 require_teacher 與開連線。
    依序檢查（不符回 409，訊息一字不改）：還沒結束／已作廢／結束超過 10 分鐘／同課之後已經開了新的一場。
    全部動作在同一個 BEGIN IMMEDIATE 交易裡：
      1. 刪掉本場 source='auto' AND status='absent' 的簽到列（結案自動補的缺席；老師結束後改過的列 source 已經是 manual，不刪）。
      2. closed_at 清成 NULL。
      3. 時間窗：保底 2 分鐘，不論取消時窗還剩多少——
         出席窗是被這次結束切斷的（closed_at < open_until）→ open_until=max(open_until, 現在+120 秒)；
         結束時已經在遲到窗的 open_until 不動——延長出席窗會讓後掃的人記出席、先掃的人記遲到（不公平）；
         late_until=max(late_until, 現在+120 秒)；最後 late_until 取 max(late_until, open_until)
         （綁定場次因此會把 open_until 設成跟 late_until 一樣）。
      4. note 是 discord_held（自動暫緩）或 discord_failed（背景發送失敗）的清回空字串；discord_held_manual（老師自己按的）不動。
      5. 記一筆 reopen 事件（reason＝「拿掉的缺席列數｜原本 HH:MM:SS 結束」）。
    pin_fail 在結案時已清掉，取消結束不還原——PIN 失敗次數重新起算。"""
    con.execute("BEGIN IMMEDIATE")
    s = get_session(con, sid)
    if not s["closed_at"]:
        raise HTTPException(409, "這一場還沒結束，不用取消。")
    if s["voided_at"] is not None:
        raise HTTPException(409, "這一場已作廢，不能取消結束。")
    t = now()
    if t - s["closed_at"] > REOPEN_WINDOW_SECONDS:
        raise HTTPException(409, "結束超過 10 分鐘，不能取消結束了。晚到的人請在名單上改狀態，或用點名頁的「手動簽到」補登。")
    if _later_session_exists(con, s):
        raise HTTPException(409, "這門課之後已經開了新的一場，不能取消結束這一場。")
    n = con.execute("DELETE FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).rowcount
    open_until, late_until = s["open_until"], s["late_until"]
    if s["closed_at"] < open_until:  # 出席窗是被這次結束切斷的
        open_until = max(open_until, t + 120)
    late_until = max(late_until, t + 120)
    late_until = max(late_until, open_until)
    if s["kind"] == "bind":
        # 綁定場次沒有遲到區，不變式是 open_until＝late_until。自動到期結案的綁定場次 closed_at 剛好等於 open_until，
        # 上面「結束時已在遲到窗就不動 open_until」會讓兩者分開：取消結束後變成遲到區、投影頁「剩 0 分鐘」、延長又從過去起算
        open_until = late_until
    con.execute("UPDATE sessions SET closed_at=NULL, open_until=?, late_until=?, "
                "note=CASE WHEN note IN ('discord_held','discord_failed') THEN '' ELSE note END WHERE id=?",
                (open_until, late_until, sid))
    log_event_on(con, sid, "reopen", reason=f"{n}｜原本 {fmt(s['closed_at'])} 結束")
    sent = con.execute("SELECT 1 FROM events WHERE session_id=? AND kind='discord_sent' LIMIT 1", (sid,)).fetchone() is not None
    return {"ok": True, "removed": n, "discord_sent": sent}


@app.post("/api/t/session/{sid}/reopen")
def api_reopen_session(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    require_teacher(req)
    with db() as con:
        return do_reopen_session(con, sid)


def do_notify_prepare(con: sqlite3.Connection, sid: int) -> tuple[str, str]:
    """組訊息前先跑一次 finalize_expired——遲到窗剛過、還沒人開過老師頁時，缺席列還沒寫入，
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


# 每場的手動發送狀態，只在行程記憶體（跟既有限流一樣靠單 worker）。sid -> {"sending": bool, "sent_at": float|None}
_notify_state: dict[int, dict] = {}
# 「是否正在發送」與「設成發送中」要在同一把鎖裡完成，否則兩個同時進來的請求（同步端點跑在
# 執行緒池）可能都讀到「沒在發送」而各發一次，重複發送就從這個縫漏出去。
_notify_lock = threading.Lock()
NOTIFY_DEDUP_SECONDS = 120


@app.post("/api/t/session/{sid}/notify")
def api_notify(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    """手動（重新）把這次點名結果發到 Discord。
    伺服器同步等 Discord（每則最多 15 秒），前端逾時放棄之後老師常常再按一次——照做會把
    全班名單重發一遍。這裡記行程記憶體的發送狀態：正在發送中再收到同一場的請求就回 409；
    120 秒內已經成功發過「同樣內容」就回 200 又 again，不再真的送一次。不改 api_close／finalize_expired
    的背景發送（那條路徑本來就是 fire-and-forget，跟這裡的手動重發鎖無關）。
    只擋內容相同的重發——老師發完發現名單有錯、改完狀態再按，內容不同就要真的發；
    摘要文字只取開場時間，不含「現在」，同一份名單算出來的雜湊一定相同。"""
    require_teacher(req)
    with db() as con:
        channel, text = do_notify_prepare(con, sid)
    digest = hashlib.sha256(f"{channel}\n{text}".encode()).hexdigest()
    with _notify_lock:
        st = _notify_state.get(sid, {})
        if st.get("sending"):
            raise HTTPException(409, "上一次發送還在進行中，請先看 Discord 頻道")
        if st.get("sent_at") is not None and now() - st["sent_at"] < NOTIFY_DEDUP_SECONDS and st.get("digest") == digest:
            log_event(sid, "discord_sent", reason="manual")  # Discord 上的名單就是這一份，discord_stale 要以這次為界歸零
            return {"ok": True, "again": True}
        _notify_state[sid] = {"sending": True, "sent_at": st.get("sent_at"), "digest": st.get("digest")}
    ok_sent = False
    try:
        ok, msg = discord_post_long(channel, text)
        if not ok:
            # Cloudflare 對 origin 的 5xx 可能換成自己的錯誤頁，老師只看得到「伺服器忙線
            # （HTTP 502），再按一次」，看不到真正的失敗原因；改回 424（Failed Dependency，
            # 這裡的依賴是 Discord 本身），CF 不攔截 4xx。
            raise HTTPException(424, msg if msg.startswith("Discord") else f"Discord 發送失敗：{msg}")  # 新訊息已自帶「Discord …」開頭，不重複加前綴
        with db() as con:  # 老師確認後手動發出，暫緩提示就該消掉：兩種暫緩（出席率低／老師手動）
            # 一併清，背景發送失敗留下的 discord_failed 也一併清掉
            con.execute("UPDATE sessions SET note='' WHERE id=? AND note IN ('discord_held','discord_held_manual','discord_failed')", (sid,))
        log_event(sid, "discord_sent", reason="manual")
        ok_sent = True
        return {"ok": True}
    finally:
        with _notify_lock:
            _notify_state[sid] = {"sending": False, "sent_at": now() if ok_sent else st.get("sent_at"),
                                  "digest": digest if ok_sent else st.get("digest")}


@app.post("/api/t/session/{sid}/hold")
def api_hold(req: Request, sid: int = PathParam(ge=1, le=2**53), hold: str = Form(...)):
    """點名頁「這場先不要自動發 Discord」。
    跟出席不到一半的暫緩（note='discord_held'）分開記成 note='discord_held_manual'，
    這樣 #heldMsg 才講得出這場是老師自己按的還是出席率低。只接受一般場次、未作廢、進行中
    （open／late）；已經作廢的場次一定也已結案，跟「已結束」共用同一句 409 訊息，不必另外判。"""
    require_teacher(req)
    if hold not in ("0", "1"):
        raise HTTPException(400, "hold 參數不合法")
    with db() as con:
        s = get_session(con, sid)
        if s["kind"] != "normal":
            raise HTTPException(409, "綁定場次不發 Discord")
        if session_phase(s) == "closed":
            raise HTTPException(409, "點名已經結束，要發請按「發到 Discord」")
        if hold == "1":
            # 判定還沒結束之後、寫入之前被自動結案：不能回「已暫緩」卻讓 Discord 照發
            if con.execute("UPDATE sessions SET note='discord_held_manual' WHERE id=? AND closed_at IS NULL", (sid,)).rowcount == 0:
                raise HTTPException(409, "點名已經結束，要發請按「發到 Discord」")
        else:
            con.execute("UPDATE sessions SET note='' WHERE id=? AND note='discord_held_manual'", (sid,))
    return {"ok": True, "held": hold == "1"}


@app.post("/api/t/course/{cid}/bind")
def api_toggle_bind(req: Request, cid: int = PathParam(ge=1, le=2**53), allow: str = Form(...)):
    """點名頁上的「首次綁定」開關。"""
    require_teacher(req)
    with db() as con:
        c = get_course(con, cid)
        st = course_settings(c)
        st["allow_bind"] = allow == "1"
        con.execute("UPDATE courses SET settings=? WHERE id=?", (json.dumps(st), cid))
        log_event_on(con, _live_sid(con, cid), "allow_bind", reason="1" if st["allow_bind"] else "0")  # L8
    return {"ok": True, "allow_bind": st["allow_bind"]}


@app.post("/api/t/session/{sid}/delete")
def api_delete_session(req: Request, sid: int = PathParam(ge=1, le=2**53)):
    """整筆刪掉這次點名（誤開、測試用）。已結束的也能刪，報表不再計入。"""
    require_teacher(req)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        s = get_session(con, sid)
        n_rows = con.execute("SELECT COUNT(*) FROM checkins WHERE session_id=?", (sid,)).fetchone()[0]
        # 老師的解除綁定／重設、首次綁定開關事件不屬於這一場——「當天免首次綁定開關」看的是 unbind 事件，
        # 場次一刪就跟著消失，學生又被擋回去。留下來，場次號清成空。
        con.execute("UPDATE events SET session_id=NULL WHERE session_id=? AND kind IN ('unbind','allow_bind')", (sid,))
        for t in ("checkins", "pin_fail", "spotchecks", "events"):
            con.execute(f"DELETE FROM {t} WHERE session_id=?", (sid,))
        con.execute("DELETE FROM sessions WHERE id=?", (sid,))
        log_event_on(con, None, "session_deleted", reason=f"course:{s['course_id']} sid:{sid} rows:{n_rows}")
    # 場次號會被重開沿用（sessions.id 沒有 AUTOINCREMENT）——行程記憶體裡以這個場次號為鍵的狀態一起清，
    # 不然新場次第一次錯 PIN 就沿用舊場次的次數、還沒開投影頁就說「投影頁已經 N 秒沒收到 QR」。清單同結案時清的那份。
    for key in [k for k in list(_pin_fail_recent) if k[0] == sid]:
        _pin_fail_recent.pop(key, None)
    PROJECTOR_SEEN.pop(sid, None)
    with _notify_lock:
        _notify_state.pop(sid, None)
    return {"ok": True}


def do_void_session(con: sqlite3.Connection, sid: int, void: bool) -> None:
    """本場作廢但保留紀錄。只有已結案的場次可作廢／取消作廢；紀錄不動，只標記 voided_at，
    報表／CSV／`/api/me`（走共用定義 counts_toward_attendance）就會自動不再算這一場。
    作廢不再順手清掉 note='discord_held'——那樣取消作廢後提示就回不來了。改成所有讀
    discord_held 的地方（api_state）都額外要求 voided_at IS NULL，作廢時提示自然被蓋掉、
    取消作廢時提示自然回來，不用在這裡另外管一次 note。"""
    s = get_session(con, sid)
    if s["closed_at"] is None:
        raise HTTPException(409, "只有已經結束的場次可以作廢或取消作廢")
    con.execute("UPDATE sessions SET voided_at=? WHERE id=?", (now() if void else None, sid))


@app.post("/api/t/session/{sid}/void")
def api_void_session(req: Request, sid: int = PathParam(ge=1, le=2**53), void: str = Form(...)):
    require_teacher(req)
    with db() as con:
        do_void_session(con, sid, void == "1")
    return {"ok": True}


BULK_STATUS_CHOICES = ("present", "late", "excused")  # 拿掉 absent——缺席等於沒改狀態卻把 source='auto' 弄丟


def do_bulk_status(con: sqlite3.Connection, sid: int, to_status: str) -> int:
    """只把本場「自動缺席」（source='auto' AND status='absent'）的列改成 to_status，
    不動老師手動設的缺席、也不動學生自己簽的列。只有已結案的場次可用，回傳更正的筆數。
    SELECT 到 UPDATE 之間如果另一個分頁把某一列逐筆改成別的狀態（例如請假），
    先前只憑 SELECT 出來的 id 就整批 UPDATE，會把那筆剛改好的請假蓋回 to_status。
    每一列的 UPDATE 都重新加上 AND source='auto' AND status='absent'，落在這個縫隙裡的
    列不會再符合條件、不會被改到；回傳筆數改成每筆 UPDATE 實際 rowcount 的總和，不是
    SELECT 當下看到的筆數，這樣老師才知道真正改了幾筆。"""
    s = get_session(con, sid)
    if s["closed_at"] is None:
        raise HTTPException(409, "只有已經結束的場次可以整批更正")
    if to_status not in BULK_STATUS_CHOICES:
        raise HTTPException(400, "狀態不合法")
    rows = con.execute("SELECT id, flags, student_pk FROM checkins WHERE session_id=? AND source='auto' AND status='absent'", (sid,)).fetchall()
    n = 0
    for r in rows:
        flags = json.loads(r["flags"])
        if "manual" not in flags:
            flags.append("manual")
        cur = con.execute(
            "UPDATE checkins SET status=?, source='manual', flags=?, ts=0 WHERE id=? AND source='auto' AND status='absent'",
            (to_status, json.dumps(flags), r["id"]))
        n += cur.rowcount
        if cur.rowcount:  # 手動列的 ts 是 0，另記一筆事件當 compute_stuck 的底線
            log_event_on(con, sid, "manual_status", reason="bulk:" + to_status, student_pk=r["student_pk"])
    return n


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
        # 綁定場次也開放抽點（叫到不在的人會撤回綁定）
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
        # 綁定場次抽到不在場，那筆綁定很可能不是本人做的，撤回讓本人重新掃碼
        if not ok and ses["kind"] == "bind":
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL, pin_hash=NULL, pin_salt=NULL WHERE id=?",
                        (sc["student_pk"],))
            unbound = True
        k = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?",
                        (sc["session_id"], sc["student_pk"])).fetchone()
        if k:
            prior_flags = json.loads(k["flags"])
            # 先前被別人「不在」連坐（proxy_for）的人，這次抽點按「在」就是把連坐撤回——
            # 不能只是照舊留著 proxy_for，否則名單看起來還是被連坐的樣子。
            had_proxy = "proxy_for" in prior_flags
            drop = ("spot_ok", "spot_absent", "proxy_for") if ok else ("spot_ok", "spot_absent")
            flags = [f for f in prior_flags if f not in drop]
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
            elif had_proxy or status == "pending":
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
        # 讀（有沒有這一列）與寫（UPDATE／INSERT）放進同一個寫入交易；學生的簽到或另一個連按的請求不能插在中間
        # （這條連線到這裡還沒做過任何 UPDATE／INSERT／DELETE，沒有留下隱式交易）
        con.execute("BEGIN IMMEDIATE")
        s = get_session(con, sid)
        if s["kind"] != "normal":  # 綁定場次不計出缺勤，手動簽到走的是同一支端點，一併擋下
            raise HTTPException(409, "綁定場次不計出缺勤，要補登請另開一般點名")
        stu = con.execute("SELECT * FROM students WHERE id=? AND course_id=?", (pk, s["course_id"])).fetchone()
        if not stu:
            raise HTTPException(404, "學生不在名冊")
        k = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?", (sid, pk)).fetchone()
        chained = []
        if k and k["status"] == status:
            # 已經是這個狀態就什麼都不動（不改 source、不加 manual 旗標、不記事件）；
            # 否則按一下「出席」會把掃碼出席變成老師手動出席。其餘狀態轉換（含 pending→present）照舊。
            return {"ok": True, "unchanged": True}
        if k:
            old_flags = json.loads(k["flags"])
            # 老師把一位「抽點不在」的人改成缺席以外的狀態時，同一支手機因為代簽連坐被記缺席
            # 的其他人不會跟著自動改——只列出來讓老師自己判斷，不要替他決定要不要一起改。
            if "spot_absent" in old_flags and status != "absent" and k["device_hash"]:
                chained = [{"student_id": r["student_id"], "name": r["name"]} for r in con.execute(
                    "SELECT st2.student_id, st2.name FROM checkins k2 JOIN students st2 ON st2.id=k2.student_pk "
                    "WHERE k2.session_id=? AND k2.device_hash=? AND k2.id!=? AND k2.flags LIKE '%\"proxy_for\"%'",
                    (sid, k["device_hash"], k["id"])).fetchall()]
            flags = [f for f in old_flags if f not in ("spot_ok", "spot_absent", "proxy_for")]
            if "manual" not in flags:
                flags.append("manual")
            new_ts = 0 if k["source"] == "auto" else k["ts"]  # 自動缺席列改成請假／出席：沒有真正的簽到時間
            con.execute("UPDATE checkins SET status=?, flags=?, source='manual', ts=?, seat=COALESCE(NULLIF(?,''), seat) WHERE id=?",
                        (status, json.dumps(flags), new_ts, seat, k["id"]))
        else:
            con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, source) "
                        "VALUES(?,?,?,?,?,?,'manual')", (sid, pk, 0, seat, status, json.dumps(["manual"])))
        log_event_on(con, sid, "manual_status", reason=status, student_pk=pk)  # 兩條路徑都記
    out: dict[str, object] = {"ok": True}
    if chained:
        out["chained"] = chained
    return out


def _revoked_today(con: sqlite3.Connection, course_id: int, student_pk: int) -> bool:
    """同一門課今天（台灣時間 00:00 起）任何一場綁定場次裡，這個學號被抽點判過不在。
    do_checkin 的 bind_revoked 與 api_unbind 回傳的 revoked_today共用。"""
    return con.execute(
        "SELECT 1 FROM spotchecks sc JOIN sessions se ON se.id=sc.session_id "
        "WHERE se.course_id=? AND se.kind='bind' AND se.opened_at>=? AND se.voided_at IS NULL "  # 作廢＝整場不算
        "AND sc.student_pk=? AND sc.result='absent' LIMIT 1",
        (course_id, today_start(), student_pk)).fetchone() is not None


@app.post("/api/t/student/{pk}/unbind")
def api_unbind(req: Request, pk: int = PathParam(ge=1, le=2**53), reset_pin: str = Form("")):
    require_teacher(req)
    with db() as con:
        stu = con.execute("SELECT course_id FROM students WHERE id=?", (pk,)).fetchone()
        if not stu:
            raise HTTPException(404, "找不到這位學生")
        if reset_pin:
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL, pin_hash=NULL, pin_salt=NULL WHERE id=?", (pk,))
            # 重設等於學生要重新開始——本場（進行中的場次）的 PIN 失敗次數一併清掉，否則鎖定檢查
            # 排在 first_time 之前，學生照畫面首綁新 PIN 仍然 423。只解綁不重設時舊 PIN 還有效，鎖定留著防猜。
            con.execute("DELETE FROM pin_fail WHERE student_pk=? AND session_id IN (SELECT id FROM sessions WHERE closed_at IS NULL)", (pk,))
            for key in [k for k in list(_pin_fail_recent) if k[1] == pk]:
                _pin_fail_recent.pop(key, None)
        else:
            con.execute("UPDATE students SET device_hash=NULL, bound_at=NULL WHERE id=?", (pk,))
        log_event_on(con, _live_sid(con, stu["course_id"]), "unbind", reason="reset" if reset_pin else "unbind", student_pk=pk)  # L8
        revoked_today = _revoked_today(con, stu["course_id"], pk)
    # 名冊頁依這兩個旗標說對提示（今天被判不在不能再綁／重設／只解除綁定）
    return {"ok": True, "reset": bool(reset_pin), "revoked_today": revoked_today}


# 學生端

def set_device_cookie(resp, key: str):
    """所有下發裝置識別 cookie 的地方收斂到這一支，path 固定「/」、不設 domain。"""
    resp.set_cookie(DK_COOKIE, key, path="/", httponly=True, samesite="lax",
                    secure=BASE_URL.startswith("https"), max_age=400 * 86400)
    return resp


def _migrate_legacy_cookie(req: Request, resp, resolved_key: str):
    """這次回應如果是靠舊名 attend_dk 才認出裝置的（新名讀不到、舊名剛好等於解出的值），
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
    # 帶發證時間：出席／遲到判定看「掃到」這一刻，不是送出表單那一刻
    # 第五段帶掃到當下那支裝置的雜湊前 16 碼——憑證在下發時就綁裝置，轉貼給別支手機
    # 第一次使用就會被擋，不必等到 grants 表在「第一次使用」才記錄
    return make_token("g", str(sid), secrets.token_hex(6), str(now()), dh16, exp=now() + settings["grant_seconds"])


def _bound_checkin_for_device(con, course_id: int, sid: int, device_key: str) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    """跟 `/api/whoami` 同一套查法決定「這支手機目前綁定的學號」——
    `students.course_id=本課程 AND device_hash=hash_device(裝置識別)`，恰好一筆才算（0 筆或 2 筆以上
    都是歷史資料，不猜，回 None）。找到人之後才看這個學號在「本場」有沒有 `source!='auto'` 的簽到列，
    有才回 (學生列, 簽到列)。
    刻意不用「本場簽到列的 device_hash 等於這支手機」查——那樣會把借用者掛在同一支裝置上待確認的
    那一列，或老師解除綁定後這支裝置在本場留下的舊列也算進來，讓被解綁的裝置永遠回不到首次綁定
    的表單（第一次交件在這裡出的兩個真 bug：借用手機拿不到 grant、老師解綁後無法重綁，根因都在此）。"""
    if not device_key:
        return None
    dh = hash_device(device_key)
    rs = con.execute("SELECT * FROM students WHERE course_id=? AND device_hash=?", (course_id, dh)).fetchall()
    if len(rs) != 1:
        return None
    stu = rs[0]
    # 老師記成缺席／請假、或抽點判不在的列不算「已簽到」——回 None 讓落地頁照常給表單，
    # 學生一鍵送出才會進 do_checkin，老師頁才看得到「老師改後才掃碼」
    already = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=? AND source!='auto' "
                          "AND status IN ('present','late','pending')", (sid, stu["id"])).fetchone()
    if not already:
        return None
    return stu, already


def _marked_off(row) -> bool:
    """這一列是老師手動記的缺席／請假，或抽點判不在（source 仍是 qr、旗標 spot_absent）。"""
    return (row["source"] == "manual" and row["status"] in ("absent", "excused")) or \
        (row["status"] == "absent" and "spot_absent" in json.loads(row["flags"] or "[]"))


def _already_checked_ctx(stu: sqlite3.Row, already: sqlite3.Row, kind: str = "normal") -> dict:
    """把「已簽到」要顯示的欄位組成 template context 片段，簡易頁與借用頁共用。
    kind=='bind'（只做綁定的場次）不算出缺勤，標題改「已經綁定了」、狀態不寫「出席」。"""
    pending_note = ""
    if already["status"] == "pending":
        flags = json.loads(already["flags"]) if already["flags"] else []  # flags 一律經 json.dumps 寫入，不會壞
        pending_note = pending_reason_note(pending_reason_from_flags(flags))
    status_label = STATUS_LABEL.get(already["status"], already["status"])
    is_bind = kind == "bind"
    if is_bind:
        status_label = "已綁定（這一場不算出缺勤）"
        pending_note = pending_note or "今天還沒簽到成功的，請找老師補登。"
    return {
        "already_checked": True, "name": stu["name"], "stu_id": stu["student_id"],
        "status_label": status_label, "is_bind": is_bind,
        "ts": fmt(already["ts"]), "pending_note": pending_note,
    }


def render_student(req: Request, con, s: sqlite3.Row, c: sqlite3.Row, st: dict, error: str = "",
                   device_key: str | None = None, via_code: bool = False):
    """這次回應要下發的裝置識別，跟塞進 grant 第五段的裝置雜湊必須是同一個值——
    既有 cookie 就用它，沒有就這裡當場產生一個，讓 grant 與稍後 ensure_device_cookie_value
    下發的 cookie 用同一支裝置，不能先建 grant 再讓 cookie 各自隨機一個。
    呼叫端（student_entry）如果已經為了記 land_ok 先產生過一把，要傳進來沿用，
    不能各自各產生一把——否則事件表的裝置、grant 第五段、下發的 cookie 三者對不起來。
    `via_code`＝True 代表是從 /c（六位數）送過來的，截斷時的按鈕文字要叫人回上一頁，不是重新掃描 QR。"""
    device_key = device_key or effective_device_key(req, con) or secrets.token_urlsafe(24)
    dh = hash_device(device_key)
    dh16 = dh[:16]
    phase = session_phase(s)
    # 這支手機「目前綁定」的學號本場已經簽到過——場次結束就只回簡易的已簽到頁；
    #場次還在收簽到，改成已簽到 banner 疊在照常輸出的表單上面（表單先隱藏、
    # 不呼叫 init()），讓借用這支手機的人可以按「不是我，要借這支手機簽到」切到首綁流程。
    bound = _bound_checkin_for_device(con, s["course_id"], s["id"], device_key)
    if bound is not None:
        stu, already = bound
        already_ctx = _already_checked_ctx(stu, already, s["kind"])
        if phase == "closed":
            resp = templates.TemplateResponse(req, "student.html",
                {"closed": False, "expired": False, "c": c, "error": error, **already_ctx})
            return ensure_device_cookie_value(req, resp, device_key)
        resp = templates.TemplateResponse(req, "student.html", {
            "closed": False, "expired": False, "c": c, "s": s, "phase": phase,
            "grant": grant_for(s["id"], st, dh16), "error": error, "grant_seconds": st["grant_seconds"],
            "via_code": via_code, "already_checked_open": True, "sid": s["id"], **already_ctx,
            "session_left": max(0, int(s["late_until"] - now())),  # 倒數要看場次什麼時候結束
        })
        return ensure_device_cookie_value(req, resp, device_key)
    if phase == "closed":
        resp = templates.TemplateResponse(req, "student.html",
            {"closed": True, "expired": False, "c": c, "error": error})  # 結束頁也要下發裝置 cookie
        return ensure_device_cookie_value(req, resp, device_key)
    resp = templates.TemplateResponse(req, "student.html", {
        "closed": False, "expired": False, "c": c, "s": s, "phase": phase,
        "grant": grant_for(s["id"], st, dh16), "error": error, "grant_seconds": st["grant_seconds"],
        "via_code": via_code, "session_left": max(0, int(s["late_until"] - now())),
        "sid": s["id"],  # 前端回報在網址抓不到場次號（從 /c 進表單）時靠它
    })
    return ensure_device_cookie_value(req, resp, device_key)


def _stale_url_msg(con, s) -> str:
    """舊場次網址（這門課已有更新的場次）的 410 說明，依現在有沒有進行中的場次、是不是只做綁定的場次。"""
    live_now = con.execute(
        "SELECT kind FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>? ORDER BY id DESC LIMIT 1",
        (s["course_id"], now())).fetchone()
    if live_now is None:
        return "這個 QR 已經過期，請重新掃描投影幕上的 QR"
    if live_now["kind"] == "bind":
        return BIND_ONLY_CLOSED_MSG
    return "這是之前那一場的 QR，老師已經開了新的一場，請重新掃描投影幕上的 QR"


@app.get("/s/{sid}/{slot}/{sig}", response_class=HTMLResponse)
def student_entry(req: Request, sid: int = PathParam(ge=1, le=2**53), slot: int = PathParam(ge=0, le=2**53), sig: str = PathParam(max_length=64)):
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    with db() as con:
        dev_key = effective_device_key(req, con)  # 只查一次，供落地事件與後續共用
        s = get_session(con, sid)
        c = get_course(con, s["course_id"])
        st = course_settings(c)
        if not re.fullmatch(r"[0-9a-f]{10}", sig) or slot not in current_slots(s, st) \
                or not hmac.compare_digest(slot_sig(s, slot), sig):
            device8 = hash_device(dev_key)[:8] if dev_key else ""  # 過期路徑不動，維持原樣（沒 cookie 就是空字串）
            # 事件要等分流之後才記（簽章過期不等於「過期失敗」）——這支手機本場已簽到、回 200 收據的是
            # land_revisit；網址是已結束的場次（本場已 closed，或這門課有更新的場次）是 land_closed；
            # 只有真的回 410 過期頁、而且場次還在進行中，才是 land_expired。老師頁漏斗只數 land_expired。
            later = con.execute("SELECT 1 FROM sessions WHERE course_id=? AND opened_at>? LIMIT 1",
                                (s["course_id"], s["opened_at"])).fetchone()
            stale_url = later is not None or session_phase(s) == "closed"
            # QR 過期，但這支手機「目前綁定」的學號本場已經簽到過——
            # 仍回 200 顯示已簽到結果（不給表單、不給 GRANT、不給 #borrow，只多一行叫他重新掃 QR）
            if dev_key:
                bound = _bound_checkin_for_device(con, s["course_id"], sid, dev_key)
                if bound is not None:
                    # 只有這一場就是這門課最新的一場，才說「這一場已經簽到了」；網址裡是舊的場次號
                    # （上週的分頁）時，那句話是錯的，落到下面的 410 過期頁，說明依現在有沒有進行中的場次
                    if later:
                        log_event(sid, "land_closed", device8=device8, ua_class=ua_class)
                        return ensure_device_cookie(req, templates.TemplateResponse(req, "student.html", {
                            "closed": False, "c": c, "expired": True, "error": _stale_url_msg(con, s)}, status_code=410))
                    stu, already = bound
                    log_event(sid, "land_revisit", device8=device8, ua_class=ua_class)  # 已簽到的人回到舊的 QR 分頁
                    resp = templates.TemplateResponse(req, "student.html", {
                        "closed": False, "expired": False, "c": c,
                        "already_checked_expired_hint": True,
                        "error": "這個 QR 已經過期，請重新掃描投影幕上的 QR",
                        **_already_checked_ctx(stu, already, s["kind"]),
                    })
                    return ensure_device_cookie_value(req, resp, dev_key)
            log_event(sid, "land_closed" if stale_url else "land_expired", device8=device8, ua_class=ua_class)
            if later is not None:  # 這門課有更新的場次——說明依現在有沒有進行中的場次
                err = _stale_url_msg(con, s)
            elif stale_url:  # 這一場就是最新的一場、而且已結束——跟簽章仍有效時打開已結束場次同一個畫面（closed）；狀態碼維持 410
                return ensure_device_cookie(req, templates.TemplateResponse(req, "student.html", {
                    "closed": True, "expired": False, "c": c, "error": ""}, status_code=410))
            else:
                err = "這個 QR 已經過期，請重新掃描投影幕上的 QR"
            return ensure_device_cookie(req, templates.TemplateResponse(req, "student.html", {
                "closed": False, "c": c, "expired": True, "error": err},
                status_code=410))  # 過期頁也要下發裝置 cookie，否則沒 cookie 的人一路退到共用的指紋限流桶
        # 第一次來的學生沒有 cookie，dev_key 是空的——落地事件記不到裝置，「同一支手機最早的
        # land_ok」對他們永遠查不到。簽章驗過、確定要記 land_ok 之前先產生一把裝置金鑰，
        # 用它算 device8，再把同一把傳給 render_student，讓事件、grant 第五段、下發的 cookie 三者一致。
        nocookie = not dev_key
        if nocookie:
            # 沒帶 cookie 的落地以 IP（IPv6 為 /64）限流，每分鐘 120 次；帶 cookie 的不受限。
            # 超過回 429、不記 land_ok，免得有人洗事件、灌爆「還有 N 支在填表」
            if _throttle(_checkin_hits, "land:" + rate_ip(req), 120, 60):
                raise HTTPException(429, "太頻繁，等一下再掃")
            dev_key = secrets.token_urlsafe(24)
        device8 = hash_device(dev_key)[:8]  # 掃碼落地事件
        # 結束後才掃到的落地記 land_closed——不能算進漏斗的「掃到」，也不能被當成「落地了卻沒送出」
        log_event(sid, "land_closed" if session_phase(s) == "closed" else "land_ok",
                  reason="nocookie" if nocookie else "", device8=device8, ua_class=ua_class)
        return render_student(req, con, s, c, st, device_key=dev_key)


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
        log_event(None, "me_throttle", device8=hash_device(device_key)[:8], ua_class=classify_ua(req.headers.get("user-agent", "")))  # L4
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
            # counts／rate 算完之後才補上「進行中、這位學生本場已有簽到列」的一般場次，
            # 讓剛簽到成功的人看得到這一場，但不能算進出席率——場次還沒結束，遲到窗都還沒過。
            live_sessions = con.execute(
                "SELECT * FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>? "
                f"AND {COUNTED_SESSION_SQL} ORDER BY opened_at", (c["id"], now())).fetchall()
            for ls in live_sessions:
                lk = con.execute("SELECT status, ts, source FROM checkins WHERE session_id=? AND student_pk=?",
                                 (ls["id"], stu["id"])).fetchone()
                if not lk:
                    continue
                items.append({"date": fmt(ls["opened_at"], True), "status": lk["status"], "label": STATUS_LABEL[lk["status"]],
                              "ts": fmt(lk["ts"]) if (lk["ts"] or 0) > 0 and lk["source"] != "auto" else "", "live": True})
            out.append({"course": c["name"], "student_id": stu["student_id"], "name": stu["name"],
                        "items": items, "counts": counts, "rate": rate})
    return {"courses": out}


@app.get("/help", response_class=HTMLResponse)
def help_page(req: Request):
    return templates.TemplateResponse(req, "help.html", {})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """沒有這支路由時每支手機都會多打一個 404。204＋長快取，不是個人化內容，不受全站 no-store 影響。"""
    return Response(status_code=204, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/c", response_class=HTMLResponse)
def code_page(req: Request):
    return ensure_device_cookie(req, templates.TemplateResponse(req, "code.html", {"error": ""}))


_code_fail: dict[str, list[float]] = {}
_checkin_hits: dict[str, list[float]] = {}
# (sid, student_pk, device_hash) -> (該次錯 PIN 的雜湊, 時間, 當時算出的次數 n)，行程記憶體。
_pin_fail_recent: dict[tuple, tuple[str, float, int]] = {}
PIN_FAIL_DEDUP_SECONDS = 60


def _throttle(bucket: dict, key: str, limit: int, window: float) -> bool:
    """回 True 代表超過限制。行程內記憶體，前提是單一 worker（但單 worker 內仍可能有多條
    執行緒同時碰同一個 bucket——背景發 Discord、log_event 都各自起執行緒；整個函式體
    放進 _throttle_lock，避免一條執行緒走訪 bucket.items() 時另一條插入新鍵撞
    RuntimeError: dictionary changed size during iteration。持鎖時間極短，只有字典操作。"""
    with _throttle_lock:
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
    ip = rate_ip(req)  # IPv6 以 /64 計
    device_key = effective_device_key(req)
    has_device = bool(device_key)
    device8 = hash_device(device_key)[:8] if device_key else ""  # 六位數落地事件
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    fpk = device_key or ("fp:" + fingerprint(req))  # 有裝置 cookie 就以裝置計；沒有才退到 IP＋UA 指紋
    # 每支手機 10 次/分（只算失敗）、整個出口 300 次/分：整班同一校園 NAT 不會互相鎖，機器連猜仍被擋
    # 沒有 cookie 時大家都共用同一把 fp 桶（同 IP 同 UA），額度要放寬到跟每 IP 一樣，
    # 否則全班第一次落地（還沒拿到 cookie）就會互相把彼此鎖住
    b_limit = 10 if has_device else 300

    def throttled():
        log_event(None, "code_throttle", device8=device8, ua_class=ua_class)  # L2
        return ensure_device_cookie(req, templates.TemplateResponse(
            req, "code.html", {"error": "輸錯太多次，等一分鐘，或直接掃投影幕的 QR、找老師手動簽到"}, status_code=429))

    if len([t for t in _code_fail.get("b:" + fpk, []) if now() - t < 60]) >= b_limit:
        return throttled()
    # 每出口桶只擋「錯碼」，而且已綁裝置不受它限制（同簽到的作法）；正確的代碼一律放行
    ip_full = len([t for t in _code_fail.get("ip:" + ip, []) if now() - t < 60]) >= 300
    with db() as con:
        live = con.execute("SELECT * FROM sessions WHERE closed_at IS NULL AND late_until>?", (now(),)).fetchall()
        for s in live:
            c = get_course(con, s["course_id"])
            st = course_settings(c)
            for slot in current_slots(s, st):
                if hmac.compare_digest(code6(slot_sig(s, slot)), code):
                    log_event(s["id"], "code_ok", device8=device8, ua_class=ua_class)
                    return render_student(req, con, s, c, st, via_code=True)
        if ip_full and not (has_device and con.execute(
                "SELECT 1 FROM students WHERE device_hash=? LIMIT 1", (hash_device(device_key),)).fetchone()):
            return throttled()
        if not live:
            log_event_on(con, None, "code_no_live", device8=device8, ua_class=ua_class)  # L1
            return ensure_device_cookie(req, templates.TemplateResponse(
                req, "code.html", {"error": "目前沒有進行中的點名，等老師開始再輸"}, status_code=400))
    for key in ("b:" + fpk, "ip:" + ip):
        _throttle(_code_fail, key, 10**9, 60)  # 只記錄失敗次數（上限交給前面的判斷）
    # 只有一場進行中就掛在那一場（老師頁漏斗看得到）；兩場以上不確定是哪一場
    log_event(live[0]["id"] if len(live) == 1 else None, "code_bad", device8=device8, ua_class=ua_class)
    return ensure_device_cookie(req, templates.TemplateResponse(
        req, "code.html", {"error": "代碼不對或已過期，請看投影幕重新輸入"}, status_code=400))


def _valid_device_cookie(ck: str) -> bool:
    return bool(ck) and len(ck) >= 16 and ck.isascii() and ck.replace("-", "").replace("_", "").isalnum()


def effective_device_key(req: Request, con: sqlite3.Connection | None = None) -> str:
    """只認伺服器下發的裝置 cookie（httponly，Safari 不會七天清掉）。前端自報的值一律不採：
    否則不帶 cookie 就能每次換身分替全班簽到。沒有 cookie 就回空字串。
    新名（DK_COOKIE）優先；讀不到時，只有在雙名模式（https，新舊名不同）才退回舊名
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
    """學生頁面第一次載入就下發裝置識別（或把已綁定的舊名裝置補發成新名）。"""
    existing = effective_device_key(req)
    if existing:
        return _migrate_legacy_cookie(req, resp, existing)
    return set_device_cookie(resp, secrets.token_urlsafe(24))


def ensure_device_cookie_value(req: Request, resp, device_key: str):
    """同 ensure_device_cookie，但沿用呼叫端已經決定好的 device_key（render_student 要讓
    grant 裡塞的裝置雜湊跟這裡下發的 cookie 是同一個值，不能各自各生一個隨機值）。"""
    existing = effective_device_key(req)
    if existing:
        return _migrate_legacy_cookie(req, resp, existing)
    return set_device_cookie(resp, device_key)


@app.post("/api/whoami")
def api_whoami(req: Request, course_id: int = Form(..., ge=1, le=2**53), device_key: str = Form("", max_length=200)):
    device_key = effective_device_key(req)
    device8 = hash_device(device_key)[:8] if device_key else ""
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    r = None
    pin_only: dict | None = None
    pin_locked = False
    if not device_key:
        outcome = "no_cookie"
    elif _throttle(_checkin_hits, "who:" + device_key, 30, 60):
        outcome = "throttle"
    else:
        outcome = "unknown"
    with db() as con:
        live_sid = _live_sid(con, course_id)
        if outcome == "unknown":
            rs = con.execute("SELECT id, student_id, name FROM students WHERE course_id=? AND device_hash=?",
                             (course_id, hash_device(device_key))).fetchall()
            r = rs[0] if len(rs) == 1 else None  # 同一支手機綁到兩個學號（歷史資料）就不猜
            if r:
                outcome = "known"
            elif live_sid is not None:
                # 這支裝置沒綁任何學號，卻在這門課進行中的場次對某個學號打過 PIN（被拒）——
                # 只回給同一支裝置，前端直接進只輸 PIN 模式；多個學號取最近一次被拒的那個
                pf = con.execute("SELECT student_pk, n, rowid AS rid FROM pin_fail WHERE session_id=? AND device_hash=? AND n>0",
                                 (live_sid, hash_device(device_key))).fetchall()
                if pf:
                    def _recent(x):
                        last = con.execute("SELECT MAX(id) FROM events WHERE session_id=? AND kind='checkin_reject' "
                                           "AND student_pk=? AND device8=?", (live_sid, x["student_pk"], device8)).fetchone()[0]
                        return (last or 0, x["rid"])
                    best = max(pf, key=_recent)
                    bst = con.execute("SELECT student_id FROM students WHERE id=?", (best["student_pk"],)).fetchone()
                    if bst:
                        pin_only = {"student_id": bst["student_id"]}
                        pin_locked = best["n"] >= course_settings(get_course(con, course_id))["pin_max_fail"]
    # L3：決定學生看到首綁還是回簽的就是這一步；on 區塊結束（連線 commit、關閉）之後才記，不跟還沒 commit 的連線互鎖
    log_event(live_sid, "whoami", reason=outcome, device8=device8, ua_class=ua_class, student_pk=r["id"] if r else None)
    if not r:
        if pin_only:
            return {"known": False, "pin_only": pin_only, **({"locked": True} if pin_locked else {})}
        return {"known": False}
    return {"known": True, "student_id": r["student_id"], "name": r["name"]}


_beacon_hits: dict[str, list[float]] = {}
_beacon_drop_log: dict[str, list[float]] = {}  # 同一個限流鍵每分鐘最多記一筆 beacon_dropped


@app.post("/api/beacon", status_code=204)
def api_beacon(req: Request, kind: str = Form(""), msg: str = Form("", max_length=2000), sid: str = Form("", max_length=32)):
    """前端錯誤回報：navigator.sendBeacon 打這支，故意不回錯誤內容、一律 204，
    不得因為回報失敗又讓學生端多一次例外。超過限流就丟掉、不噴錯；每個限流鍵每分鐘留一筆 beacon_dropped 事件。"""
    if kind not in ("jserror", "slow", "neterr", "whoami_fail"):  # L7：neterr＝送出時網路中斷、whoami_fail＝兩次 whoami 都失敗
        return Response(status_code=204)
    device_key = effective_device_key(req)
    ip = rate_ip(req)
    device8 = hash_device(device_key)[:8] if device_key else ""
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    sid_int = int(sid) if sid.isascii() and sid.isdigit() else None  # 「²」「١٢」isdigit() 為真但 int() 會炸
    # 以前被限流丟掉的回報完全無聲，最需要看見的那些人（整班同時網路壞掉）正好最先被丟；
    # 上限對齊簽到端點對未綁裝置的 IP（240），丟掉時每個限流鍵每分鐘留一筆 beacon_dropped 讓報告看得到
    limits = [("ip:" + ip, 240, "ip")]
    if device_key:
        limits.insert(0, ("d:" + device_key, 6, "device"))
    for limit_key, limit, why in limits:
        if _throttle(_beacon_hits, limit_key, limit, 60):
            if not _throttle(_beacon_drop_log, limit_key, 1, 60):
                log_event(sid_int, "beacon_dropped", reason=why, device8=device8, ua_class=ua_class)
            return Response(status_code=204)
    reason = f"{kind}:{msg}"[:200]
    log_event(sid_int, "client", reason=reason, device8=device8, ua_class=ua_class)
    return Response(status_code=204)


class Reject(HTTPException):
    """簽到被拒：多帶一個 slug 給事件記錄用，回應內容與狀態碼跟原本的 HTTPException 完全一樣，
    一個位元組都不變——slug 只在 api_checkin 內部攔截記錄時讀，不會出現在回應裡。"""

    pin_count: tuple[int, bool, int, bool, int] | None = None  # (累計次數, 是否沿用去重沒累加, 計次的學生 pk, 是否 variant, pin_max_fail)

    def __init__(self, status_code: int, detail: str, slug: str):
        super().__init__(status_code=status_code, detail=detail)
        self.slug = slug


def _claimed_pk(sid: int, student_id: str, name: str) -> int | None:
    """被擋在查名冊之前的拒絕（closed／grant_expired／no_cookie／grant_other_device／throttle／checkin_error）
    事件的 student_pk 本來是空的，tried、卡住名單、session_report 都看不到這些有到課的人。呼叫端只在 grant 簽章驗過
    （場次號可信）時才叫這支，用學號查名冊歸戶；學號打錯、或表單帶了姓名卻對不上，一律 None（不知道是誰，
    不能把別人的拒絕記在同學身上）。唯讀、自己開短逾時連線；任何失敗（含 database is locked）
    都回 None、不得往外拋——這是 except 分支裡的補記，不能讓診斷害主流程多炸一次。"""
    try:
        con = connect(timeout=0.5)
        try:
            ses = con.execute("SELECT course_id FROM sessions WHERE id=?", (sid,)).fetchone()
            if not ses:
                return None
            stu = con.execute("SELECT id, name FROM students WHERE course_id=? AND student_id=?",
                              (ses["course_id"], normalize_student_id(student_id))).fetchone()
        finally:
            con.close()
    except (sqlite3.Error, OverflowError, ValueError):
        return None
    if not stu or (name.strip() and name_matches(name, stu["name"]) != "exact"):  # 同姓同字數也不歸戶
        return None
    return stu["id"]


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


def pending_reason_from_flags(flags: list[str]) -> str | None:
    """待確認要說對原因，多個旗標同時成立時依這個順序取第一個：
    name_variant → same_device → device_bound_other。找不到就回 None。"""
    for reason in ("name_variant", "same_device", "device_bound_other", "same_fp"):
        if reason in flags:
            return reason
    return None


def pending_reason_note(reason: str | None) -> str:
    """待確認收據的第二行說明——姓名寫法不同跟「這支手機簽過別的學號」是不一樣的情況。"""
    if reason == "name_variant":
        return ("你填的姓名和名冊的寫法不完全一樣。請先看上面的學號和姓名是不是你本人："
                "不是的話馬上舉手找老師（多半是學號打錯）；是的話老師會當場確認，確認前不算出席。")
    if reason == "same_fp":
        return "系統看到同一個網路、同一種瀏覽器剛替別的學號簽過，老師會當場確認，確認前不算出席。"
    return "系統看到這支手機簽過別的學號，老師會當場確認，確認前不算出席。"


def _resend_receipt_for_device(con: sqlite3.Connection, sid: int, dh: str, student_id: str, device_key: str = "") -> tuple[dict, int] | None:
    """已經簽到的人重送，一律先回收據——不論憑證是否已過期、場次是否已結束，只要這一支裝置
    在這一場已經有這個學號的非 auto 簽到列，就回原本的收據；否則回 None（呼叫端照原本的 410 走）。
    只認 session_id＋device_hash＋student_pk 三者都對上的列，不得讓別支裝置或不同學號拿到收據。
    狀態是待確認時也帶 pending_reason，跟首次成功那筆一致。
    場次已結束時，簽到列的裝置對不上也有一條路——老師手動記的出席列 device_hash 是空的，學生用已綁定的那支手機送表單，
    本來會落到「點名已經結束」。這時改看「這支手機目前綁定的是不是這個學號、本場有沒有 present／late／pending 的非 auto 列」
    （`_bound_checkin_for_device`，跟落地頁同一套）；綁在別支手機、老師記缺席的人查不到，照舊。"""
    s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        return None
    stu = con.execute("SELECT * FROM students WHERE course_id=? AND student_id=?",
                      (s["course_id"], normalize_student_id(student_id))).fetchone()
    if not stu:
        return None
    k = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=? AND device_hash=? AND source!='auto'",
                    (sid, stu["id"], dh)).fetchone()
    if not k and session_phase(s) == "closed":
        bound = _bound_checkin_for_device(con, s["course_id"], sid, device_key)
        if bound is not None and bound[0]["id"] == stu["id"]:
            k = bound[1]
    if not k:
        return None
    payload = {"ok": True, "again": True, "name": stu["name"], "student_id": stu["student_id"],
              "ts": fmt(k["ts"]), "seat": k["seat"], "status": STATUS_LABEL[k["status"]], "kind": s["kind"]}
    if k["status"] == "pending":
        flags = json.loads(k["flags"]) if k["flags"] else []  # flags 一律經 json.dumps 寫入，不會壞
        # status 只有跟三個旗標之一同時成立才會是 pending（見 do_checkin），這裡一定找得到原因
        payload["pending_reason"] = pending_reason_from_flags(flags)
    return payload, stu["id"]


@app.post("/api/checkin")
def api_checkin(req: Request, grant: str = Form(..., max_length=200), device_key: str = Form("", max_length=200),
                student_id: str = Form(..., max_length=32), name: str = Form("", max_length=64), pin: str = Form("", max_length=16),
                pin2: str = Form("", max_length=16), seat: str = Form("", max_length=8), hints: str = Form("", max_length=300),
                name_hint: str = Form("", max_length=64)):
    # 薄殼：主邏輯在 do_checkin。mutmut 3 會跳過帶裝飾器的函式，留在這裡就永遠進不了突變測試
    return do_checkin(req, grant, student_id, name, pin, pin2, seat, hints, name_hint)


def do_checkin(req: Request, grant: str, student_id: str, name: str, pin: str, pin2: str, seat: str, hints: str, name_hint: str = ""):
    device_key = effective_device_key(req)
    ip = client_ip(req)
    dh = hash_device(device_key) if device_key else ""
    device8 = dh[:8]
    ua_class = classify_ua(req.headers.get("user-agent", ""))
    sid = _grant_sid_hint(grant)
    sid_verified = False  # grant 簽章驗過（場次號可信）才敢用學號歸戶，竄改過的 grant 不歸
    stu_pk: int | None = None

    def _ok(status: str, con: sqlite3.Connection | None = None) -> None:
        """con 有給就跟主流程共用同一條連線、同一次 commit——早退路徑此時 con 還握著沒 commit 的寫入，
        另開一條連線會跟它互鎖等到 busy timeout（事件記錄曾因此造成 database is locked）。
        con 沒給（with 區塊已經跑完、con 已關閉）才用獨立連線，這種情況不會撞鎖。"""
        if con is not None:
            log_event_on(con, sid, "checkin_ok", reason=status, device8=device8, ua_class=ua_class, student_pk=stu_pk)
        else:
            log_event(sid, "checkin_ok", reason=status, device8=device8, ua_class=ua_class, student_pk=stu_pk)

    try:
        # g, sid, nonce, issued, dh16（四段式與更舊的三段式憑證都沒有裝置雜湊那一段，一律視為無效不相容）
        parts = read_token(grant, 5)
        if not parts or parts[0] != "g":
            # 簽章仍有效、只是時效過了（不是被竄改）時，同裝置在這一場若已有這個學號的
            # 非 auto 簽到列，回原本的收據而不是 410——不可只靠 _grant_sid_hint 這種沒驗簽的
            # 字串切割，這裡驗過簽章、裝置雜湊前 16 碼與憑證第五段相符，才去查 checkins。
            # 簽章有效的過期憑證，場次號也是可信的（拒絕事件可以用學號歸戶）。
            ig = read_token_ignore_expiry(grant, 5)
            ig_sid = None
            if ig and ig[0] == "g":
                try:
                    ig_sid = int(ig[1])
                except ValueError:
                    ig_sid = None
                if ig_sid is not None:
                    sid, sid_verified = ig_sid, True
            if device_key and ig_sid is not None and ig is not None and hmac.compare_digest(ig[4], dh[:16]):
                with db() as con2:
                    resend = _resend_receipt_for_device(con2, ig_sid, dh, student_id, device_key)
                if resend is not None:
                    payload, resend_pk = resend
                    stu_pk = resend_pk
                    _ok(payload["status"])
                    return with_device_cookie(req, payload, device_key)
            raise Reject(410, "填表時間已到，請重新掃描 QR", "grant_expired")
        sid = int(parts[1])
        sid_verified = True
        try:
            issued = float(parts[3])
        except ValueError:
            raise Reject(410, "填表時間已到，請重新掃描 QR", "grant_expired") from None
        grant_dh16 = parts[4]
        if not device_key:
            raise Reject(400, "瀏覽器沒有保存簽到識別（無痕模式或封鎖 cookie），請改用一般模式重新掃描 QR", "no_cookie")
        # 憑證下發時就綁裝置，這是第一層防護——比 grants 表在「第一次使用」才記錄快一步，
        # 擋住教室內把 GRANT 字串轉貼給場外的人這種攻擊（那份表單本來就是這支裝置掃到的）。
        # 這第一道裝置檢查也會被「同一支手機兩個落地請求並行」（重掃、開兩個分頁）誤觸——
        # 晚到的 cookie 蓋掉早的，本人在另一分頁送出反而看到「別支手機」，像在指控。訊息改中性，
        # 不猜測原因；事件原因碼 grant_other_device 不變（老師頁漏斗看的是碼，不是這句人話）。
        if not hmac.compare_digest(grant_dh16, dh[:16]):
            raise Reject(410, "這份簽到表單和這支手機對不上（可能重新整理過或開了兩個分頁），請重新掃描 QR", "grant_other_device")
        seat = seat.strip()[:8]  # 學生沒有固定座位，此欄保留但不強制
        # PIN 格式檢查搬到「確定需要 PIN」之後（已綁裝置的裝置 cookie 對上時完全不驗 PIN）——
        # 這裡不能再無條件擋格式，得先知道是不是同一支已綁裝置才能判斷要不要驗。
        fp = fingerprint(req, hints)
        # 一支手機一分鐘最多 12 次：擋亂試 PIN，開資料庫之前先擋
        if _throttle(_checkin_hits, "d:" + dh, 12, 60):
            raise Reject(429, "送太多次了，等一分鐘再試", "throttle")
        with db() as con:
            # 每出口 IP 一分鐘 240 次只算未綁裝置——已綁裝置的回簽不因整班同一個校園 NAT
            # 被攻擊者灌爆而連坐擋下；查詢放在 BEGIN IMMEDIATE 之前，不佔寫入鎖
            bound_elsewhere = con.execute("SELECT 1 FROM students WHERE device_hash=? LIMIT 1", (dh,)).fetchone() is not None
            if not bound_elsewhere and _throttle(_checkin_hits, "ip:" + rate_ip(req), 240, 60):
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
            # 讀後寫放進同一個寫入交易——grant 處理（含它自己的 commit）之後、讀 sessions／
            # students／other／same 之前才 BEGIN IMMEDIATE，其後的讀與寫全程在這個交易鎖裡，
            # 兩個執行緒不會都讀到「這支裝置還沒被別人綁走」的舊結果
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise Reject(410, "老師已取消這次點名，等重新開始再掃", "closed")
            s = get_session(con, sid)
            # 場次 id 被刪除重開會沿用同一個 id；用舊表單（issued 早於這次真正開場
            # 的 opened_at）送出，不能被新場次收下——否則刪除前掃到的舊 QR、刪除後重開前送出的
            # 舊表單，都會被算進重開後的新場次。
            if issued < s["opened_at"]:
                raise Reject(410, "這份簽到表單是之前那一場的，請重新掃描 QR", "grant_expired")
            c = get_course(con, s["course_id"])
            st = course_settings(c)
            phase = session_phase(s)
            if phase == "closed":
                # 場次已結束也一樣先回收據——grant_dh16 在上面已經驗過等於 dh，這裡不用重驗裝置
                resend = _resend_receipt_for_device(con, sid, dh, student_id, device_key)
                if resend is not None:
                    payload, resend_pk = resend
                    stu_pk = resend_pk
                    _ok(payload["status"], con)
                    return with_device_cookie(req, payload, device_key)
                # 綁定場次換一般點名時，舊表單只說「點名已結束」不夠——同一門課如果已經有
                # 新的一場在進行，講清楚原因並叫他重新掃描投影幕，而不是讓他以為是自己操作有問題。
                next_live = con.execute(
                    "SELECT kind FROM sessions WHERE course_id=? AND closed_at IS NULL AND late_until>? LIMIT 1",
                    (c["id"], now())).fetchone()
                if next_live:
                    if next_live["kind"] == "bind":  # 一般點名剛結束就開只做綁定，不能叫人去掃那一場（拿到「綁」章、出缺勤仍缺席）
                        raise Reject(410, BIND_ONLY_CLOSED_MSG, "closed")
                    raise Reject(410, "這一場已經結束，老師開了新的一場，請重新掃描投影幕上的 QR", "closed")
                raise Reject(410, "點名已經結束。有到課但沒簽到的同學，請直接找老師。", "closed")
            stu = con.execute("SELECT * FROM students WHERE course_id=? AND student_id=?",
                              (c["id"], normalize_student_id(student_id))).fetchone()  # 與前端同一套全形→半形規則
            NOT_MATCH = f"學號或姓名與【{c['name']}】的名冊不符。請先確認學號沒打錯、姓名照名冊寫全名；用六位數輸到別班的，請回上一頁重輸；剛加選的請找老師"
            if not stu:
                raise Reject(400, NOT_MATCH, "not_match")  # 與姓名不符同一句：外人猜不出哪些學號存在
            stu_pk = stu["id"]  # student_pk 只有從名冊查到人才填，這裡起就填得到
            pf = con.execute("SELECT n FROM pin_fail WHERE session_id=? AND student_pk=? AND device_hash=?", (sid, stu["id"], dh)).fetchone()
            pf_all = con.execute("SELECT n FROM pin_fail WHERE session_id=? AND student_pk=? AND device_hash=''", (sid, stu["id"])).fetchone()
            # 本人已綁的那支手機平日不驗 PIN，PIN 鎖定也就不該擋到它——
            # 否則別人從別支裝置亂猜到全場上限，就能讓本人這堂課簽不了到
            same_device_as_bound = stu["device_hash"] is not None and hmac.compare_digest(stu["device_hash"], dh)
            if name.strip() and not same_device_as_bound and not name_matches(name, stu["name"]):
                stu_pk = None  # 姓名對不上就不知道是誰，被擋這一筆不能歸到這個學號
                raise Reject(400, NOT_MATCH, "not_match")
            if not same_device_as_bound and (
                    (pf and pf["n"] >= st["pin_max_fail"]) or (pf_all and pf_all["n"] >= PIN_MAX_FAIL_TOTAL)):
                raise Reject(423, PIN_LOCKED_MSG, "pin_locked")
            flags: list[str] = []
            variant_only = False  # 非首次分支，表單姓名只是「同姓同字數」（不是名冊上的全名）
            first_time = stu["pin_hash"] is None
            # 判不在（撤回綁定）之後，這個學號「今天」都不能再綁——不只擋回同一場。
            # 只查「本場」的 spotchecks 不夠：同一天接著開的一般點名若剛好開著首次綁定，
            # 代綁者換瀏覽器（device_hash 對這個學號來說本來就是 None）就能把學號綁回去，兩秒前的撤回
            # 形同白做。所以不論現在這場是綁定還是一般點名，只要這個學號目前沒有綁定，且同一門課
            # 今天（台灣時間 TZ 當日 00:00 起）任何一場綁定場次裡，這個學號被抽點判過不在，一律擋下。
            # 代價：老師按錯「不在」，這個學號當天都綁不了，只能下次上課再綁（見下面訊息）。
            # 這段排在首次綁定開關檢查之前，「判不在、今天不能再綁」的訊息優先（判不在時 PIN 一併清掉，
            # 學號回到 first_time，不排前面的話只會看到「不開放首次綁定」）。
            if stu["device_hash"] is None and _revoked_today(con, c["id"], stu["id"]):
                raise Reject(403, "老師今天叫名時這個學號不在場，今天不能再綁定。人在教室請直接找老師，下次上課再綁", "bind_revoked")
            # 不論課程的 allow_bind 設定，綁定場次一律允許首次綁定
            # 今天被老師解除綁定或重設過的學號不受這個開關限制
            #（unbind 事件由 api_unbind 在同一個交易寫入，session_id 可能是 None，所以只用 student_pk 與時間查）
            # 首次綁定開關只管從沒設過 PIN 的人；已設過 PIN、目前沒綁裝置的學號
            # （老師只按「解除綁定」）往下走一般驗 PIN 的流程，PIN 對就綁到這支。「當天」豁免
            # 只剩重設（PIN 被清掉）的人用得到。
            if first_time and not st["allow_bind"] and s["kind"] != "bind" \
                    and not con.execute("SELECT 1 FROM events WHERE kind='unbind' AND student_pk=? AND ts>=? LIMIT 1",
                                        (stu["id"], today_start())).fetchone():
                raise Reject(403, "這門課目前不開放首次綁定手機，請找老師當場打開後再簽", "bind_closed")
            # 這支手機是不是已經是別人的、或本場已經簽過別的學號，要在「要不要寫 PIN、要不要綁裝置」
            # 之前就先知道——首次綁定被這兩種情況擋下時，兩者都不能做（不然代簽者的自訂 PIN 會側寫進被代簽者帳號）
            other = con.execute("SELECT id FROM students WHERE device_hash=? AND id!=? AND student_id!=?",
                                (dh, stu["id"], stu["student_id"])).fetchone()  # 跨課也算：這支手機已是別人的（同一人修兩門課不算）
            same = con.execute("SELECT id, flags, source FROM checkins WHERE session_id=? AND device_hash=? AND student_pk!=?",
                               (sid, dh, stu["id"])).fetchone()
            if first_time:
                if not (pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 6):
                    raise Reject(400, "PIN 要 4 到 6 位數字", "pin_format")
                name_match = name_matches(name, stu["name"])  # 完全相同或「字數相同且第一個字相同」都放行
                if not name_match:
                    raise Reject(400, NOT_MATCH, "not_match")
                if pin != pin2:
                    raise Reject(400, "兩次 PIN 不一樣", "pin_mismatch2")
                if not (other or same):  # 手機是別人的、或本場已簽過別的學號，不寫 PIN、也不綁裝置
                    salt = secrets.token_hex(8)
                    con.execute("UPDATE students SET pin_hash=?, pin_salt=? WHERE id=?", (hash_pin(pin, salt), salt, stu["id"]))
                flags.append("late_bind")
                if name_match == "variant":
                    flags.append("name_variant")
            else:
                # 送出請求的裝置 cookie 雜湊＝該學號已綁的 device_hash 時，平日回簽不驗 PIN——
                # 不要求格式、不比對、多餘的 pin 一律忽略、不計失敗次數。其餘情況（換手機、解綁後、
                # 手機是別人的）PIN 規則不變。
                if not same_device_as_bound:
                    # 首綁表單帶了姓名，就要對得上這個學號的名冊姓名——學號末碼打成同學的，
                    # 不能把 PIN 錯算在同學名下、鎖住同學，更不能（PIN 剛好一樣時）搬走同學的綁定。
                    # 只輸 PIN 模式前端送空字串，不比對；不計 PIN 失敗、不寫 pin_fail。
                    # 完全對不上的那一段已提前到 pin_locked 之前（上面）；
                    # 剩下「同姓同字數」的只算半個本人——被擋的事件不歸到那位同學（stu_pk=None），
                    # pin_fail 照寫照計（猜 PIN 上限不能失效），PIN 對了寫簽到列時轉待確認（name_variant）。
                    if name.strip() and name_matches(name, stu["name"]) == "variant":
                        variant_only = True
                    # 切成只輸 PIN 後 name 是空的，前端另帶 name_hint（切換前表單上打的姓名）；
                    # 提示對不上這位同學的全名（同姓同字數、或完全不同姓）一樣只算半個本人，處理同上。
                    # 首次分支（pin_hash 是 NULL）不看 name_hint。
                    elif not name.strip() and name_hint.strip() and name_matches(name_hint, stu["name"]) != "exact":
                        variant_only = True
                    if not (pin.isascii() and pin.isdigit() and 4 <= len(pin) <= 6):
                        if variant_only:
                            stu_pk = None
                        raise Reject(400, "PIN 要 4 到 6 位數字", "pin_format")
                    if not hmac.compare_digest(hash_pin(pin, stu["pin_salt"]), stu["pin_hash"]):
                        # 回應遺失、學生原樣重送同一個錯 PIN，不該被計兩次——60 秒內同一個
                        # （sid, student_pk, device_hash）送的還是同一個錯 PIN，就沿用上次算出的
                        # 次數，不再累加、不再多寫一筆 pin_fail；PIN 對了或換了一組不同的錯 PIN，
                        # 照現在規則正常累加。只記雜湊，不留明文 PIN 在行程記憶體裡。
                        dedup_key = (sid, stu["id"], dh)
                        pin_hash_for_dedup = hashlib.sha256(pin.encode()).hexdigest()
                        # 先 list() 取快照再走訪：寫入發生在 commit 之後、寫入鎖之外，別的執行緒可能正好在寫
                        # （字典邊走邊改會丟 RuntimeError → 500）
                        for k in [k for k, v in list(_pin_fail_recent.items()) if now() - v[1] >= PIN_FAIL_DEDUP_SECONDS]:
                            _pin_fail_recent.pop(k, None)
                        recent = _pin_fail_recent.get(dedup_key)
                        counted_dup = recent is not None and recent[0] == pin_hash_for_dedup and now() - recent[1] < PIN_FAIL_DEDUP_SECONDS
                        if recent is not None and counted_dup:
                            n = recent[2]
                        else:
                            n = (pf["n"] if pf else 0) + 1
                            for key in (dh, ""):  # 每裝置計一次，本場該生總計也計一次（換裝置識別重試也會累加）
                                con.execute("INSERT INTO pin_fail(session_id, student_pk, device_hash, n) VALUES(?,?,?,1) "
                                            "ON CONFLICT(session_id, student_pk, device_hash) DO UPDATE SET n=n+1", (sid, stu["id"], key))
                            con.commit()  # 例外會略過 context manager 的 commit，失敗次數要先落地；events 走獨立連線，不受這裡影響
                            _pin_fail_recent[dedup_key] = (pin_hash_for_dedup, now(), n)
                        left = st["pin_max_fail"] - n
                        if variant_only:
                            stu_pk = None  # pin_fail 上面已照寫，只是事件不歸到那位同學
                        if pin2:  # 前端以為是首次（帶了第二個 PIN 欄），但這學號其實已經設過 PIN
                            # 這一次已經算一次錯，訊息要說清楚還剩幾次（前端靠「已經設過 PIN」切只輸 PIN 模式）
                            exc = Reject(401, f"這個學號已經設過 PIN，你輸入的不是原本那組（剩 {left} 次）。先確認學號沒打錯，再輸入原本的 PIN；忘記了請找老師重設"
                                         if left > 0 else "這個學號已經設過 PIN，你輸入的不是原本那組，" + PIN_LOCKED_MSG, "pin_exists")
                        else:
                            exc = Reject(401, f"PIN 錯誤，剩 {left} 次。先確認學號沒打錯；忘記了請找老師重設" if left > 0 else PIN_LOCKED_MSG, "pin_wrong")
                        # 計次資訊掛在例外上，由 api_checkin 的 except 用獨立連線記 pin_count（這裡不能 log_event_on：例外會略過 commit）
                        exc.pin_count = (n, counted_dup, stu["id"], variant_only, st["pin_max_fail"])
                        raise exc
                    con.execute("DELETE FROM pin_fail WHERE session_id=? AND student_pk=? AND device_hash IN (?, '')", (sid, stu["id"], dh))
            # 已綁裝置送來的是首綁表單（帶了 pin2）——不驗 PIN、不改 PIN（上面 same_device_as_bound
            # 分支本來就沒動 pin_hash），只是多回一個旗標讓前端告訴學生「PIN 沒有變更，仍是原本那組」，
            # 免得學生誤以為自己剛剛設的新 PIN 生效了。
            pin_unchanged = (not first_time) and same_device_as_bound and bool(pin2)
            # 裝置綁定放在回收據之前：老師重設後學生重掃，PIN 與手機要一起綁好
            # 這支手機已經是別人的就不綁（否則代簽者的手機會認成被代簽者，還能看他的自查）
            rebound = False
            if stu["device_hash"] is None and not other and not same:
                con.execute("UPDATE students SET device_hash=?, bound_at=? WHERE id=?", (dh, now(), stu["id"]))
                if not first_time:
                    # 學號原本就設過 PIN、只是沒綁裝置（老師只按「解除綁定」）——PIN 對了綁上這支，
                    # 跟改綁一樣要留旗標與事件，老師才看得到「這支是靠 PIN 綁上的」
                    flags.append("rebind")
                    rebound = True
                    log_event_on(con, sid, "rebind", reason="", device8=device8, ua_class=ua_class, student_pk=stu["id"])
            # PIN 對了、學號卻綁在另一個瀏覽器（iPhone 控制中心掃碼器、Safari
            # 私密分頁、從 LINE 裡開，每次都是新的 cookie 罐）——到這裡 PIN 已經驗過，分三種情況：
            #   1. 本場已經有這個學號的非 auto 簽到列：不改綁，照舊 409（別支瀏覽器簽的或老師記的，不用再簽）
            #   2. 這支裝置是別人的（other）或本場已有別的學號用它簽過（same）：不改綁、不擋，往下走
            #      既有流程，由 device_bound_other／same_device 旗標轉成待確認
            #   3. 其餘：改綁並收下（旗標 rebind，allow_bind 不管改綁）
            if stu["device_hash"] is not None and stu["device_hash"] != dh:
                prior = con.execute("SELECT id, status, source, flags FROM checkins WHERE session_id=? AND student_pk=? AND source!='auto'",
                                    (sid, stu["id"])).fetchone()
                if prior:
                    if variant_only:
                        stu_pk = None  # 姓名只是同姓同字數，這個 409 不歸到那位同學
                    if _marked_off(prior):
                        # 老師先記了缺席／請假（或抽點判不在），學生才用新環境掃到——標旗讓老師頁看得到；
                        # 事件由下面的 Reject 例外統一記一筆 checkin_reject，這裡不另記
                        pf_flags = json.loads(prior["flags"] or "[]")
                        if "qr_after_manual" not in pf_flags:
                            con.execute("UPDATE checkins SET flags=? WHERE id=?", (json.dumps(pf_flags + ["qr_after_manual"]), prior["id"]))
                        con.commit()
                        raise Reject(409, f"這個學號這一場已被老師記為「{STATUS_LABEL[prior['status']]}」。你人在教室的話，現在舉手讓老師改；下次請用同一個 App 或瀏覽器掃 QR。",
                                     "already_recorded")
                    con.commit()  # PIN 打對時清除失敗次數的 DELETE 要落地，不能跟這個例外一起回滾
                    if s["kind"] == "bind":  # 綁定場次不算出缺勤，別說「出席」讓人以為今天已記出席
                        raise Reject(409, "這個學號這一場已經綁定過了（另一個瀏覽器或老師記的）。這一場只做綁定、不算出缺勤；今天還沒簽到成功的，請找老師補登。"
                                     "不是你本人、或紀錄不對，現在就舉手找老師。", "already_recorded")
                    raise Reject(409, f"這個學號這一場已經有紀錄（{STATUS_LABEL[prior['status']]}），是用另一個瀏覽器簽的或老師記的，不用再簽。"
                                 "不是你本人簽的、或紀錄不對，現在就舉手找老師。下次請用同一個 App 或瀏覽器掃 QR。",
                                 "already_recorded")
                if not (other or same):
                    con.execute("UPDATE students SET device_hash=?, bound_at=? WHERE id=?", (dh, now(), stu["id"]))
                    flags.append("rebind")
                    rebound = True
                    log_event_on(con, sid, "rebind", reason=stu["device_hash"][:8], device8=device8, ua_class=ua_class,
                                 student_pk=stu["id"])
            # PIN 與裝置都驗過了，才回「已簽過」的收據
            already = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?", (sid, stu["id"])).fetchone()
            if already and already["source"] != "auto":
                af = json.loads(already["flags"])
                add = []
                # 老師先把他記成請假／缺席，學生之後才掃到：讓老師看得到（記成出席的不必標，首頁說明寫的就是請假或缺席）
                if _marked_off(already) and "qr_after_manual" not in af:
                    add.append("qr_after_manual")
                # 這一列不是這次寫的（已存在），但這次的裝置本身仍可能撞衝突——照樣標旗給老師看，
                # 但「狀態不改」：老師手動設的狀態是既定不變式，不能因為別人拿別的手機來簽而被動掉
                if other and "device_bound_other" not in af:
                    add.append("device_bound_other")
                if same and "same_device" not in af:
                    add.append("same_device")
                af_final = af + add
                if add:
                    con.execute("UPDATE checkins SET flags=? WHERE id=?", (json.dumps(af_final), already["id"]))
                _ok(already["status"], con)
                again_payload = {"ok": True, "again": True, "name": stu["name"], "student_id": stu["student_id"],
                                 "ts": fmt(already["ts"]), "seat": already["seat"], "status": STATUS_LABEL[already["status"]],
                                 "kind": s["kind"]}
                if already["status"] == "pending":  # 待確認的收據也要說對原因；status 只會跟三個旗標之一同時成立
                    again_payload["pending_reason"] = pending_reason_from_flags(af_final)
                return with_device_cookie(req, again_payload, device_key)
            if other:
                flags.append("device_bound_other")
            if same:
                flags.append("same_device")
                sf = json.loads(same["flags"])  # 先簽的那位也標、也轉待確認：兩個人都要老師當場看
                if "same_device" not in sf:
                    # 只有學生自己簽、也沒被老師當場確認在場的列才轉待確認；老師手動定案的狀態
                    # 與抽點按「在」的人只加旗標，不因為別人拿這支手機來簽而改
                    if same["source"] == "qr" and "spot_ok" not in sf:
                        con.execute("UPDATE checkins SET flags=?, status=CASE WHEN status IN ('present','late') THEN 'pending' ELSE status END WHERE id=?",
                                    (json.dumps(sf + ["same_device"]), same["id"]))
                    else:
                        con.execute("UPDATE checkins SET flags=? WHERE id=?", (json.dumps(sf + ["same_device"]), same["id"]))
            mass_bind = False
            if first_time:
                # 首綁潮期間 same_fp／bind_burst 幾乎人人亮紅，老師會學會忽略旗標。
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
            # 私密分頁替人首綁或改綁——本場另一個學號已成功（出席／遲到／待確認）的簽到列
            # 若指紋（IP＋UA＋語言）完全相同，這筆加 same_fp 並在一般點名轉待確認；綁定場次只加旗標。
            # 首綁維持「本場首綁滿 5 人就不比」，改綁一律比。
            same_fp_hit = False
            if rebound or (first_time and not mass_bind):
                same_fp_hit = con.execute(
                    "SELECT 1 FROM checkins WHERE session_id=? AND fp=? AND student_pk!=? AND status IN ('present','late','pending') LIMIT 1",
                    (sid, fp, stu["id"])).fetchone() is not None
                if same_fp_hit and "same_fp" not in flags:
                    flags.append("same_fp")
            if st["ip_check"] and s["teacher_ip"] and ip and ip != s["teacher_ip"]:
                flags.append("ip_differs")
            if variant_only:
                flags.append("name_variant")  # 同姓同字數的姓名只算半個本人，轉待確認（下面 name_variant → pending）
            # 出席／遲到看「掃到」（grant 發證時間）是否落在開放窗內，不是送出表單的時間；
            # 用送出當下讀到的 open_until（延長過就是延長後的值）。
            # issued 是這次落地請求「到達
            # 伺服器」的時刻，網路慢或重掃時會比實際掃到晚；所以改用「同一支手機這一場最早的一次
            # land_ok」跟 issued 取較早者——落地時間只記在 events，這裡才要查表。經 /c 六位數
            # 進來的憑證沒有 land_ok 事件（land_expired/land_ok 只在 /s/... 那條路徑記），查不到
            # 就照舊用 issued，行為不變。只影響「出席還是遲到」與 was_late 這一個判定，其他檢查
            # （憑證時效、裝置綁定、場次是否結束）都照舊用 issued／原本的值。
            # 借同學手機簽到的人（這支裝置是別人的、或本場別人用它簽過）不繼承出借者的最早落地時間，
            # 9/26 裁定說的「同一支手機」是本人的手機
            earliest_land = None if (other or same) else con.execute(
                "SELECT MIN(ts) FROM events WHERE session_id=? AND device8=? AND kind='land_ok' AND ts>=?",
                (sid, device8, s["opened_at"])).fetchone()[0]
            scan_ts = min(issued, earliest_land) if earliest_land is not None else issued
            status = "present" if scan_ts <= s["open_until"] else "late"
            if status == "late":
                flags.append("was_late")  # 待確認被判在場時，照簽到當下的窗口還原，不受之後延長影響
            if "same_device" in flags or "device_bound_other" in flags or "name_variant" in flags \
                    or (same_fp_hit and s["kind"] == "normal"):
                status = "pending"  # 姓名寫法不同的首綁跟 same_device 同一套待確認機制
            t = now()
            if already:  # 防禦：若有 auto 缺席列殘留（正常流程結案後就 410，走不到這裡）
                con.execute("UPDATE checkins SET ts=?, seat=?, status=?, flags=?, ip=?, device_hash=?, fp=?, source='qr' WHERE id=?",
                            (t, seat, status, json.dumps(flags), ip, dh, fp, already["id"]))
            else:
                try:
                    con.execute("INSERT INTO checkins(session_id, student_pk, ts, seat, status, flags, ip, device_hash, fp, source) "
                                "VALUES(?,?,?,?,?,?,?,?,?,'qr')", (sid, stu["id"], t, seat, status, json.dumps(flags), ip, dh, fp))
                # 手機重複送出、網路重試：同一筆已經在了。這條分支要求兩個獨立請求都通過上面
                # 「already」查無此列的判斷、又都活到 INSERT——但本函式從 BEGIN IMMEDIATE 起
                # 全程持有寫入鎖到 commit，第二個請求的 BEGIN IMMEDIATE 會先卡住，等第一個
                # commit 之後才會往下讀，屆時「already」查詢一定看得到第一個剛寫入的列，走不到
                # 這個 except；保留只是防禦未來連線／鎖定模型改變。
                except sqlite3.IntegrityError:
                    dup = con.execute("SELECT * FROM checkins WHERE session_id=? AND student_pk=?", (sid, stu["id"])).fetchone()
                    _ok(dup["status"], con)
                    dup_payload = {"ok": True, "again": True, "name": stu["name"], "student_id": stu["student_id"],
                                  "ts": fmt(dup["ts"]), "seat": dup["seat"], "status": STATUS_LABEL[dup["status"]],
                                  "kind": s["kind"]}
                    if dup["status"] == "pending":  # 待確認的收據也要說對原因
                        dup_flags = json.loads(dup["flags"]) if dup["flags"] else []
                        dup_payload["pending_reason"] = pending_reason_from_flags(dup_flags)
                    return with_device_cookie(req, dup_payload, device_key)
        _ok(status)
        payload = {"ok": True, "again": False, "name": stu["name"], "student_id": stu["student_id"], "ts": fmt(t),
                  "seat": seat, "status": STATUS_LABEL[status], "first_time": first_time, "kind": s["kind"]}
        if pin_unchanged:
            payload["pin_unchanged"] = True
        if rebound:
            payload["rebound"] = True
        if status == "pending":
            # 待確認要說對原因，姓名寫法不同跟「這支手機簽過別的學號」是完全不同的情況，
            # 多個旗標同時成立時依這個順序取第一個；status 只會在其中一個旗標成立時才被設成 pending。
            payload["pending_reason"] = pending_reason_from_flags(flags)
        return with_device_cookie(req, payload, device_key)
    except Reject as exc:
        if stu_pk is None and sid_verified and sid is not None:
            stu_pk = _claimed_pk(sid, student_id, name if name.strip() else name_hint)
        log_event(sid, "checkin_reject", reason=exc.slug, device8=device8, ua_class=ua_class, student_pk=stu_pk)
        if exc.pin_count is not None:  # 只進事件表給事後診斷，老師看得到的地方都不讀 pin_count
            n, dup, pc_pk, variant, pin_max = exc.pin_count
            log_event(sid, "pin_count", reason=f"{n}/{pin_max}" + (" dup" if dup else "") + (" variant" if variant else ""),
                      device8=device8, ua_class=ua_class, student_pk=pc_pk)
        raise
    except Exception as exc:
        # 非預期例外（例如 database is locked）原本直接 500、不記事件，漏斗把這些學生
        # 誤算成「落地未送出」。這裡盡力記一筆 checkin_error 事件再原樣往外拋，不吞掉例外、
        # 不改回應內容或狀態碼；log_event 本身逾時很短（見它的文件字串），不會讓這段本身卡住。
        if stu_pk is None and sid_verified and sid is not None:
            stu_pk = _claimed_pk(sid, student_id, name if name.strip() else name_hint)
        log_event(sid, "checkin_error", reason=type(exc).__name__, device8=device8, ua_class=ua_class, student_pk=stu_pk)
        raise


@app.middleware("http")
async def no_store(req: Request, call_next):
    """所有頁面與 API 都是個人化內容，禁止 Cloudflare 與瀏覽器快取。"""
    resp = await call_next(req)
    if req.url.path.startswith("/static/"):
        if req.query_params.get("v") == FONT_V:  # 帶目前內容雜湊的網址內容不會變，長期快取（沒帶或不符維持七天）
            resp.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        else:
            resp.headers.setdefault("Cache-Control", "public, max-age=604800")
    else:
        resp.headers.setdefault("Cache-Control", "private, no-store")
    return resp


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    """除了 SELECT 1 能不能通，另外檢查資料庫檔與所在目錄是不是容器帳號可寫——部署時
    chown 沒加 -R 就會出現「檔案是舊擁有者、目錄是新的」或反過來的情況，SELECT 1 照樣過，
    但一按「開始點名」就 500（readonly database）。WAL 模式寫入還要能在目錄裡建
    -wal／-shm，所以目錄也要可寫，不只檔案。這裡只檢查權限，不在健康檢查裡真的寫入
    （上課中每次健康檢查都搶寫入鎖不划算）。"""
    db_file = Path(DB_PATH)
    db_dir = db_file.parent
    if db_file.exists() and not os.access(db_file, os.W_OK):
        return PlainTextResponse("database file not writable", status_code=503)
    if not os.access(db_dir, os.W_OK):
        return PlainTextResponse("database directory not writable (WAL needs -wal/-shm files there)", status_code=503)
    with db() as con:
        con.execute("SELECT 1")
    return "ok"


def _err_page(req: Request, status: int, detail: str):
    if req.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": detail}, status_code=status)
    if status == 401 and req.url.path.startswith("/t"):
        return RedirectResponse(_login_redirect_url(req), 303)
    if status == 404:
        detail = "沒有這個頁面"
    p = req.url.path
    home_href = "/t" if p == "/t" or p.startswith("/t/") else "/help"  # 學生端錯誤頁的「主頁」不連老師登入
    return templates.TemplateResponse(req, "error.html", {"status": status, "detail": detail, "home_href": home_href}, status_code=status)


STUDENT_ROUTE_PATHS = ("/c", "/api/whoami", "/api/me", "/api/checkin")  # L5：學生端路由（再加 /s/ 開頭的）


def _is_student_route(path: str) -> bool:
    return path in STUDENT_ROUTE_PATHS or path.startswith("/s/")


def _log_student_error(req: Request, kind: str, reason: str, with_sid: bool = False) -> None:
    """L5／L6：學生看得到的失敗都留一筆事件。只記原因碼、裝置雜湊前 8 碼、UA 類別，不記學號姓名與 PIN；
    with_sid（L6）時場次號從網址的 {sid} 取（取不到就 None）。同步函式，由 handler 丟進執行緒池跑（log_event 是同步 I/O）。"""
    key = effective_device_key(req)
    sid = str(req.path_params.get("sid", "")) if with_sid else ""
    log_event(int(sid) if sid.isascii() and sid.isdigit() else None, kind, reason=reason,
              device8=hash_device(key)[:8] if key else "", ua_class=classify_ua(req.headers.get("user-agent", "")))


@app.exception_handler(StarletteHTTPException)
async def http_exc(req: Request, exc: StarletteHTTPException):
    path, route = req.url.path, req.scope.get("route")
    # L6：路由有對到（沒對到的 404 是掃站機器人）、不是老師端、也不是 /api/checkin（do_checkin 已經自己記了）
    if route is not None and not (path == "/t" or path.startswith(("/t/", "/api/t/"))) and path != "/api/checkin":
        await run_in_threadpool(_log_student_error, req, "http_err", f"{exc.status_code} {route.path}", True)
    return _err_page(req, exc.status_code, str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_exc(req: Request, exc: Exception):
    """非預期例外一律回中文頁（學生端）或中文 JSON（/api/），不讓學生看到英文的 Internal Server Error。
    事件只記路由樣式（不含簽章、學號等個資）；老師端與 /api/checkin（do_checkin 自己記了）不記學生端事件。
    例外本身仍由 Starlette 往外拋，伺服器日誌照樣有 traceback。"""
    path, route = req.url.path, req.scope.get("route")
    teacher = path == "/t" or path.startswith(("/t/", "/api/t/"))
    if route is not None and not teacher and path != "/api/checkin":
        await run_in_threadpool(_log_student_error, req, "http_err", f"500 {route.path}", True)
    if teacher or path.startswith("/api/"):
        return _err_page(req, 500, "系統出了點問題，請重新整理頁面再試一次")
    return _err_page(req, 500, "系統出了點問題，請重新掃 QR；還是不行請找老師")


@app.exception_handler(RequestValidationError)
async def validation_exc(req: Request, exc: RequestValidationError):
    path = req.url.path
    if _is_student_route(path):  # L5
        route = req.scope.get("route")
        await run_in_threadpool(_log_student_error, req, "invalid", route.path if route is not None else "/" + path.split("/")[1])
    return _err_page(req, 400, "網址或表單內容不對")


@app.get("/api/ip", response_class=PlainTextResponse)
def api_ip(req: Request):
    """診斷用：系統看到的你的 IP（IP 比對標記就是拿這個比）。"""
    return client_ip(req)

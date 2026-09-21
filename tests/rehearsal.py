"""教室彩排模擬器：真瀏覽器（Chromium＋WebKit）、真實時鐘，模擬一整班手機在真實教室裡點名，
產出漏斗報告。背景見 tests/README.md——2026-09-10 清大 OOP
第一堂進教室試用（人還沒到齊，請先到的人測），連上的 9 支手機只成功 6 支，而當時測試鏈（pytest 分支覆蓋 97%、情境 50 項、Schemathesis、
單機端對端）全綠。那份文件證明「單機、無延遲、假設一次就填對表單」的端對端測不出真實教室的摩擦。
這支腳本要在本機重現那種場面：QR 每 10 秒換一次＋5 秒容許（app.py DEFAULT_SETTINGS），學生從
「掃到」到「瀏覽器真的送出請求」有實測的對數常態延遲，首次綁定表單會打錯字、PIN 位數、兩次 PIN 不同。

獨立可執行：
  .venv/bin/python tests/rehearsal.py [--students 30] [--profile field|fast] [--weeks 2] [--seed 1]
      [--projector-seconds N] [--app-dir PATH] [--json out.json] [--headed]

只讀本專案原始碼決定流程與選擇器（模板與 app.py），不猜測。全程使用暫存資料庫與子行程 uvicorn，
結束時（含中途出錯）一定收掉子行程與暫存檔。

--app-dir：uvicorn／manage.py／templates 從這個目錄起算（預設＝這支腳本所在的 services/attend）。
直譯器（.venv/bin/python、.venv/bin/uvicorn）永遠用這支腳本自己的 venv，不受 --app-dir 影響——
量基線常見用法是拿一個沒有 .venv 的唯讀 git worktree當 --app-dir，借這支腳本自己的環境跑它。

鐵律：不可為了讓數字好看而改延遲模型或閘門。profile=field 第 1 週預期過不了閘門，這是對的。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import hmac
import json
import math
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent  # services/attend
PY = str(BASE_DIR / ".venv" / "bin" / "python")
UVICORN = str(BASE_DIR / ".venv" / "bin" / "uvicorn")
TEACHER_PW = "rehearsal-pw"
COURSE_CODE = "REHEARSAL"
COURSE_NAME = "教室彩排模擬"

# app.py DEFAULT_SETTINGS 的預設值（我們不呼叫 /t/course/{cid}/settings，所以場次一律用這組）。
DEFAULT_QR_INTERVAL = 10
DEFAULT_OPEN_MINUTES = 3
DEFAULT_LATE_MINUTES = 15

# 點名頁 #err 的鍵盤遮擋判定：iPhone 13 裝置描述（Playwright p.devices）視窗高度正是 664，
# 這支腳本主要就是用這個描述模擬 iPhone；假設鍵盤彈出後吃掉約 300px（iOS/Android 常見數字級距），
# 可視高度只剩 364px，#err 的 bounding box 若落在這條線以下就記一次「被鍵盤蓋住」。
KB_VIEWPORT_H = 664
KB_HEIGHT = 300
KB_VISIBLE_H = KB_VIEWPORT_H - KB_HEIGHT

ANDROID_DEVICES = ["Pixel 7", "Galaxy S9+"]


# ───────────────────────── 環境：找埠、暫存庫、子行程 ─────────────────────────

def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_healthz(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as e:  # noqa: PERF203
            last_err = e
        time.sleep(0.3)
    raise RuntimeError(f"伺服器 {base_url} 一直沒通過 /healthz：{last_err}")


def ensure_shared_roster(app_dir: Path) -> None:
    """跟 tests/run_all.sh 生成同一份 <app_dir>/tests/fixtures/roster.csv（只在不存在時建立，不覆寫）。
    這份彩排腳本自己另外匯入獨立課程（REHEARSAL），不依賴這份檔案的列數，
    建立它只是為了跟其他測試共用同一個 fixture 慣例，避免各自產生內容不一致的版本。
    走 app_dir 而不是這支腳本自己的 BASE_DIR：--app-dir 可能指到另一份 checkout
    （例如量基線用的唯讀 git worktree），那份不一定已經有這個檔案。"""
    path = app_dir / "tests" / "fixtures" / "roster.csv"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    names = ["陳大文", "吳家豪", "林小華", "張志明"] + [f"測試{n}" for n in range(5, 49)]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "name", "class"])
        for n in range(1, 49):
            if n == 18:
                continue
            w.writerow([f"9994A{n:03d}", names[n - 1], "測試班"])


def write_rehearsal_roster(csv_path: Path, n: int) -> None:
    """為這次彩排單獨產生 N 人名冊（獨立課程代碼，不動共用的 fixtures/roster.csv）。"""
    base_names = ["陳大文", "吳家豪", "林小華", "張志明"]
    names = base_names + [f"測試{i}" for i in range(len(base_names) + 1, n + 1)]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["student_id", "name", "class"])
        for i in range(1, n + 1):
            w.writerow([f"9994B{i:03d}", names[i - 1], "彩排班"])


@dataclass
class Server:
    proc: subprocess.Popen
    port: int
    base_url: str
    db_path: Path
    tmp_dir: Path
    log_path: Path
    secret: str


def start_server(students: int, seed: int, app_dir: Path) -> Server:
    tmp_dir = Path(tempfile.mkdtemp(prefix="attend-rehearsal-"))
    db_path = tmp_dir / "attend.db"
    log_path = tmp_dir / "uvicorn.log"
    roster_path = tmp_dir / "roster.csv"
    write_rehearsal_roster(roster_path, students)
    ensure_shared_roster(app_dir)

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    secret = hashlib.sha256(f"attend-rehearsal-seed-{seed}".encode()).hexdigest()
    env = os.environ.copy()
    env.update({
        "ATTEND_SECRET": secret, "ATTEND_TEACHER_PASSWORD": TEACHER_PW,
        "ATTEND_DB": str(db_path), "ATTEND_BASE_URL": base_url,
    })

    imp = subprocess.run(
        [PY, "manage.py", "import-roster", "--code", COURSE_CODE, "--name", COURSE_NAME, "--csv", str(roster_path)],
        cwd=str(app_dir), env=env, capture_output=True, text=True, check=False)
    if imp.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"manage.py import-roster 失敗：{imp.stdout}\n{imp.stderr}")

    log_f = log_path.open("w")
    proc = subprocess.Popen([UVICORN, "app:app", "--port", str(port)], cwd=str(app_dir), env=env,
                            stdout=log_f, stderr=subprocess.STDOUT)
    try:
        wait_healthz(base_url)
    except Exception:
        stop_server_proc(proc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return Server(proc=proc, port=port, base_url=base_url, db_path=db_path, tmp_dir=tmp_dir, log_path=log_path, secret=secret)


def stop_server_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def teardown_server(srv: Server) -> None:
    stop_server_proc(srv.proc)
    shutil.rmtree(srv.tmp_dir, ignore_errors=True)


# ───────────────────────── 輪換碼：與 app.py slot_sig/code6 位元對位重算 ─────────────────────────
# app.py 的 /api/t/session/{sid}/qr 只回 svg（QR 圖的路徑資料）與 code（六位數），不回 url 純文字，
# 也沒有其他 API 能拿到 QR 實際編碼的網址（本檔開頭已讀過 app.py 確認過，不是漏讀）。
# 要模擬「手機相機讀到投影幕上的 QR」，這裡直接重算跟 app.py 完全相同的 HMAC 算式：
# K_SLOT = HMAC(ATTEND_SECRET, "attend:slot")、slot_sig = HMAC(K_SLOT, f"{sid}:{session.secret}:{slot}")[:10]。
# ATTEND_SECRET 是我們自己起服務時設的環境變數（已知），session.secret 用唯讀連線直接讀 sqlite
# （我們自己建的暫存庫，跟伺服器並行讀沒有互斥問題）。算出來的 url／code 跟真正投影幕上會顯示的
# 位元相同，比另外裝 QR 解碼器對著 svg 去讀畫面要輕量、也更貼近「這一刻該顯示什麼」。

def k_slot(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"attend:slot", hashlib.sha256).digest()


def slot_sig(ks: bytes, sid: int, session_secret: str, slot: int) -> str:
    return hmac.new(ks, f"{sid}:{session_secret}:{slot}".encode(), hashlib.sha256).hexdigest()[:10]


def code6(sig: str) -> str:
    return f"{int(sig, 16) % 1_000_000:06d}"


@dataclass
class SessionCrypto:
    sid: int
    base_url: str
    ks: bytes
    session_secret: str
    iv: int

    def at(self, t: float) -> tuple[str, str]:
        slot = int(t // self.iv)
        sig = slot_sig(self.ks, self.sid, self.session_secret, slot)
        code = code6(sig)
        return f"{self.base_url}/s/{self.sid}/{slot}/{sig}", code


def read_session_crypto(db_path: Path, secret: str, base_url: str, sid: int, retries: int = 30) -> SessionCrypto:
    ks = k_slot(secret)
    for _ in range(retries):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT s.secret AS secret, c.settings AS settings FROM sessions s "
                "JOIN courses c ON c.id=s.course_id WHERE s.id=?", (sid,)).fetchone()
            con.close()
            if row:
                iv = DEFAULT_QR_INTERVAL
                try:
                    cfg = json.loads(row["settings"] or "{}")
                    iv = max(5, int(cfg.get("qr_interval", DEFAULT_QR_INTERVAL)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                return SessionCrypto(sid=sid, base_url=base_url, ks=ks, session_secret=row["secret"], iv=iv)
        except sqlite3.OperationalError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"讀不到 session {sid} 的密鑰（暫存庫 {db_path}）")


# ───────────────────────── 延遲模型（掃到→瀏覽器真的送出請求） ─────────────────────────

def sample_scan_delay(profile: str, rng: random.Random) -> float:
    if profile == "field":
        median, sigma, lo, hi = 7.0, 0.6, 2.0, 40.0  # 實地日誌中位數 7 秒（見 FIELD_2026-09-10.md）
    else:
        median, sigma, lo, hi = 1.5, 0.6, 0.3, 10.0  # fast＝對照組，等同現有 e2e 幾乎即時送出的假設
    mu = math.log(median)
    return min(hi, max(lo, rng.lognormvariate(mu, sigma)))


def sample_fill_delay(mode: str, rng: random.Random) -> float:
    """落地→第一次送出：思考「想 PIN、找學號」的時間加上打字時間，合計一個對數常態抽樣值。
    只在 profile=field 套用（fast 當對照組維持原本近乎即時送出的假設，見呼叫端）。
    首次綁定（mode="first"）：實地「落地→第一次送出」五筆＝10、37、39、37、172 秒
    （背景與逐支摩擦紀錄見 FIELD_2026-09-10.md，例如 3fd1 落地後 10 秒送出、c6fb 37 秒送出成功；
    完整五筆數列於 2026-09-21 這次校準任務提供）。中位數 37 秒、sigma 0.5，夾在 8–180 秒。
    回簽（mode="return"，只輸 PIN）：FIELD_2026-09-10.md 沒有這類實地資料——9/10 那堂課全班都是
    第一次，走的都是首次綁定。暫用中位數 8 秒、sigma 0.4，待第一次真實回簽後校正；
    2–60 秒的夾限只是模擬安全網，不是實地值。"""
    if mode == "first":
        median, sigma, lo, hi = 37.0, 0.5, 8.0, 180.0
    else:
        median, sigma, lo, hi = 8.0, 0.4, 2.0, 60.0
    mu = math.log(median)
    return min(hi, max(lo, rng.lognormvariate(mu, sigma)))


def alt_pin(p1: str) -> str:
    n = int(p1)
    return f"{(n + 1) % 10000:04d}"


def seeded_rng(*parts) -> random.Random:
    """random.Random() 只吃 None/int/float/str/bytes；用字串串接組出可重現的子種子。"""
    return random.Random("|".join(str(p) for p in parts))


# ───────────────────────── 老師端（真瀏覽器） ─────────────────────────

async def teacher_login(page, base_url: str) -> None:
    await page.goto(f"{base_url}/t/login")
    await page.fill("input[name=password]", TEACHER_PW)
    async with page.expect_navigation():
        await page.click("button:has-text('登入')")


async def open_week_session(teacher_page, base_url: str, kind: str = "normal") -> int:
    """kind="bind"：B030 綁定場次（第 1 週用），對應 home.html 表單裡 name=kind value=bind 的那顆次要按鈕。"""
    await teacher_page.goto(f"{base_url}/t")
    async with teacher_page.expect_navigation(url=re.compile(r"/t/session/\d+")):
        await teacher_page.locator(f"form[action$='/open'] button[value={kind}]").click()
    return int(teacher_page.url.rsplit("/", 1)[1])


async def open_projector(teacher_ctx, base_url: str, sid: int):
    proj = await teacher_ctx.new_page()
    await proj.goto(f"{base_url}/t/projector/{sid}")
    await proj.wait_for_function("document.querySelector('#qr svg')!==null", timeout=8000)
    return proj


async def _accept_dialog(dialog) -> None:
    await dialog.accept()


async def close_week_session(teacher_page) -> None:
    teacher_page.once("dialog", _accept_dialog)
    await teacher_page.click("#closeBtn")
    await teacher_page.wait_for_function("document.getElementById('phase').innerText==='已結束'", timeout=15000)


async def teacher_state(teacher_ctx, base_url: str, sid: int) -> dict:
    r = await teacher_ctx.request.get(f"{base_url}/api/t/session/{sid}/state")
    return await r.json()


async def teacher_qr_code(teacher_ctx, base_url: str, sid: int) -> str:
    """六位數改輸流程用：實際呼叫老師端 API 拿當下 code（跟投影頁同一個端點）。"""
    r = await teacher_ctx.request.get(f"{base_url}/api/t/session/{sid}/qr")
    data = await r.json()
    return data["code"]


# ───────────────────────── 學生端 ─────────────────────────

@dataclass
class Attempt:
    """一個學生這一週的嘗試預算：120 秒與累計 4 次失敗共用同一個門檻。
    「4 次失敗」讀作累計失敗（QR 落地過期、改輸六位數失敗、表單送出被拒都算一次），
    不只是表單送出被拒——這樣讀最貼近規格原文「同一學生累計 4 次失敗」的字面，
    也才會讓落地過期的重試真的吃到放棄預算，不是無限重試到成功為止。"""
    deadline: float
    fails: int = 0

    def time_up(self) -> bool:
        return time.time() > self.deadline

    def fail(self) -> bool:
        """記一次失敗，回傳是否已達 4 次門檻（達到即應放棄）。"""
        self.fails += 1
        return self.fails >= 4


def classify_error(text: str) -> str:
    mapping = [
        ("PIN 要 4 到 6 位數字", "PIN格式錯"),
        ("兩次 PIN 不一樣", "兩次PIN不同"),
        ("名冊不符", "學號姓名不符"),
        ("剩 ", "PIN錯誤(還有機會)"),
        ("PIN 錯太多次", "PIN鎖定"),
        ("另一支手機", "裝置已綁他人"),
        ("填表時間已到", "表單逾時"),
        ("不開放首次綁定", "首次綁定未開放"),
    ]
    for key, label in mapping:
        if key in text:
            return label
    return "其他：" + text[:24]


async def err_covered(page) -> bool:
    """#err 的 bounding box 是否落在「假設鍵盤彈出後的可視高度」以下。"""
    try:
        box = await page.locator("#err").bounding_box()
    except Exception:  # noqa: BLE001 -- 頁面可能剛好在導頁，量不到就當作沒蓋住
        return False
    if not box:
        return False
    return (box["y"] + box["height"]) > KB_VISIBLE_H


async def blind_retries_then_notice(page, rng: random.Random, attempt: Attempt, errors: list[str], log
                                     ) -> tuple[str, dict | None, int]:
    """錯誤被鍵盤蓋住時的共用處理：模擬的學生讀不到 DOM 裡的 #err 文字，以為按鈕沒反應，
    1 秒內再按 1–2 次、值原樣不變地重送（FIELD_2026-09-10.md：3fd1 一秒內連按三次 400）。
    回傳 (status, success_outcome, covered_delta)：
      status="ok"      → success_outcome 帶完整送出結果
      status="fails4"／"timeout_120s" → 已達放棄門檻，呼叫端直接收工
      status="continue" → 這幾次盲按都還是失敗但預算沒用完，呼叫端接著等 20–45 秒才發現並改正
    （FIELD_2026-09-10.md：3fd1 從第一次 400 到成功 46 秒、0c5d 77 秒）。"""
    covered_delta = 0
    for _ in range(rng.randint(1, 2)):
        if attempt.time_up():
            return "timeout_120s", None, covered_delta
        await asyncio.sleep(rng.uniform(0.2, 0.5))
        await page.click("#go")
        outcome = await wait_submit_result(page)
        if outcome["ok"]:
            log("submit_ok", outcome["status"])
            return "ok", outcome, covered_delta
        errors.append(outcome["error"])
        if await err_covered(page):
            covered_delta += 1
        log("submit_err", outcome["error"])
        if attempt.fail():
            return "fails4", None, covered_delta
    return "continue", None, covered_delta


async def ensure_field(page, selector: str, value: str, rng: random.Random) -> None:
    """只在值不同時才清空重打（模擬學生只改錯的那個欄位），每字 80–200ms。"""
    loc = page.locator(selector)
    cur = await loc.input_value()
    if cur == value:
        return
    await loc.click()
    await loc.fill("")
    for ch in value:
        await loc.press_sequentially(ch)
        await asyncio.sleep(rng.uniform(0.08, 0.2))


async def wait_submit_result(page, timeout_ms: float = 12000) -> dict:
    await page.wait_for_function(
        "document.getElementById('receipt').style.display!=='none'||document.getElementById('err').innerText!==''",
        timeout=timeout_ms)
    if await page.locator("#receipt").is_visible():
        status = await page.locator("#rstatus").inner_text()
        again = "已經簽過" in (await page.locator("#rnote").inner_text() or "")
        return {"ok": True, "status": status, "again": again}
    err = await page.locator("#err").inner_text()
    return {"ok": False, "error": err}


async def fill_and_submit_loop(page, student: dict, rng: random.Random, attempt: Attempt, log, profile: str) -> dict:
    """回傳 dict：ok, status(若成功), mode, errors(list[str]), covered(int), reason(若失敗)。
    失敗與逾時共用 attempt（跟落地階段同一個 4 次失敗預算與 120 秒門檻）。
    profile=field 時先套 sample_fill_delay（落地→第一次送出的思考＋打字時間）；
    fast 當對照組不套，維持原本近乎即時送出的假設。"""
    await page.wait_for_function(
        "document.getElementById('modeHint')!==null && "
        "(document.getElementById('returnFields').style.display!=='none' || document.getElementById('modeHint').innerText.length>0)",
        timeout=8000)
    is_return = await page.locator("#returnFields").is_visible()
    mode = "return" if is_return else "first"
    errors: list[str] = []
    covered = 0

    if profile == "field":
        fill_delay = min(sample_fill_delay(mode, rng), max(0.0, attempt.deadline - time.time()))
        await asyncio.sleep(fill_delay)
        log("fill_delay", f"{fill_delay:.1f}s")

    if mode == "first":
        pin = student["pin"]
        defects = {
            "pin_len": rng.random() < 0.15,      # 15% 第一次輸 3 位數 PIN
            "name_typo": rng.random() < 0.10,     # 10% 姓名多打空白或少一個字
            "pin_mismatch": rng.random() < 0.05,  # 5% 兩次 PIN 不同
        }
        name_missing_char = rng.random() < 0.5  # 10% 裡再對半分「多空白／少一字」
        log("defects", json.dumps({**defects, "name_missing_char": name_missing_char if defects["name_typo"] else None}, ensure_ascii=False))
        while True:
            if attempt.time_up():
                return {"ok": False, "reason": "timeout_120s", "mode": mode, "errors": errors, "covered": covered}
            cur_name = student["name"]
            if defects["name_typo"]:
                cur_name = student["name"][:-1] if name_missing_char else (student["name"] + " ")
            p1 = "123" if defects["pin_len"] else pin
            p2 = alt_pin(p1) if (defects["pin_mismatch"] and not defects["pin_len"]) else p1
            await ensure_field(page, "#student_id", student["student_id"], rng)
            await ensure_field(page, "#name", cur_name, rng)
            await ensure_field(page, "#pin1", p1, rng)
            await ensure_field(page, "#pin2", p2, rng)
            await page.click("#go")
            outcome = await wait_submit_result(page)
            if outcome["ok"]:
                log("submit_ok", outcome["status"])
                return {"ok": True, "status": outcome["status"], "again": outcome["again"], "mode": mode, "errors": errors, "covered": covered}
            err_text = outcome["error"]
            errors.append(err_text)
            covered_now = await err_covered(page)
            if covered_now:
                covered += 1
            log("submit_err", err_text)
            if attempt.fail():
                return {"ok": False, "reason": "fails4", "mode": mode, "errors": errors, "covered": covered}

            if covered_now:
                status, blind_outcome, delta = await blind_retries_then_notice(page, rng, attempt, errors, log)
                covered += delta
                if status == "ok":
                    assert blind_outcome is not None  # noqa: S101 -- blind_retries_then_notice 契約保證 ok 必帶 outcome
                    return {"ok": True, "status": blind_outcome["status"], "again": blind_outcome["again"],
                            "mode": mode, "errors": errors, "covered": covered}
                if status in ("fails4", "timeout_120s"):
                    return {"ok": False, "reason": status, "mode": mode, "errors": errors, "covered": covered}
                wait_s = min(rng.uniform(20, 45), max(0.0, attempt.deadline - time.time()))
            else:
                wait_s = min(rng.uniform(3, 6), max(0.0, attempt.deadline - time.time()))
            await asyncio.sleep(wait_s)
            if attempt.time_up():
                return {"ok": False, "reason": "timeout_120s", "mode": mode, "errors": errors, "covered": covered}
            if "PIN 要 4 到 6 位數字" in err_text:
                defects["pin_len"] = False
            elif "兩次 PIN 不一樣" in err_text:
                defects["pin_mismatch"] = False
            elif "名冊不符" in err_text:
                defects["name_typo"] = False
            # 其餘未預期的錯誤：不改欄位，原樣重送，交給 fails 計數把關
    else:
        # A2（2026-09-21 裁定題三 A）：已綁裝置回簽不驗 PIN，student.html 的回簽模式已經拿掉 PIN 欄位，
        # 畫面只剩姓名學號＋「簽到」鈕；掃到→送出之間只剩這一次點按，不再有欄位可填。
        # 落地→送出的延遲已經在函式開頭套過 sample_fill_delay(mode="return", ...)（既有分布，未變動）。
        while True:
            if attempt.time_up():
                return {"ok": False, "reason": "timeout_120s", "mode": mode, "errors": errors, "covered": covered}
            await page.click("#go")
            outcome = await wait_submit_result(page)
            if outcome["ok"]:
                log("submit_ok", outcome["status"])
                return {"ok": True, "status": outcome["status"], "again": outcome["again"], "mode": mode, "errors": errors, "covered": covered}
            err_text = outcome["error"]
            errors.append(err_text)
            covered_now = await err_covered(page)
            if covered_now:
                covered += 1
            log("submit_err", err_text)
            if attempt.fail():
                return {"ok": False, "reason": "fails4", "mode": mode, "errors": errors, "covered": covered}

            if covered_now:
                status, blind_outcome, delta = await blind_retries_then_notice(page, rng, attempt, errors, log)
                covered += delta
                if status == "ok":
                    assert blind_outcome is not None  # noqa: S101 -- blind_retries_then_notice 契約保證 ok 必帶 outcome
                    return {"ok": True, "status": blind_outcome["status"], "again": blind_outcome["again"],
                            "mode": mode, "errors": errors, "covered": covered}
                if status in ("fails4", "timeout_120s"):
                    return {"ok": False, "reason": status, "mode": mode, "errors": errors, "covered": covered}
                await asyncio.sleep(min(rng.uniform(20, 45), max(0.0, attempt.deadline - time.time())))
            else:
                await asyncio.sleep(min(rng.uniform(3, 6), max(0.0, attempt.deadline - time.time())))


async def land_student(page, teacher_ctx, base_url: str, sid: int, crypto: SessionCrypto,
                        profile: str, rng: random.Random, attempt: Attempt, log,
                        projector_close_at: float | None) -> dict:
    """處理「舉手機→掃到→送出」到「落地成功」為止，含過期重掃／改六位數。
    回傳 {"landed": bool, "reason": str|None, "landed_200": int, "landed_410": int,
          "switched_code": bool, "code_attempts": int}
    projector_close_at：投影頁關閉的絕對時間（--projector-seconds 有設才非 None）。過了這個時間，
    模擬的學生「掃不到 QR」——沒有新的落地網址可掃，也看不到投影上換過的六位數，直接放棄
    （reason="projector_closed"）。已經落地成功、正在填表的學生不受影響：那時 land_student
    早就回傳了，接下來走的是 fill_and_submit_loop，不會再進到這個迴圈。"""
    n_200 = n_410 = code_attempts = 0
    switched = False
    while True:
        if attempt.time_up():
            return {"landed": False, "reason": "timeout_120s", "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        if projector_close_at is not None and time.time() >= projector_close_at:
            return {"landed": False, "reason": "projector_closed", "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        t_scan = time.time()
        url, _code = crypto.at(t_scan)
        delay = sample_scan_delay(profile, rng)
        remaining = attempt.deadline - time.time()
        if delay >= remaining:
            await asyncio.sleep(max(0.0, remaining))
            return {"landed": False, "reason": "timeout_120s", "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        await asyncio.sleep(delay)
        log("scan_delay", f"{delay:.1f}s")
        try:
            await page.goto(url, wait_until="load", timeout=8000)
        except Exception as e:  # noqa: BLE001
            log("goto_error", str(e)[:150])
            continue
        expired = await page.locator("h1:has-text('QR 已過期')").count() > 0
        if not expired:
            n_200 += 1
            log("landing_200")
            return {"landed": True, "reason": None, "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        n_410 += 1
        log("landing_410")
        if attempt.fail():
            return {"landed": False, "reason": "fails4", "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        if rng.random() < 0.5:
            log("retry_rescan")
            continue
        # 改輸六位數：從老師 API 取當下的 code，等 4–9 秒打字時間後送出
        switched = True
        code_attempts += 1
        wait_s = min(rng.uniform(4, 9), max(0.0, attempt.deadline - time.time()))
        code = await teacher_qr_code(teacher_ctx, base_url, sid)
        await asyncio.sleep(wait_s)
        if attempt.time_up():
            return {"landed": False, "reason": "timeout_120s", "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        await page.goto(f"{base_url}/c", wait_until="load")
        try:
            async with page.expect_navigation(wait_until="load", timeout=8000):
                await page.click("#otp input >> nth=0")
                await page.keyboard.type(code, delay=60)
        except Exception as e:  # noqa: BLE001 -- 例如提交沒觸發整頁導頁，用下面的畫面判斷兜底
            log("code_nav_warn", str(e)[:150])
        if await page.locator("#firstFields, #returnFields").count() > 0:
            log("landing_via_code")
            return {"landed": True, "reason": None, "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        err_txt = ""
        if await page.locator("p.err").count() > 0:
            err_txt = await page.locator("p.err").first.inner_text()
        log("code_failed", err_txt)
        if attempt.fail():
            return {"landed": False, "reason": "fails4", "landed_200": n_200, "landed_410": n_410,
                    "switched_code": switched, "code_attempts": code_attempts}
        # 六位數也失敗：回到掃碼路徑再試一次（在同一個 while 迴圈內，直到給棄或成功）


async def run_student_week(student: dict, page, teacher_ctx, base_url: str, sid: int,
                            crypto: SessionCrypto, profile: str, rng: random.Random,
                            week_opened_at: float, projector_close_at: float | None) -> dict:
    events: list[dict] = []

    def log(kind: str, detail: str = "") -> None:
        events.append({"t": round(time.time() - week_opened_at, 2), "type": kind, "detail": detail})

    raise_at = week_opened_at + rng.uniform(0, 90)
    now = time.time()
    if raise_at > now:
        await asyncio.sleep(raise_at - now)
    raise_at = time.time()
    log("raise_phone")
    attempt = Attempt(deadline=raise_at + 120)

    land = await land_student(page, teacher_ctx, base_url, sid, crypto, profile, rng, attempt, log, projector_close_at)
    if not land["landed"]:
        return {
            "student_id": student["student_id"], "name": student["name"], "device": student["device"],
            "raised_at_rel": round(raise_at - week_opened_at, 2), "outcome": "gave_up",
            "gave_up_reason": land["reason"], "success_latency": None,
            "landed_200": land["landed_200"], "landed_410": land["landed_410"],
            "switched_code": land["switched_code"], "code_attempts": land["code_attempts"],
            "errors": [], "covered": 0, "total_fails": attempt.fails, "mode": None, "events": events,
        }

    fill = await fill_and_submit_loop(page, student, rng, attempt, log, profile)
    if fill["ok"]:
        status = fill["status"]
        outcome = "present" if status == "出席" else "late" if status == "遲到" else "pending" if status == "待確認" else status
        success_latency = round(time.time() - raise_at, 2)
        return {
            "student_id": student["student_id"], "name": student["name"], "device": student["device"],
            "raised_at_rel": round(raise_at - week_opened_at, 2), "outcome": outcome, "gave_up_reason": None,
            "success_latency": success_latency,
            "landed_200": land["landed_200"], "landed_410": land["landed_410"],
            "switched_code": land["switched_code"], "code_attempts": land["code_attempts"],
            "errors": fill["errors"], "covered": fill["covered"], "total_fails": attempt.fails,
            "mode": fill["mode"], "events": events,
        }
    return {
        "student_id": student["student_id"], "name": student["name"], "device": student["device"],
        "raised_at_rel": round(raise_at - week_opened_at, 2), "outcome": "gave_up",
        "gave_up_reason": fill["reason"], "success_latency": None,
        "landed_200": land["landed_200"], "landed_410": land["landed_410"],
        "switched_code": land["switched_code"], "code_attempts": land["code_attempts"],
        "errors": fill["errors"], "covered": fill["covered"], "total_fails": attempt.fails,
        "mode": fill["mode"], "events": events,
    }


# ───────────────────────── 漏斗彙整與報表 ─────────────────────────

def percentile(data: list[float], p: float) -> float | None:
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def build_funnel(week_idx: int, n_students: int, results: list[dict], teacher_state_data: dict,
                  is_bind_week: bool = False) -> dict:
    """is_bind_week（B030）：第 1 週改開綁定場次，過關條件改成「綁定窗內完成綁定的比例 ≥95%」，
    不看 p90 ≤60 秒（綁定場次本來就給 bind_minutes，預設 10 分鐘，不是 3 分鐘出席窗）；
    p90 仍照算、照印，只是不算進 gate_ok。"""
    err_counter: Counter = Counter()
    covered_total = 0
    landed_200 = landed_410 = code_attempts = 0
    switched_students = 0
    outcomes: Counter = Counter()
    gave_up_reasons: Counter = Counter()
    latencies: list[float] = []
    for r in results:
        landed_200 += r["landed_200"]
        landed_410 += r["landed_410"]
        code_attempts += r["code_attempts"]
        if r["switched_code"]:
            switched_students += 1
        covered_total += r["covered"]
        for e in r["errors"]:
            err_counter[classify_error(e)] += 1
        outcomes[r["outcome"]] += 1
        if r["outcome"] == "gave_up":
            gave_up_reasons[r["gave_up_reason"] or "unknown"] += 1
        if r["success_latency"] is not None:
            latencies.append(r["success_latency"])

    present = outcomes.get("present", 0)
    late = outcomes.get("late", 0)
    success = present + late + outcomes.get("pending", 0)
    gave_up = outcomes.get("gave_up", 0)
    p50, p90, pmax = percentile(latencies, 0.5), percentile(latencies, 0.9), (max(latencies) if latencies else None)
    present_rate = present / n_students if n_students else 0.0
    gate_p90_ok = p90 is not None and p90 <= 60
    gate_ok = present_rate >= 0.95 if is_bind_week else (present_rate >= 0.95 and gate_p90_ok)

    return {
        "week": week_idx, "n_students": n_students, "is_bind_week": is_bind_week,
        "raised": len(results),
        "landed_200": landed_200, "landed_410": landed_410,
        "landed_410_pct": (landed_410 / (landed_200 + landed_410) * 100) if (landed_200 + landed_410) else 0.0,
        "switched_code_students": switched_students, "code_attempts": code_attempts,
        "error_counts": dict(err_counter), "covered_total": covered_total,
        "success": success, "present": present, "late": late, "pending": outcomes.get("pending", 0),
        "gave_up": gave_up, "gave_up_reasons": dict(gave_up_reasons),
        "latency_p50": p50, "latency_p90": p90, "latency_max": pmax,
        "teacher_state_counts": teacher_state_data.get("counts", {}),
        "present_rate": present_rate, "gate_p90_ok": gate_p90_ok, "gate_ok": gate_ok,
    }


def fmt_s(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}s"


def print_funnel_table(f: dict) -> None:
    w = f["week"]
    is_bind = f.get("is_bind_week", False)
    print(f"\n== 第 {w} 週漏斗（{f['n_students']} 人{'，綁定場次' if is_bind else ''}）==")
    rate_label = "完成綁定比例（gate 用）" if is_bind else "出席比例（gate 用）"
    p90_label = "p90 ≤ 60 秒（本週不計入 gate，綁定場次給 bind_minutes）" if is_bind else "p90 ≤ 60 秒"
    rows = [
        ("舉手機人數", f"{f['raised']} / {f['n_students']}"),
        ("QR 落地 200", str(f["landed_200"])),
        ("QR 落地 410（過期）", f"{f['landed_410']}（{f['landed_410_pct']:.0f}%）"),
        ("改走六位數的人數", str(f["switched_code_students"])),
        ("六位數送出次數", str(f["code_attempts"])),
        ("錯誤被鍵盤蓋住次數", str(f["covered_total"])),
        ("成功（出席＋遲到＋待確認）", str(f["success"])),
        ("　其中出席", str(f["present"])),
        ("　其中遲到", str(f["late"])),
        ("　其中待確認", str(f["pending"])),
        ("gave_up 人數", str(f["gave_up"])),
        ("成功延遲 p50／p90／max", f"{fmt_s(f['latency_p50'])} / {fmt_s(f['latency_p90'])} / {fmt_s(f['latency_max'])}"),
        (rate_label, f"{f['present_rate']*100:.1f}%（門檻 ≥95%）"),
        (p90_label, "是" if f["gate_p90_ok"] else "否"),
        ("本週 gate", "PASS" if f["gate_ok"] else "FAIL"),
    ]
    width = max(len(k) for k, _ in rows) + 2
    for k, v in rows:
        print(f"  {k:<{width}}{v}")
    print("  ※ 實地另有 22% 落地後未送出、成因未知（FIELD_2026-09-10.md），本模擬未納入")
    if f["error_counts"]:
        print("  錯誤訊息分類：")
        for k, v in sorted(f["error_counts"].items(), key=lambda kv: -kv[1]):
            print(f"    {k:<20}{v}")
    if f["gave_up_reasons"]:
        print("  gave_up 原因：")
        for k, v in sorted(f["gave_up_reasons"].items(), key=lambda kv: -kv[1]):
            print(f"    {k:<20}{v}")
    tc = f["teacher_state_counts"]
    if tc:
        print(f"  老師端 state API 對帳：出席 {tc.get('present', 0)}、遲到 {tc.get('late', 0)}、"
              f"待確認 {tc.get('pending', 0)}、缺席 {tc.get('absent', 0)}、未簽到 {tc.get('none', 0)}")


# ───────────────────────── 主流程 ─────────────────────────

def build_students(n: int, seed: int) -> list[dict]:
    base_names = ["陳大文", "吳家豪", "林小華", "張志明"]
    names = base_names + [f"測試{i}" for i in range(len(base_names) + 1, n + 1)]
    rng = random.Random(seed)
    students = []
    for i in range(1, n + 1):
        device = "iphone" if rng.random() < 0.45 else "android"
        pin = f"{rng.randint(1000, 9999):04d}"
        students.append({
            "student_id": f"9994B{i:03d}", "name": names[i - 1], "device": device, "pin": pin,
            "android_kind": rng.choice(ANDROID_DEVICES) if device == "android" else None,
        })
    return students


async def make_student_context(p, chromium, webkit, student: dict):
    if student["device"] == "iphone":
        desc = p.devices["iPhone 13"]
        ctx = await webkit.new_context(**desc)
    else:
        desc = p.devices[student["android_kind"]]
        ctx = await chromium.new_context(**desc)
    page = await ctx.new_page()
    return ctx, page


async def run_rehearsal(args: argparse.Namespace) -> int:
    # app_dir 的 resolve()/is_file() 是阻塞的 pathlib 操作（ASYNC240），故在 main() 同步階段先做好，
    # 這裡只接已驗證過的 Path 字串。
    app_dir = Path(args.app_dir)
    srv = start_server(args.students, args.seed, app_dir)
    print(f"[rehearsal] 伺服器 {srv.base_url}（暫存庫 {srv.db_path}，程式碼目錄 {app_dir}）")
    exit_code = 0
    all_funnels: list[dict] = []
    all_students_raw = build_students(args.students, args.seed)

    try:
        async with async_playwright() as p:
            chromium = await p.chromium.launch(headless=not args.headed)
            webkit = await p.webkit.launch(headless=not args.headed)
            teacher_ctx = await chromium.new_context(viewport={"width": 1280, "height": 800})
            teacher_page = await teacher_ctx.new_page()
            await teacher_login(teacher_page, srv.base_url)

            student_ctxs: dict[str, tuple] = {}
            for stu in all_students_raw:
                ctx, page = await make_student_context(p, chromium, webkit, stu)
                student_ctxs[stu["student_id"]] = (ctx, page)

            projector_page = None
            try:
                for week in range(1, args.weeks + 1):
                    if projector_page is not None:
                        await projector_page.close()
                    is_bind_week = week == 1  # B030：第 1 週改開綁定場次，第 2 週起回簽（不輸 PIN）
                    sid = await open_week_session(teacher_page, srv.base_url, kind="bind" if is_bind_week else "normal")
                    week_opened_at = time.time()
                    projector_page = await open_projector(teacher_ctx, srv.base_url, sid)
                    crypto = read_session_crypto(srv.db_path, srv.secret, srv.base_url, sid)
                    projector_close_at = (week_opened_at + args.projector_seconds) if args.projector_seconds else None
                    proj_note = f"，投影頁 {args.projector_seconds:.0f}s 後關閉" if args.projector_seconds else ""
                    kind_note = "，綁定場次" if is_bind_week else ""
                    print(f"[rehearsal] 第 {week} 週 session {sid} 已開（qr_interval={crypto.iv}s{proj_note}{kind_note}）")

                    tasks = []
                    for stu in all_students_raw:
                        ctx, page = student_ctxs[stu["student_id"]]
                        student_rng = seeded_rng(args.seed, week, stu["student_id"])
                        tasks.append(asyncio.wait_for(
                            run_student_week(stu, page, teacher_ctx, srv.base_url, sid, crypto,
                                             args.profile, student_rng, week_opened_at, projector_close_at),
                            timeout=240))
                    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
                    results = []
                    for stu, r in zip(all_students_raw, raw_results):
                        if isinstance(r, Exception):
                            print(f"[rehearsal] 學生 {stu['student_id']} 的模擬丟出例外：{r!r}", file=sys.stderr)
                            results.append({
                                "student_id": stu["student_id"], "name": stu["name"], "device": stu["device"],
                                "raised_at_rel": None, "outcome": "gave_up", "gave_up_reason": f"script_error:{r!r}"[:200],
                                "success_latency": None, "landed_200": 0, "landed_410": 0, "switched_code": False,
                                "code_attempts": 0, "errors": [], "covered": 0, "total_fails": 0, "mode": None, "events": [],
                            })
                        else:
                            results.append(r)

                    await close_week_session(teacher_page)
                    state_data = await teacher_state(teacher_ctx, srv.base_url, sid)
                    funnel = build_funnel(week, args.students, results, state_data, is_bind_week=is_bind_week)
                    funnel["students"] = results
                    all_funnels.append(funnel)
                    print_funnel_table(funnel)
                    if not funnel["gate_ok"]:
                        exit_code = 1
            finally:
                if projector_page is not None:
                    await projector_page.close()
                for ctx, _page in student_ctxs.values():
                    await ctx.close()
                await teacher_ctx.close()
                await chromium.close()
                await webkit.close()
    finally:
        teardown_server(srv)

    if args.json:
        out = {
            "args": {"students": args.students, "profile": args.profile, "weeks": args.weeks, "seed": args.seed,
                      "projector_seconds": args.projector_seconds, "app_dir": str(app_dir)},
            "weeks": all_funnels,
        }
        await asyncio.to_thread(Path(args.json).write_text, json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[rehearsal] 完整資料寫到 {args.json}")

    print(f"\n==> {'全部週次通過 gate' if exit_code == 0 else '至少一週沒過 gate（照實回報，不調參數）'}")
    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="教室彩排模擬器：真瀏覽器＋真實時鐘模擬課堂點名，產出漏斗報告")
    ap.add_argument("--students", type=int, default=30)
    ap.add_argument("--profile", choices=["field", "fast"], default="field")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--projector-seconds", type=float, default=None,
                     help="投影頁曝光秒數，超過後模擬學生掃不到新 QR（預設不限＝維持現有行為；實地 300s）")
    ap.add_argument("--app-dir", type=str, default=str(BASE_DIR),
                     help="uvicorn/manage.py/templates 起算的目錄（預設＝這支腳本所在的 services/attend）")
    ap.add_argument("--json", type=str, default=None)
    ap.add_argument("--headed", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app_dir = Path(args.app_dir).resolve()
    if not (app_dir / "app.py").is_file():
        print(f"[rehearsal] --app-dir 給的路徑不對：{app_dir / 'app.py'} 不存在", file=sys.stderr)
        return 2
    args.app_dir = str(app_dir)
    return asyncio.run(run_rehearsal(args))


# ───────────────────────── pytest 收口（不用 test_ 檔名，平常不會被自動收） ─────────────────────────

@pytest.mark.rehearsal
def test_rehearsal_smoke():
    """tests/gate.sh --full 才會跑；小班快跑一次當回歸檢查，發現流程壞掉就失敗。"""
    rc = main(["--students", "10", "--profile", "fast", "--weeks", "1", "--seed", "1"])
    assert rc == 0


if __name__ == "__main__":
    sys.exit(main())

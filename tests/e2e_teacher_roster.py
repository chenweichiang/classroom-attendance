"""老師端前端回歸。
被 tests/e2e_teacher_friction_playwright.py 結尾以子程序呼叫（同 e2e_manual_checkin.py 的手法），也可以獨立跑。
前提同該檔：乾淨庫、ATTEND_TEST_BASE、NTUB-CD 名冊（姓名「測試N」，學號 9994A0NN）。
名單列位移的量測要等「輪詢真的重繪過」才算數：#mid 有焦點時 busy() 會暫停輪詢 5 秒，所以等的是
loadSeq 前進＋該列狀態已套用的條件，不是固定秒數。
"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:64890")
PW = os.environ.get("ATTEND_TEACHER_PASSWORD", "pw")
FAIL = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL "), msg)
    if not cond:
        FAIL.append(msg)


def close_all_live(req):
    html = req.get(f"{B}/t").text()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        req.post(f"{B}/api/t/session/{s}/close")


def open_session(req, cid):
    r = req.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
    return int(re.search(r"/t/session/(\d+)", r.url).group(1))


def scan_checkin(browser, treq, sid, stu, name, pin="1234"):
    """學生掃碼簽到（一個一次性 context ＝ 一支手機）。"""
    ctx = browser.new_context()
    try:
        code = treq.get(f"{B}/api/t/session/{sid}/qr").json()["code"]
        html = ctx.request.post(f"{B}/c", form={"code": code}).text()
        grant = re.search(r'GRANT="([^"]+)"', html).group(1)
        j = ctx.request.post(f"{B}/api/checkin", form={"grant": grant, "student_id": stu, "name": name, "pin": pin, "pin2": pin, "hints": "h"}).json()
        return j
    finally:
        ctx.close()


def rows_of(req, sid):
    return {r["student_id"]: r for r in req.get(f"{B}/api/t/session/{sid}/state").json()["rows"]}


def teacher_page(browser, kwargs, storage):
    ctx = browser.new_context(**kwargs, storage_state=storage)
    return ctx, ctx.new_page()


def wait_applied(pg, stu, status, timeout=20000):
    """等到輪詢真的重繪過：loadSeq 前進、不在飛行中、而且那一列的狀態已套用到畫面上的 state。"""
    seq0 = pg.evaluate("loadSeq")
    pg.wait_for_function("([s,st,q])=>loadSeq>q&&!inflight&&state.rows.find(r=>r.student_id===s)&&state.rows.find(r=>r.student_id===s).status===st",
                         arg=[stu, status, seq0], timeout=timeout)


def run_row_shift(p, chrome, webkit, treq, storage, cid):
    cases = [("WebKit iPhone 13", webkit, dict(p.devices["iPhone 13"])),
             ("Chromium 390", chrome, dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=3))]
    for label, br, kw in cases:
        close_all_live(treq)
        sid = open_session(treq, cid)
        ctx, pg = teacher_page(br, kw, storage)
        try:
            pg.goto(f"{B}/t/session/{sid}")
            pg.wait_for_selector("#rows tr")
            # 同姓：「測試2」開頭的有 測試20～測試29，候選最多列 8 位，目標取第 4 位（9994A023）
            pg.tap("#mid"); pg.keyboard.type("測試2")
            pg.wait_for_function("document.querySelectorAll('#mcands button').length>=6")
            target_row = pg.locator("#mcands > div", has_text="9994A023")
            target_row.scroll_into_view_if_needed()  # 目標要在可視區內，原座標 tap 才點得到
            box0 = target_row.locator("button", has_text="出席").bounding_box()
            check(box0 is not None, f"[{label}]前置，目標候選列有「出席」鈕")
            # 另一位（排在目標前面的 9994A021）簽到，等輪詢真的重繪過
            j = scan_checkin(br, treq, sid, "9994A021", "測試21")
            check(j.get("ok") is True, f"[{label}]前置，9994A021 簽到成功：{j}")
            wait_applied(pg, "9994A021", "present")
            box1 = target_row.locator("button", has_text="出席").bounding_box()
            dy = abs(box1["y"] - box0["y"])
            check(dy <= 1, f"[{label}]另一位簽到並輪詢重繪後，目標「出席」鈕 y 位移 {dy:.1f}px ≤ 1（{box0['y']:.1f}→{box1['y']:.1f}）")
            # 在原座標 tap：被記的必須是目標學生
            pg.touchscreen.tap(box0["x"] + box0["width"] / 2, box0["y"] + box0["height"] / 2)
            pg.wait_for_function("state.rows.find(r=>r.student_id==='9994A023').status==='present'", timeout=10000)
            rs = rows_of(treq, sid)
            check(rs["9994A023"]["status"] == "present" and rs["9994A023"]["source"] == "manual", f"[{label}]原座標 tap 記到的是目標 9994A023（{rs['9994A023']['status']}/{rs['9994A023']['source']}）")
            others = [s for s in ("9994A020", "9994A022", "9994A024") if rs[s]["status"]]
            check(not others, f"[{label}]沒有記到別的同姓同學：{others}")
            check(rs["9994A021"]["source"] == "qr", f"[{label}]掃碼出席的 9994A021 沒有被改成手動：{rs['9994A021']['source']}")
        except Exception as e:
            check(False, f"[{label}]拋例外 {e!r}")
        finally:
            ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")

    # 手動簽到區在名單之上，卡住名單的人可以點
    close_all_live(treq)
    sid = open_session(treq, cid)
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    try:
        pg.goto(f"{B}/t/session/{sid}"); pg.wait_for_selector("#rows tr")
        order = pg.evaluate("(()=>{const m=document.getElementById('manualSection').getBoundingClientRect().top;"
                            "const t=document.getElementById('rowsTable').getBoundingClientRect().top;return [m,t]})()")
        check(order[0] < order[1], f"#manualSection 在名單表格之上，y={order}")
        check(pg.evaluate("(()=>{const h=[...document.querySelectorAll('h2')];const mi=h.findIndex(x=>x.textContent.startsWith('手動簽到'));"
                          "const ni=h.findIndex(x=>x.textContent.startsWith('名單'));return mi>=0&&ni>=0&&mi<ni})()"), "「手動簽到」的 h2 在「名單」的 h2 之前")
        # 造一個卡住的人：9994A040 簽到綁定，下一場用另一支手機打錯 PIN 兩次
        treq.post(f"{B}/api/t/session/{sid}/close")
        sid2 = open_session(treq, cid)
        j = scan_checkin(chrome, treq, sid2, "9994A040", "測試40", pin="1234")
        check(j.get("ok") is True, f"前置，9994A040 簽到並綁定：{j}")
        treq.post(f"{B}/api/t/session/{sid2}/close")
        sid3 = open_session(treq, cid)
        for wrong in ("1111", "2222"):
            ctx2 = chrome.new_context()
            code = treq.get(f"{B}/api/t/session/{sid3}/qr").json()["code"]
            html = ctx2.request.post(f"{B}/c", form={"code": code}).text()
            g = re.search(r'GRANT="([^"]+)"', html).group(1)
            ctx2.request.post(f"{B}/api/checkin", form={"grant": g, "student_id": "9994A040", "name": "", "pin": wrong, "pin2": "", "hints": "h"})
            ctx2.close()
        pg.goto(f"{B}/t/session/{sid3}"); pg.wait_for_selector("#rows tr")
        pg.wait_for_selector("#stuck .stuckpick", timeout=15000)
        pg.locator("#stuck .stuckpick", has_text="9994A040").click()
        check(pg.locator("#mid").input_value() == "9994A040", f"點卡住名單的人，學號填進 #mid，實得 {pg.locator('#mid').input_value()!r}")
        check(pg.evaluate("document.activeElement.id") == "mid", "焦點在 #mid")
        check(pg.locator("#mcands > div", has_text="9994A040").count() == 1, "候選列已更新")
        check(rows_of(treq, sid3)["9994A040"]["status"] == "", "點下去不自動送出")
        treq.post(f"{B}/api/t/session/{sid3}/close")
    except Exception as e:
        check(False, f"卡住名單／版面段拋例外{e!r}")
    finally:
        ctx.close()


def run_late_confirm(p, chrome, treq, storage, cid):
    close_all_live(treq)
    sid = open_session(treq, cid)
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    try:
        pg.goto(f"{B}/t/session/{sid}"); pg.wait_for_selector("#rows tr")
        j = scan_checkin(chrome, treq, sid, "9994A041", "測試41")
        check(j.get("ok") is True, f"前置，9994A041 掃碼簽到：{j}")
        pg.click("#mid")
        # 全形學號找得到
        pg.keyboard.type("９９９４Ａ０４１")
        pg.wait_for_function("document.getElementById('mcands').innerText.includes('9994A041')", timeout=8000)
        check(True, "#mid 打全形學號找得到人")
        wait_applied(pg, "9994A041", "present")
        txt = pg.locator("#mcands").inner_text()
        check("目前：出席（掃碼）" in txt, f"候選列顯示目前狀態，實得 {txt!r}")
        # 已經是出席又按「出席」：沒有 confirm、回饋「已經是出席，沒有變更」、來源仍是 qr
        dlg = []
        pg.on("dialog", lambda d: (dlg.append(d.message), d.dismiss()))
        pg.locator("#mcands button", has_text="出席").click()
        pg.wait_for_function("document.getElementById('mdone').innerText.includes('已經是出席')", timeout=8000)
        check("9994A041" in pg.locator("#mdone").inner_text() and "沒有變更" in pg.locator("#mdone").inner_text(), f"unchanged 的回饋字樣，實得 {pg.locator('#mdone').inner_text()!r}")
        check(dlg == [], f"同狀態不跳 confirm，實得 {dlg}")
        check(rows_of(treq, sid)["9994A041"]["source"] == "qr", "按同一個狀態沒有把掃碼出席改成手動")
        # 按「遲到」（降級）：跳 confirm，取消就不送
        pg.fill("#mid", "")  # 清空只是為了重打；輸入行為本身在上面逐字打過
        pg.click("#mid"); pg.keyboard.type("A041")
        pg.wait_for_function("document.getElementById('mcands').innerText.includes('9994A041')")
        posts = []
        pg.on("request", lambda r: posts.append(r.url) if r.url.endswith("/status") and r.method == "POST" else None)
        pg.locator("#mcands button", has_text="遲到").click()
        # click() 會等到 confirm 對話框處理完才返回，這裡不用再等
        check(len(dlg) == 1 and "目前是出席（掃碼），確定改成遲到？" in dlg[0], f"降級前跳 confirm，訊息 {dlg}")
        check(posts == [], f"取消 confirm 不送出，實得 {posts}")
        check(rows_of(treq, sid)["9994A041"]["status"] == "present", "取消後狀態仍是出席")
        # 接受 confirm 才送
        pg.evaluate("window.confirm=()=>true")
        pg.locator("#mcands button", has_text="遲到").click()
        pg.wait_for_function("state.rows.find(r=>r.student_id==='9994A041').status==='late'", timeout=10000)
        check("已記：9994A041" in pg.locator("#mdone").inner_text() and "遲到" in pg.locator("#mdone").inner_text(), f"成功後 #mdone 顯示已記，實得 {pg.locator('#mdone').inner_text()!r}")
        pg.fill("#mid", "")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")


def run_double_tap(p, chrome, treq, storage, cid):
    close_all_live(treq)
    sid = open_session(treq, cid)
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 900}), storage)
    try:
        pg.goto(f"{B}/t/session/{sid}"); pg.wait_for_selector("#rows tr")
        posts = []
        pg.on("request", lambda r: posts.append(r.url) if r.url.endswith("/status") and r.method == "POST" else None)
        # 連按兩下只送一次、不 500
        pg.click("#mid"); pg.keyboard.type("A042")
        pg.wait_for_function("document.getElementById('mcands').innerText.includes('9994A042')")
        statuses = []
        # 本機回應不到 30ms 就回來了，第二下根本點不到「送出中」的狀態；頁面內把 fetch 的回應固定延後 400ms，
        # 讓「送出中連按」確實發生（延遲是為了製造被測情境，不是在猜時間）
        pg.evaluate("(()=>{const f=window.fetch;window.fetch=(u,o)=>f(u,o).then(r=>new Promise(res=>setTimeout(()=>res(r),400)))})()")
        pg.on("response", lambda r: statuses.append(r.status) if r.url.endswith("/status") and r.request.method == "POST" else None)
        pg.evaluate("""()=>{const q=()=>[...document.querySelectorAll('#mcands button')].find(b=>b.textContent.trim()==='出席');
          q().click();setTimeout(()=>{const b=q();if(b)b.click()},30);}""")
        pg.wait_for_function("state.rows.find(r=>r.student_id==='9994A042').status==='present'", timeout=10000)
        for _ in range(60):  # 等每個送出的請求都有回應（條件，不是固定秒數）
            if len(statuses) >= len(posts):
                break
            pg.wait_for_timeout(50)
        check(len(posts) == 1, f"0.03 秒連按兩下只送出 1 個 status 請求，實得 {len(posts)}")
        check(all(s_ == 200 for s_ in statuses), f"回應都不是 500，實得 {statuses}")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")


def run_reopen_reload(p, chrome, treq, storage, cid):
    """刪除此次後同號重開：開著的投影頁要整頁重新載入（標題與課名跟著換），不能沿用舊頁面。"""
    close_all_live(treq)
    sid = open_session(treq, cid)
    ctx, pg = teacher_page(chrome, dict(viewport={"width": 1280, "height": 720}), storage)
    try:
        pg.goto(f"{B}/t/projector/{sid}")
        pg.wait_for_function("document.querySelector('#qr svg')!==null")
        pg.evaluate("window.__reload_marker=1")
        treq.post(f"{B}/api/t/session/{sid}/delete")
        pg.wait_for_function("document.getElementById('msg').innerText.includes('已被刪除')", timeout=8000)
        sid2 = open_session(treq, cid)
        check(sid2 == sid, f"前置，刪除後重開沿用同一個場次號（{sid}→{sid2}）")
        # 投影頁每秒輪詢；重開後 2 次輪詢內（給到 3 秒）頁面要重新載入：自訂的標記消失，QR 重新出現
        pg.wait_for_function("window.__reload_marker===undefined", timeout=3500)
        check(True, "重開後投影頁整頁重新載入（頁面內的舊標記消失）")
        pg.wait_for_function("document.querySelector('#qr svg')!==null")
        check("已被刪除" not in pg.locator("#msg").inner_text(), "重新載入後不再顯示「已被刪除」")
    except Exception as e:
        check(False, f"拋例外{e!r}")
    finally:
        ctx.close()
        treq.post(f"{B}/api/t/session/{sid}/close")


def main():
    with sync_playwright() as p:
        chrome = p.chromium.launch()
        webkit = p.webkit.launch()
        tctx = chrome.new_context(viewport={"width": 1280, "height": 900})
        r = tctx.request.post(f"{B}/t/login", form={"password": PW, "next": "/t"})
        assert r.ok, r.status
        storage = tctx.storage_state()
        treq = tctx.request
        cid = int(re.search(r"course/(\d+)/open", treq.get(f"{B}/t").text()).group(1))
        run_row_shift(p, chrome, webkit, treq, storage, cid)
        run_late_confirm(p, chrome, treq, storage, cid)
        run_double_tap(p, chrome, treq, storage, cid)
        run_reopen_reload(p, chrome, treq, storage, cid)
        chrome.close(); webkit.close()


main()
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)

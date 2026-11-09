"""學生端前端回歸。
被 tests/e2e_friction_playwright.py 結尾以子程序呼叫（同 e2e_student_front.py 的手法），也可以獨立跑。
前提同該檔：乾淨庫、ATTEND_TEST_BASE、NTUB-CD 名冊。老師端只用 API context。
輸入行為一律 tap 欄位後 keyboard.type 逐字打，不用 fill()（fill 會先清空欄位，量不到 PIN 欄殘留的問題）。
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


def close_all_live(teacher):
    html = teacher.request.get(f"{B}/t").text()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        teacher.request.post(f"{B}/api/t/session/{s}/close")


def open_session(teacher, cid):
    r = teacher.request.post(f"{B}/t/course/{cid}/open", form={"kind": "normal"})
    return int(re.search(r"/t/session/(\d+)", r.url).group(1))


def qr_code(teacher, sid):
    return teacher.request.get(f"{B}/api/t/session/{sid}/qr").json()["code"]


def enter(page, code):
    page.goto(f"{B}/c")
    page.click("#otp input >> nth=0")
    page.keyboard.type(code)
    page.wait_for_selector("#student_id, #returnFields")


def type_into(page, sel, text):
    page.tap(sel)
    page.keyboard.type(text)


def err_text(page):
    return page.locator("#err").inner_text()


def wait_err(page, needle):
    page.wait_for_function("n=>document.getElementById('err').innerText.includes(n)", arg=needle, timeout=8000)


def bind_first(browser, device, teacher, cid, sid_no, name, pin):
    """setup：在一個一次性 context 把學號綁上並設 PIN（簽到後關掉 context ＝ 清掉 cookie）。"""
    sid = open_session(teacher, cid)
    ctx = browser.new_context(**device)
    pg = ctx.new_page()
    enter(pg, qr_code(teacher, sid))
    pg.fill("#student_id", sid_no); pg.fill("#name", name); pg.fill("#pin1", pin); pg.fill("#pin2", pin)
    pg.click("#go")
    pg.wait_for_selector("#receipt", state="visible")
    ctx.close()
    teacher.request.post(f"{B}/api/t/session/{sid}/close")


def main():
    with sync_playwright() as p:
        webkit = p.webkit.launch()
        device = p.devices["iPhone 13"]
        tctx = webkit.new_context(viewport={"width": 1280, "height": 900})
        r = tctx.request.post(f"{B}/t/login", form={"password": PW, "next": "/t"})
        assert r.ok, r.status
        cid = int(re.search(r"course/(\d+)/open", tctx.request.get(f"{B}/t").text()).group(1))

        close_all_live(tctx)  # 被 e2e_friction 呼叫時，前面的測試可能還留著進行中的場次

        # 名冊姓名（夾具名冊：蘇虛一、傅構二、林擬三、涂演四、測試N）
        def roster_name(n):
            return {1: "蘇虛一", 2: "傅構二", 3: "林擬三", 4: "涂演四"}.get(n, f"測試{n}")

        # 已經設過 PIN 之後，同一頁打舊 PIN 要成功（新舊位數三種組合）
        # 欄位：(學號, 號碼, 舊 PIN, 新 PIN, 標籤)；6→4 ＝ 新 PIN 六碼、舊 PIN 四碼
        combos = [("9994A030", 30, "1111", "7777", "4→4"),
                  ("9994A031", 31, "2222", "888888", "6→4"),
                  ("9994A032", 32, "333333", "9999", "4→6")]
        for stu, n, old, new, label in combos:
            bind_first(webkit, device, tctx, cid, stu, roster_name(n), old)
        sid = open_session(tctx, cid)
        for stu, n, old, new, label in combos:
            ctx = webkit.new_context(**device)
            pg = ctx.new_page()
            enter(pg, qr_code(tctx, sid))
            type_into(pg, "#student_id", stu)
            type_into(pg, "#name", roster_name(n))
            type_into(pg, "#pin1", new); type_into(pg, "#pin2", new)
            pg.click("#go")
            wait_err(pg, "已經設過 PIN")
            check(pg.locator("#pin1").input_value() == "", f"[{label}]切成只輸 PIN 後 #pin1 已清空，實得 {pg.locator('#pin1').input_value()!r}")
            check(pg.locator("#pin2").input_value() == "", f"[{label}]切成只輸 PIN 後 #pin2 已清空")
            type_into(pg, "#pin1", old)
            pg.click("#go")
            try:
                pg.wait_for_selector("#receipt", state="visible", timeout=6000)
                got = True
            except Exception:
                got = False
            check(got, f"[{label}]同一頁 tap #pin1 後打舊 PIN（{len(old)} 碼，新 PIN {len(new)} 碼）拿到收據，畫面訊息 {err_text(pg)!r}")
            ctx.close()

        # PIN 類拒絕之後清空 PIN 欄、學號姓名不清
        stu, n, old = "9994A033", 33, "4444"
        bind_first(webkit, device, tctx, cid, stu, roster_name(n), old)
        sid = open_session(tctx, cid)
        ctx = webkit.new_context(**device); pg = ctx.new_page()
        enter(pg, qr_code(tctx, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", roster_name(n))
        type_into(pg, "#pin1", "5555"); type_into(pg, "#pin2", "5555"); pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        type_into(pg, "#pin1", "6666"); pg.click("#go")
        wait_err(pg, "PIN 錯誤")
        check(pg.locator("#pin1").input_value() == "", f"只輸 PIN 被拒（PIN 錯誤）後 #pin1 清空，實得 {pg.locator('#pin1').input_value()!r}")
        check(pg.locator("#student_id").input_value() == stu, "PIN 被拒後學號不清")
        type_into(pg, "#pin1", old); pg.click("#go")
        try:
            pg.wait_for_selector("#receipt", state="visible", timeout=6000); got = True
        except Exception:
            got = False
        check(got, f"PIN 被拒一次之後再打對舊 PIN 仍拿到收據，訊息 {err_text(pg)!r}")
        ctx.close()

        # 重設後切回完整表單：隱藏的 #pin2 也要是空的
        stu, n, old = "9994A034", 34, "1212"
        bind_first(webkit, device, tctx, cid, stu, roster_name(n), old)
        sid = open_session(tctx, cid)
        ctx = webkit.new_context(**device); pg = ctx.new_page()
        enter(pg, qr_code(tctx, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", roster_name(n))
        type_into(pg, "#pin1", "3434"); type_into(pg, "#pin2", "3434"); pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        html = tctx.request.get(f"{B}/t/course/{cid}/students").text()
        idx = html.index(f">{stu}<")
        pk = int(re.search(r"unbind\((\d+),", html[idx: html.index("</tr>", idx)]).group(1))
        tctx.request.post(f"{B}/api/t/student/{pk}/unbind", form={"reset_pin": "1"})
        type_into(pg, "#pin1", old); pg.click("#go")
        wait_err(pg, "姓名也填上")
        check(pg.locator("#nameField").is_visible(), "重設後切回完整表單（姓名欄出現）")
        check(pg.locator("#pin2").input_value() == "", f"切回完整表單時 #pin2 值是空字串，實得 {pg.locator('#pin2').input_value()!r}")
        check(pg.locator("#pin1").input_value() == "", "切回完整表單時 #pin1 也是空的")
        ctx.close()
        tctx.request.post(f"{B}/api/t/session/{sid}/close")

        # 只輸 PIN 路徑被鎖之後，按鈕停用、提示不再說輸對就會改用
        stu, n, old = "9994A035", 35, "5656"
        bind_first(webkit, device, tctx, cid, stu, roster_name(n), old)
        sid = open_session(tctx, cid)
        ctx = webkit.new_context(**device); pg = ctx.new_page()
        enter(pg, qr_code(tctx, sid))
        type_into(pg, "#student_id", stu); type_into(pg, "#name", roster_name(n))
        type_into(pg, "#pin1", "1357"); type_into(pg, "#pin2", "1357"); pg.click("#go")
        wait_err(pg, "已經設過 PIN")
        type_into(pg, "#pin1", "2468"); pg.click("#go")
        wait_err(pg, "PIN 錯誤")
        type_into(pg, "#pin1", "9753"); pg.click("#go")
        wait_err(pg, "錯太多次")
        pg.wait_for_function("document.getElementById('go').disabled===true", timeout=3000)
        check(pg.locator("#go").is_disabled(), "只輸 PIN 錯到鎖定後簽到鈕停用")
        check(pg.locator("#go").inner_text() == "這一場請找老師", f"按鈕文字是「這一場請找老師」，實得 {pg.locator('#go').inner_text()!r}")
        hint = pg.locator("#modeHint").inner_text()
        check("輸對就會改用" not in hint, f"#modeHint 不含「輸對就會改用」，實得 {hint!r}")
        check("重設" in hint and "重新掃 QR" in hint, f"#modeHint 換成鎖定說明（含重設、重新掃 QR），實得 {hint!r}")
        grant = pg.evaluate("GRANT")
        r = ctx.request.post(f"{B}/api/checkin", form={"grant": grant, "student_id": stu, "name": "", "pin": old, "pin2": "", "hints": "h"})
        check(r.status == 423, f"直接對 API 送正確 PIN 仍是 423（前端停用是對的），實得 {r.status}")
        ctx.close()
        tctx.request.post(f"{B}/api/t/session/{sid}/close")
        webkit.close()


main()
print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)

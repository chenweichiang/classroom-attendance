"""手機直立開投影頁時六位數與 QR 要完整在畫面內。

量測手法：iPhone 13（WebKit）與
Pixel 7（Chromium）各一次直立裝置描述，量六位數每個字的右緣、QR 左緣、scrollWidth；
另外在 1280×720（常見投影機解析度）量 QR 寬度，把實測值寫成這支測試的下限，版面調整後
不能變小。
"""
import os, re, sys
from playwright.sync_api import sync_playwright

B = os.environ.get("ATTEND_TEST_BASE", "http://127.0.0.1:64890")
FAIL = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL"), msg)
    if not cond:
        FAIL.append(msg)


JS = """()=>{const el=document.querySelector('#code');const rg=document.createRange();rg.selectNodeContents(el);
 const rs=[...rg.getClientRects()].map(b=>({l:Math.round(b.left),r:Math.round(b.right)}));
 const q=document.querySelector('#qr').getBoundingClientRect();
 return {innerWidth:innerWidth, codeText:el.textContent, glyphLines:rs, qrL:Math.round(q.left), qrR:Math.round(q.right), docScrollW:document.documentElement.scrollWidth}}"""

with sync_playwright() as p:
    chrome = p.chromium.launch()
    webkit = p.webkit.launch()
    ctx = chrome.new_context(viewport={"width": 1280, "height": 900})
    tp = ctx.new_page()
    tp.goto(f"{B}/t/login"); tp.fill("input[name=password]", "pw")
    tp.click("button:has-text('登入')"); tp.wait_for_url(f"{B}/t")
    # 這支獨立跑（見 e2e_teacher_friction_playwright.py 結尾），跑在它後面時前面的場次（例如
    # 那段刻意留著開場超過一分鐘不結束的情境）可能還沒關；先關掉所有還開著的場次，避免
    # /open 沿用到別的測試留下的場次。
    html = tp.content()
    for s in set(re.findall(r"/t/session/(\d+)", html)):
        ctx.request.post(f"{B}/api/t/session/{s}/close")
    r = ctx.request.post(f"{B}/t/course/1/open", form={"kind": "normal"})
    sid = int(re.search(r"/t/session/(\d+)", r.url).group(1))
    storage = ctx.storage_state()

    for name, br, dev in [("iPhone13_WebKit", webkit, p.devices["iPhone 13"]),
                          ("Pixel7_Chromium", chrome, p.devices["Pixel 7"])]:
        pc = br.new_context(**dev, storage_state=storage)
        pp = pc.new_page()
        pp.goto(f"{B}/t/projector/{sid}")
        pp.wait_for_function("document.querySelector('#qr svg')!==null")
        pp.wait_for_timeout(500)
        info = pp.evaluate(JS)
        cut = any(g["r"] > info["innerWidth"] for g in info["glyphLines"])
        check(not cut, f"裝置 {name} 六位數沒有字被切在可視區外，實得 innerWidth={info['innerWidth']} glyphLines={info['glyphLines']}")
        check(info["qrL"] >= 0, f"裝置 {name} QR 左緣在畫面內（≥0），實得 {info['qrL']}")
        check(info["docScrollW"] <= info["innerWidth"], f"裝置 {name} 沒有橫向捲軸，實得 scrollWidth={info['docScrollW']} innerWidth={info['innerWidth']}")
        pc.close()

    # 橫向投影幕（1280×720）版面不得變小：QR 實測寬度 635.390625px，以這個實測值當下限
    ctx720 = chrome.new_context(viewport={"width": 1280, "height": 720}, storage_state=storage)
    pg720 = ctx720.new_page()
    pg720.goto(f"{B}/t/projector/{sid}")
    pg720.wait_for_function("document.querySelector('#qr svg')!==null")
    qr_w = pg720.evaluate("document.getElementById('qr').getBoundingClientRect().width")
    check(qr_w >= 635, f"1280x720 投影幕 QR 寬度不小於基準 ~635px，實得 {qr_w}")
    ctx720.close()

    ctx.request.post(f"{B}/api/t/session/{sid}/close")
    chrome.close(); webkit.close()

print("\n==>", "全部通過" if not FAIL else f"{len(FAIL)} 項失敗：{FAIL}")
sys.exit(1 if FAIL else 0)

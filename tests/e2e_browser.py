"""浏览器端到端测试（真 Chrome）。测试专用依赖 playwright，skill 本身不依赖它。

  uv run --no-project --with playwright python tests/e2e_browser.py

覆盖：
1. 无服务（file://）：本地文件模式、刷新后批注仍在、两个标签页同时写不互相覆盖、CSP 拦住注入的内联事件、
   发现并导入旧版批注、导出、「交给 Agent」退回为复制；
2. 报告样式保真：:root 变量、外壳同名变量不渗入、body 布局写法不把幻灯片挤偏、rem 按报告根字号；
3. 有服务：落盘、刷新恢复、选中文字快捷删除 / 改为、快捷键与按键提示、输入框停靠底部、
   轮询不打断草稿、翻页草稿保留、「回到上次」提示、交给 Agent → wait 唤醒 → 处理中 → 已处理；
4. 断线暂存补传（同文不同标点两条都在、「交给 Agent」排在批注之后）；
5. 服务端写入失败（5xx）不丢、恢复后补传；孤立代理字符（半个 emoji）不卡住队列；
6. 服务卡死（SIGSTOP）：请求超时后转暂存，重启后补传；
7. 同一端口换成另一份审阅：切换后立刻写也不会写进别人的 notes.json；换回后补传；
8. 恶意网页写批注全部失败；导入带恶意 id / 内容的批注不执行脚本；旧版 Markdown 导入保留状态。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURE = ROOT / "fixtures" / "sample"
RESULTS: list[tuple[bool, str]] = []
STAGE = "document.getElementById('host').shadowRoot.getElementById('ae-stage')"


def ok(cond, label):
    RESULTS.append((bool(cond), label))
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build(out: Path, report: Path = FIXTURE / "report.html", script: Path | None = FIXTURE / "script.md"):
    cmd = [sys.executable, str(SCRIPTS / "build_viewer.py"), "--report", str(report), "--out", str(out)]
    if script:
        cmd += ["--script", str(script)]
    subprocess.run(cmd, check=True, capture_output=True)


def start_server(d: Path, port: int):
    p = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(d), "--port", str(port), "--no-open"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(80):
        info = d / ".annotation-server.json"
        if info.exists() and json.loads(info.read_text()).get("port") == port:
            return p
        time.sleep(0.1)
    p.terminate()
    raise RuntimeError("server did not start on the expected port: " + p.communicate()[1])


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()


def disk_notes(d: Path):
    f = d / "notes.json"
    return json.loads(f.read_text())["notes"] if f.exists() else []


def add_note(page, text, pin=None):
    if pin:
        page.click('[data-act="pin"]')
        box = page.locator("#slide").bounding_box()
        page.mouse.click(box["x"] + box["width"] * pin[0], box["y"] + box["height"] * pin[1])
    page.fill("#ntext", text)
    page.click('[data-act="add"]')
    page.wait_for_timeout(400)


def go_page(page, n):
    page.select_option("#jump", str(n - 1))
    page.wait_for_timeout(150)


def drag_select(page, locator):
    """真实的鼠标拖选（报告在 Shadow DOM 里，locator 能穿透）。"""
    b = locator.bounding_box()
    y = b["y"] + min(12, b["height"] / 2)   # 第一行文字上，避免落在两行之间的空隙
    page.mouse.move(b["x"] + 2, y)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] - 2, y, steps=8)
    page.mouse.up()
    page.wait_for_timeout(200)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ae-e2e-"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)

        # ---------- 1. file:// 退回模式 ----------
        fdir = tmp / "file_mode"
        build(fdir)
        ctx = browser.new_context()
        page = ctx.new_page()
        url = (fdir / "index.html").as_uri()
        page.goto(url)
        ok("本地文件模式" in page.inner_text("#mode"), "file:// 打开显示「本地文件模式」")
        go_page(page, 3)
        add_note(page, "file 模式批注", pin=(0.5, 0.5))
        page.reload()
        page.wait_for_timeout(300)
        ok("file 模式批注" in page.inner_text("#script"), "file:// 刷新后批注仍在（localStorage）")
        ok("上次批注到第 3 页" in page.inner_text("#resume"), "file:// 打开时提示上次批注到哪一页")
        page2 = ctx.new_page()
        page2.goto(url)
        page2.wait_for_timeout(300)
        go_page(page2, 4)
        add_note(page2, "第二个标签页的批注")
        go_page(page, 5)
        add_note(page, "第一个标签页又加一条")
        texts = [n["text"] for n in page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")]
        ok({"file 模式批注", "第二个标签页的批注", "第一个标签页又加一条"} <= set(texts), "两个 file:// 标签页各自添加，批注都在")
        page2.close()
        page.evaluate(f"() => {STAGE}.insertAdjacentHTML('beforeend', '<img src=\"x:y\" onerror=\"window.__p_file=1\">')")
        page.wait_for_timeout(300)
        ok(page.evaluate("window.__p_file") is None, "file:// 模式下 CSP 拦住注入的内联事件")
        legacy = [{"id": "old1", "page": 4, "title": "收入结构", "target": "报告", "text": "旧版批注甲",
                   "quote": "", "quoteFrom": "", "pin": {"x": 20, "y": 30}, "at": "2026-10-27T01:00:00Z"},
                  {"id": "old2", "page": 5, "title": "成本", "target": "讲稿", "text": "旧版批注乙",
                   "quote": "", "quoteFrom": "", "pin": None, "at": "2026-10-27T01:01:00Z"}]
        page.evaluate("v => localStorage.setItem('v7viewer.notes.viewer_r2', JSON.stringify(v))", legacy)
        page.reload()
        page.wait_for_timeout(300)
        ok(page.is_visible("#legacyBtn") and "2" in page.inner_text("#legacyBtn"), "发现旧版对照页的 2 条批注，显示导入按钮")
        page.click("#legacyBtn")
        page.click("[data-found='0']")
        page.wait_for_timeout(300)
        n = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')).length")
        ok(n == 5, f"旧批注导入后共 5 条（实际 {n}）")
        ok(not page.is_visible("#legacyBtn"), "导入后不再提示旧批注")
        page.keyboard.press("Escape")
        go_page(page, 3)
        page.evaluate("document.querySelector('[data-note] [data-act=\"resolve\"]')?.click()")
        page.wait_for_timeout(200)
        page.click("#handBtn")
        page.wait_for_timeout(300)
        ok("本地文件模式连不到 Agent" in page.inner_text("#toast"), "file:// 模式点「交给 Agent」退回为复制全部并提示")
        md = page.evaluate("exportText()")
        ok("【已处理】file 模式批注" in md and "## 第 4 页" in md, "导出 Markdown 含页码与已处理标记")
        (tmp / "export.md").write_text(md, encoding="utf-8")
        ctx.close()

        # ---------- 2. 报告样式保真 ----------
        css_dir = tmp / "css_src"
        css_dir.mkdir()
        (css_dir / "r.html").write_text(
            "<!doctype html><title>样式</title><style>html{font-size:62.5%} :root{--brand:rgb(255, 0, 0)}"
            " body{font-size:30px;display:flex;flex-direction:column;align-items:center;gap:40px;padding:40px;min-height:100vh}"
            " h2{color:var(--brand)} .acc{color:var(--accent, rgb(0, 128, 0))} .rem{font-size:2.4rem}"
            " .page{width:1280px;height:720px;box-sizing:border-box}</style>"
            '<section class="page" data-page="1" data-key="a"><h2>标题</h2><p class="acc">正文</p><p class="rem">rem</p></section>',
            encoding="utf-8")
        build(tmp / "css_review", css_dir / "r.html", None)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto((tmp / "css_review" / "index.html").as_uri())
        got = page.evaluate(f"""() => {{ const st = {STAGE}, r = st.getRootNode();
            const sec = r.querySelector('section.page'), a = sec.getBoundingClientRect(), b = st.getBoundingClientRect();
            return [getComputedStyle(r.querySelector('h2')).color, getComputedStyle(r.querySelector('.acc')).color,
                    getComputedStyle(r.querySelector('.acc')).fontSize, getComputedStyle(r.querySelector('.rem')).fontSize,
                    Math.round(a.left - b.left), Math.round(a.top - b.top)]; }}""")
        ok(got[0] == "rgb(255, 0, 0)", f"报告 :root 变量在 Shadow DOM 里生效（{got[0]}）")
        ok(got[1] == "rgb(0, 128, 0)", f"外壳的同名 CSS 变量不渗入报告（{got[1]}）")
        ok(got[2] == "30px", f"报告 body 样式（字号）生效（{got[2]}）")
        ok(got[3] == "24px", f"rem 按报告的根字号计算（{got[3]}）")
        ok(got[4] == 0 and got[5] == 0, f"body 的 flex / padding 写法不把幻灯片挤偏（偏移 {got[4]},{got[5]}）")
        ctx.close()

        # ---------- 3. 服务模式 ----------
        sdir = tmp / "server_mode"
        build(sdir)
        port = free_port()
        srv = start_server(sdir, port)
        ctx = browser.new_context()
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}/"
        page.goto(base)
        page.wait_for_timeout(700)
        ok("已连接本地服务" in page.inner_text("#mode"), "经服务打开显示「已连接本地服务」")
        ok(page.locator("header kbd").count() >= 5 and "P" in page.inner_text("#pinBtn"), "按钮上显示快捷键")
        go_page(page, 3)
        page.evaluate("""() => { const p = document.querySelector('.talk p'); const r = document.createRange();
            r.selectNodeContents(p); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup')); }""")
        page.keyboard.press("Escape")
        add_note(page, "服务模式批注：讲法第一句太长", pin=(0.3, 0.4))
        notes = disk_notes(sdir)
        ok(len(notes) == 1 and notes[0]["page"] == 3 and notes[0].get("key") == "sum" and notes[0]["pin"] and notes[0]["quoteFrom"] == "讲稿",
           "批注即时写入 notes.json（页码、页面键、标点、讲稿引用）")
        ok(page.evaluate("localStorage.getItem('ae.' + RID + '.notes')") is None, "服务模式不在浏览器里留整份批注副本")
        page.click('[data-act="edit"]')
        page.fill("#etext", "服务模式批注（已编辑）")
        page.click('[data-act="save"]')
        page.wait_for_timeout(500)
        ok(disk_notes(sdir)[0]["text"] == "服务模式批注（已编辑）", "页面编辑批注后落盘")
        # 标点交互：拖动、悬停高亮、点击弹出操作
        page.on("dialog", lambda dlg: dlg.accept())
        nid0, old_pin = disk_notes(sdir)[0]["id"], disk_notes(sdir)[0]["pin"]
        pin = page.locator(f'#host .ae-pin[data-id="{nid0}"]')
        bb = pin.bounding_box()
        cx, cy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 80, cy + 40, steps=6)
        page.mouse.up()
        page.wait_for_timeout(700)
        new_pin = [n for n in disk_notes(sdir) if n["id"] == nid0][0]["pin"]
        ok(new_pin and new_pin["x"] > old_pin["x"] and new_pin["y"] > old_pin["y"], f"拖动标点：新位置落盘（{old_pin} → {new_pin}）")
        page.hover(f'#nlist [data-note="{nid0}"] .body')
        ok("hl" in (pin.get_attribute("class") or ""), "鼠标移到右侧批注上，对应标点高亮")
        bb = pin.bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.wait_for_timeout(200)
        ok(page.is_visible("#pinpop") and "服务模式批注" in page.inner_text("#pinpop"), "点击标点弹出这条批注及操作")
        page.keyboard.press("Escape")
        add_note(page, "临时标点批注", pin=(0.6, 0.6))
        tid = [n for n in disk_notes(sdir) if n["text"] == "临时标点批注"][0]["id"]
        tpin = page.locator(f'#host .ae-pin[data-id="{tid}"]')
        bb = tpin.bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.click('#pinpop [data-pp="unpin"]')
        page.wait_for_timeout(600)
        tn = [n for n in disk_notes(sdir) if n["id"] == tid][0]
        ok(tn["pin"] is None and not tn.get("region"), "在标注弹窗里「去掉标注」")
        page.click(f'#nlist [data-note="{tid}"] [data-act="del"]')
        page.wait_for_timeout(600)
        ok(not any(n["id"] == tid for n in disk_notes(sdir)), "删除批注（确认后）")
        # 技能：代理写入技能目录 → 输入栏有候选 → 批注带上技能
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(sdir), "--set", "humanizer-zh=中文润色", "dataviz=图表"],
                       check=True, capture_output=True)
        page.wait_for_timeout(4600)
        page.click("#nskills")
        ok(page.locator('#skillList option[value="humanizer-zh"]').count() == 1, "技能目录出现在「技能」输入栏的候选里")
        go_page(page, 2)   # 放在第 2 页，不影响第 3 页「全部已处理」的检查
        page.fill("#ntext", "这段口气太硬，润色一下")
        page.fill("#nskills", "humanizer-zh, dataviz")
        page.click('[data-act="add"]')
        page.wait_for_timeout(600)
        sk = [n for n in disk_notes(sdir) if n["text"] == "这段口气太硬，润色一下"]
        ok(sk and sk[0].get("skills") == ["humanizer-zh", "dataviz"] and "⚙ humanizer-zh" in page.inner_text("#nlist"),
           "批注带上指定技能并显示")
        shown = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(sdir)], capture_output=True, text=True).stdout
        ok("技能：humanizer-zh、dataviz（处理这条前先调用这些技能）" in shown, "notes.py show 列出这条批注要调用的技能")
        # 快捷修改：选中报告文字 → D 删除；选中讲稿文字 → R 改为
        go_page(page, 4)
        drag_select(page, page.locator("#host section.page p").first)
        ok(page.is_visible("#selbar"), "选中报告文字后弹出「删除 / 改为 / 批注」工具条")
        ok(page.is_visible("#pairRow") and "讲稿这页也有相近的句子" in page.inner_text("#pairRow") and page.is_checked("#pairOn"),
           "讲稿同一页有相近的句子：提示「同时改」并默认勾选")
        page.keyboard.press("d")
        page.wait_for_timeout(400)
        ok(not any((n.get("edit") or {}).get("op") == "delete" for n in disk_notes(sdir)) and "确认删除" in page.inner_text("#selbar"),
           "按一次 D 只进入「待确认」，不会误记删除（防止打拼音时误触）")
        page.keyboard.press("d")
        page.wait_for_timeout(600)
        dels = [n for n in disk_notes(sdir) if (n.get("edit") or {}).get("op") == "delete"]
        ok(dels and dels[0]["target"] == "报告和讲稿" and "毛利率" in dels[0]["edit"]["from"]
           and (dels[0].get("pair") or {}).get("side") == "讲稿" and "平台抽成" in dels[0]["pair"]["text"],
           "再按 D：记下「删除这段」，对象自动为报告和讲稿，并带上讲稿的对应句")
        ok("↔ 讲稿" in page.inner_text("#nlist"), "批注卡片上显示另一侧也要改")
        drag_select(page, page.locator("#pageScript .talk p").nth(1))
        ok(page.is_checked("#pairOn"), "选中讲稿文字：报告里有相近的句子，同样提示同时改")
        page.keyboard.press("b")
        ok(not page.is_checked("#pairOn") and page.input_value("#ntarget") == "讲稿", "按 B 取消同时改，对象回到讲稿")
        page.keyboard.press("r")
        page.wait_for_timeout(200)
        ok(page.is_visible("#repbox") and "平台抽成" in page.input_value("#repText"), "按 R：弹出改写框，预填选中的原文")
        page.fill("#repText", "平台抽成上调，外卖毛利率降了 2 个百分点。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        reps = [n for n in disk_notes(sdir) if (n.get("edit") or {}).get("op") == "replace"]
        ok(reps and reps[0]["target"] == "讲稿" and reps[0]["edit"]["to"].startswith("平台抽成上调，") and not reps[0].get("pair"),
           "改写框按回车：记下「改为」（只改讲稿）")
        ok(page.locator("#nlist del").count() >= 2 and page.locator("#nlist ins").count() >= 1, "快捷修改在批注区显示为删除线 → 新文字")
        shown = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(sdir)], capture_output=True, text=True).stdout
        ok("·删除】删除「" in shown and "·改写】「" in shown and "」改为「平台抽成上调，" in shown, "notes.py show 把快捷修改写成明确指令")
        ok("↔ 另一侧（讲稿）也要改" in shown and "↔ 提示：报告第 4 页有相近的句子" in shown,
           "notes.py show 列出同时改的对应句，并提醒没勾选同改的那条另一侧可能也要改")
        # 风格优化：框选区域（第 4 页的表格）、整页
        page.keyboard.press("Escape")
        page.keyboard.press("s")
        ok(page.is_visible("#stylemenu"), "按 S 弹出风格优化范围菜单")
        page.keyboard.press("2")
        tb = page.locator("#host section.page table").bounding_box()
        page.mouse.move(tb["x"] - 4, tb["y"] - 4)
        page.mouse.down()
        page.mouse.move(tb["x"] + tb["width"] + 4, tb["y"] + tb["height"] + 4, steps=8)
        page.mouse.up()
        page.wait_for_timeout(200)
        ok("风格优化 · 框选处" in page.inner_text("#styleChip"), "S → 2 后拖动框选，输入框标明「风格优化 · 框选处」")
        page.fill("#ntext", "表格太挤，加大行距并对齐")
        page.click('[data-act="add"]')
        page.wait_for_timeout(600)
        rg = [n for n in disk_notes(sdir) if n["text"] == "表格太挤，加大行距并对齐"]
        ok(rg and rg[0]["style"] == {"scope": "region"} and rg[0]["region"]["w"] > 5 and "table" in (rg[0].get("anchor") or ""),
           f"框选区域的风格优化落盘，附位置线索（{(rg[0].get('anchor') or '')[:40] if rg else ''}）")
        reg = page.locator(f'#host .ae-region[data-id="{rg[0]["id"]}"]')
        bb = reg.bounding_box()
        page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.mouse.down()
        page.mouse.move(bb["x"] + bb["width"] / 2 + 40, bb["y"] + bb["height"] / 2 + 30, steps=6)
        page.mouse.up()
        page.wait_for_timeout(600)
        moved_n = [n for n in disk_notes(sdir) if n["id"] == rg[0]["id"]][0]
        moved = moved_n["region"]
        ok(moved["x"] > rg[0]["region"]["x"] and moved["w"] == rg[0]["region"]["w"] and moved_n["style"] == {"scope": "region"},
           "拖动区域：新位置落盘，大小不变")
        bb = reg.bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        ok(page.is_visible("#pinpop") and "表格太挤" in page.inner_text("#pinpop"), "点击区域弹出这条批注")
        page.keyboard.press("Escape")
        page.keyboard.press("s")
        page.keyboard.press("1")
        page.keyboard.press("Escape")   # 离开输入框
        page.click('[data-act="add"]')
        page.wait_for_timeout(600)
        whole = [n for n in disk_notes(sdir) if (n.get("style") or {}).get("scope") == "page"]
        ok(whole and whole[0]["text"] == "风格优化：整页" and whole[0]["target"] == "报告" and "整页" in whole[0].get("anchor", ""),
           "整页风格优化：不写方向也能添加")
        shown = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(sdir)], capture_output=True, text=True).stdout
        ok("·风格优化·框选处】" in shown and "·框选 " in shown and "位置线索：" in shown and "·风格优化·整页】" in shown,
           "notes.py show 写明风格优化的范围、框的坐标与位置线索")
        # 通用标注：P 之后拖动 = 框选（普通批注也能框）；框拖右下角调大小；已有批注可重新标注（单击 = 点）、去掉标注
        page.keyboard.press("Escape")
        page.keyboard.press("p")
        sec = page.locator("#host section.page").bounding_box()
        page.mouse.move(sec["x"] + sec["width"] * .1, sec["y"] + sec["height"] * .15)
        page.mouse.down()
        page.mouse.move(sec["x"] + sec["width"] * .35, sec["y"] + sec["height"] * .35, steps=8)
        page.mouse.up()
        page.wait_for_timeout(200)
        ok(page.locator("#host .ae-region.pending").count() == 1 and page.locator("#host .ae-pin.pending").count() == 0
           and "on" not in (page.get_attribute("#pinBtn", "class") or ""), "标注模式下拖动 = 框选一块区域（不是放点），放好后退出标注模式")
        ok(not page.is_visible("#styleChip"), "普通框选不会变成风格优化")
        page.fill("#ntext", "这块口径要和讲稿一致")
        page.click('[data-act="add"]')
        page.wait_for_timeout(600)
        bx = [n for n in disk_notes(sdir) if n["text"] == "这块口径要和讲稿一致"]
        ok(bx and bx[0].get("region") and abs(bx[0]["region"]["w"] - 25) < 2 and not bx[0].get("style") and not bx[0].get("pin")
           and bx[0]["target"] == "报告" and bx[0].get("anchor"), f"普通批注框选：区域与位置线索落盘（{bx[0].get('region') if bx else ''}）")
        bid, r1 = bx[0]["id"], bx[0]["region"]
        ok("框选 " in page.inner_text(f'#nlist [data-note="{bid}"] .meta'), "批注卡片标明「框选 n」")
        rz = page.locator(f'#host .ae-region[data-id="{bid}"] .rz').bounding_box()
        page.mouse.move(rz["x"] + rz["width"] / 2, rz["y"] + rz["height"] / 2)
        page.mouse.down()
        page.mouse.move(rz["x"] + 70, rz["y"] + 45, steps=6)
        page.mouse.up()
        page.wait_for_timeout(600)
        r2 = [n for n in disk_notes(sdir) if n["id"] == bid][0]["region"]
        ok(r2["w"] > r1["w"] and r2["h"] > r1["h"] and r2["x"] == r1["x"] and r2["y"] == r1["y"], f"拖框的右下角调大小：落盘，位置不变（{r1} → {r2}）")
        page.click(f'#nlist [data-note="{bid}"] [data-act="edit"]')
        page.click(f'#nlist [data-note="{bid}"] [data-act="remark"]')
        ok("on" in (page.get_attribute("#pinBtn", "class") or ""), "编辑里点「重新标注」进入标注模式")
        page.mouse.click(sec["x"] + sec["width"] * .8, sec["y"] + sec["height"] * .8)
        page.wait_for_timeout(600)
        rn = [n for n in disk_notes(sdir) if n["id"] == bid][0]
        ok(rn.get("pin") and not rn.get("region") and page.locator(f'#host .ae-pin[data-id="{bid}"]').count() == 1,
           "重新标注时单击 = 放点，原来的框被替换")
        page.click(f'#nlist [data-note="{bid}"] [data-act="unpin"]')
        page.wait_for_timeout(600)
        rn = [n for n in disk_notes(sdir) if n["id"] == bid][0]
        ok(not rn.get("pin") and not rn.get("region") and page.locator(f'#host [data-id="{bid}"]').count() == 0, "编辑里「去掉标注」")
        page.click(f'#nlist [data-note="{bid}"] [data-act="remark"]')
        page.click(f'#nlist [data-note="{bid}"] [data-act="cancel"]')
        page.mouse.click(sec["x"] + sec["width"] * .5, sec["y"] + sec["height"] * .9)
        page.wait_for_timeout(500)
        rn = [n for n in disk_notes(sdir) if n["id"] == bid][0]
        ok("on" not in (page.get_attribute("#pinBtn", "class") or "") and not rn.get("pin"),
           "点了「重新标注」又取消：退出标注模式，之后随手点报告不会改这条批注")
        # 从一条的编辑直接切到另一条：草稿各归各的
        cur = {n["id"]: n for n in disk_notes(sdir)}
        cards = page.locator("#nlist [data-note]")
        ids = [cards.nth(k).get_attribute("data-note") for k in range(cards.count())]
        editable = [x for x in ids if (cur[x].get("edit") or {}).get("op") != "delete"]
        A, B = editable[0], editable[1]
        page.click(f'#nlist [data-note="{A}"] [data-act="edit"]')
        page.fill("#etext", "AAA 改到一半")
        page.click(f'#nlist [data-note="{B}"] [data-act="edit"]')
        ok(page.input_value("#etext") != "AAA 改到一半", "从批注 A 的编辑直接点批注 B 的「编辑」，B 不会带上 A 的草稿")
        page.click(f'#nlist [data-note="{A}"] [data-act="edit"]')
        ok(page.input_value("#etext") == "AAA 改到一半", "切回 A，A 的草稿还在")
        page.click(f'#nlist [data-note="{A}"] [data-act="cancel"]')
        # 原页面已不存在的批注：提示条有数，在「批注」里挪到正确的页
        lost_id = B
        subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0,sys.argv[1]);import notes_store as s;from pathlib import Path;"
                        "s.update(Path(sys.argv[2]),sys.argv[3],{'page':99,'orphan':True})", str(SCRIPTS), str(sdir), lost_id], check=True)
        page.wait_for_timeout(4800)
        ok("条批注的原页面在新版报告里找不到" in page.inner_text("#resume"), "原页面找不到的批注：提示条给出数量")
        page.keyboard.press("Escape")
        page.keyboard.press("n")
        page.select_option(f'#orphanBox select[data-mv="{lost_id}"]', "2")
        page.click(f'#orphanBox [data-mvgo="{lost_id}"]')
        page.wait_for_timeout(700)
        moved_note = [n for n in disk_notes(sdir) if n["id"] == lost_id][0]
        ok(moved_note["page"] == 2 and not moved_note.get("orphan"), "在「批注」里把它挪到第 2 页")
        page.keyboard.press("Escape")
        # 快捷键
        page.keyboard.press("p")
        ok("on" in (page.get_attribute("#pinBtn", "class") or ""), "按 P 进入标注模式")
        page.keyboard.press("Escape")
        page.keyboard.press("c")
        ok(page.evaluate("document.activeElement.id") == "ntext", "按 C 聚焦输入框")
        page.keyboard.press("Escape")
        # 输入框停靠：讲稿很长时不用往下翻
        page.evaluate("() => { const t = document.querySelector('#pageScript'); for (let k = 0; k < 60; k++) t.insertAdjacentHTML('beforeend', '<p>很长的讲稿段落……</p>'); }")
        page.evaluate("document.getElementById('script').scrollTop = 0")
        box = page.evaluate("""() => { const f = document.getElementById('nform').getBoundingClientRect(), s = document.getElementById('script').getBoundingClientRect();
            return [f.top >= s.top, f.bottom <= s.bottom + 1]; }""")
        ok(all(box), "讲稿很长时，输入框停靠在面板底部，不用滚动就能写")
        go_page(page, 3)
        # 正在写草稿时，Claude 标已处理触发轮询刷新：草稿与焦点都要保住
        page.click("#ntext")
        page.keyboard.type("正在写的草稿")
        nid = [n for n in disk_notes(sdir) if n["text"].startswith("服务模式批注")][0]["id"]
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(sdir), nid, "--note", "已拆成两句",
                        "--by", "claude"], check=True, capture_output=True)
        page.wait_for_timeout(5000)
        txt = page.inner_text("#nlist")
        ok("已处理 · Claude" in txt and "处理说明：已拆成两句" in txt, "Claude 用 CLI 标已处理后页面自动刷新显示")
        ok(page.input_value("#ntext") == "正在写的草稿" and page.evaluate("document.activeElement.id") == "ntext",
           "轮询刷新不打断正在写的草稿（内容与焦点都在）")
        page.keyboard.type("，继续")
        ok(page.input_value("#ntext") == "正在写的草稿，继续", "刷新后继续输入不丢字")
        page.click("#next")
        page.wait_for_timeout(150)
        ok(page.input_value("#ntext") == "", "翻到下一页，输入框是该页自己的（空）")
        page.click("#prev")
        page.wait_for_timeout(150)
        ok(page.input_value("#ntext") == "正在写的草稿，继续", "翻回来，草稿还在")
        ok("全部已处理" in page.inner_text("#nlist") or "✓" in page.inner_text("#jump"), "已处理完的页有 ✓ 标记")
        page.fill("#ntext", "")
        # 交给 Agent：代理在后台等待，用户点按钮后等待命令退出（Claude Code 里即唤醒会话）
        waiter = subprocess.Popen([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(sdir), "--timeout", "30",
                                   "--interval", "0.3", "--by", "claude"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.8)
        page.keyboard.press("Escape")   # 先离开输入框（在输入框里按 A 是打字）
        page.keyboard.press("a")
        page.fill("#handMsg", "先改第 3 页")
        page.click("#doHand")
        page.wait_for_timeout(600)
        hfile = json.loads((sdir / "handoff.json").read_text())
        ok(hfile["items"][-1]["message"] == "先改第 3 页", "按 A 打开「交给 Agent」，提交与附言记进审阅目录")
        try:
            wout, _ = waiter.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            waiter.kill()
            wout = ""
        ok(waiter.returncode == 0 and "第 1 批" in wout and "先改第 3 页" in wout and "指定了技能：dataviz、humanizer-zh" in wout,
           "后台等待的 notes.py wait 随即退出，输出提交、附言与要用的技能")
        page.wait_for_timeout(4800)
        ok("Agent 处理中" in page.inner_text("#handState"), "代理接收后页面显示「Agent 处理中」")
        fixed = [n for n in disk_notes(sdir) if n["status"] == "open"][0]
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(sdir), fixed["id"], "--note", "标题缩短了", "--by", "claude"],
                       check=True, capture_output=True)
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(sdir), "--summary", "第 3 页已改"],
                       check=True, capture_output=True)
        page.wait_for_timeout(4800)
        hs = page.inner_text("#handState")
        ok("Agent 已处理 · 第 1 批（解决 1 条）" in hs and "第 3 页已改" in (page.get_attribute("#handState", "title") or ""),
           f"代理收尾后页面显示「Agent 已处理 · 第 1 批（解决 1 条）」与处理摘要（{hs}）")
        page.click("#handState")
        page.wait_for_timeout(200)
        bl = page.inner_text("#batchList") if page.is_visible("#batches") else ""
        ok("第 1 批" in bl and "解决 1 条" in bl and "处理摘要：第 3 页已改" in bl and "处理说明：标题缩短了" in bl and "附言：先改第 3 页" in bl,
           "点 Agent 状态打开处理记录：批次、解决几条、处理摘要、每条的处理说明")
        page.click(f'#batchList [data-goto="{fixed["id"]}"]')
        page.wait_for_timeout(300)
        ok(not page.is_visible("#batches") and page.input_value("#jump") == str(fixed["page"] - 1),
           "在处理记录里点一条批注：跳到它所在的页")
        page.keyboard.press("j")
        ok(page.is_visible("#batches"), "按 J 打开处理记录")
        page.keyboard.press("Escape")
        go_page(page, 1)
        page.reload()
        page.wait_for_timeout(900)
        rs = page.inner_text("#resume")
        ok("上次批注到第" in rs and "Agent 已处理完第 1 批（解决 1 条）" in rs, f"重新打开时提示上次批注到哪页、Agent 是否处理完（{rs[:60]}…）")
        page.keyboard.press("l")
        page.wait_for_timeout(200)
        from datetime import datetime
        def user_time(n):
            return max(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() for v in (n["at"], n.get("updatedAt") or n["at"]))
        last_page = max(disk_notes(sdir), key=user_time)["page"]
        ok(page.inner_text("#stat").startswith(f"第 {last_page} /"), f"按 L 跳到上次批注的页（第 {last_page} 页）")
        time.sleep(1.1)
        subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), "--rebuild", str(sdir)], check=True, capture_output=True)
        page.wait_for_timeout(5000)
        ok(page.is_visible("#reloadBtn"), "Claude 重建对照页后页面提示「点此刷新」")

        # ---------- 4. 断线暂存与补传 ----------
        stop(srv)
        page.wait_for_timeout(4500)
        ok("服务未连接" in page.inner_text("#mode"), "服务停止后页面提示未连接")
        add_note(page, "删", pin=(0.2, 0.2))
        add_note(page, "删", pin=(0.7, 0.7))
        ok("待补传 2" in page.inner_text("#mode"), "断线时两条批注暂存浏览器并显示待补传")
        page.click("#handBtn")
        page.click("#doHand")
        page.wait_for_timeout(300)
        ok("待补传" in page.inner_text("#handState"), "断线时点「交给 Agent」也先暂存")
        srv = start_server(sdir, port)
        page.wait_for_timeout(5500)
        dels = [n for n in disk_notes(sdir) if n["text"] == "删"]
        ok(len(dels) == 2 and "已连接" in page.inner_text("#mode"), f"服务恢复后补传，同文不同标点两条都在（{len(dels)} 条）")
        last = json.loads((sdir / "handoff.json").read_text())["items"][-1]
        ok(last["seq"] == 2 and last["status"] == "pending" and {n["id"] for n in dels} <= set(last["openIds"]),
           "暂存的「交给 Agent」在批注补传之后才提交（提交快照里包含这两条）")
        # 处理记录：用户展开的批次在状态变化重绘后仍展开
        page.keyboard.press("Escape")
        page.keyboard.press("j")
        page.wait_for_timeout(500)
        page.click('#batchList details[data-seq="1"] summary')
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(sdir)], check=True, capture_output=True)
        page.wait_for_timeout(4800)
        ok("处理中" in page.inner_text("#batchList") and page.evaluate(
            "document.querySelector('#batchList details[data-seq=\"1\"]').open"), f"处理记录随状态刷新，用户展开的批次保持展开（{page.inner_text('#batchList')[:120]!r}）")
        page.keyboard.press("Escape")
        # 背后是旧版服务（没有 marks 功能）：提示并拦住框选，不让改动被静默丢掉
        old = ctx.new_page()
        def strip_features(route):
            r = route.fetch()
            body = r.json()
            body.pop("features", None)
            route.fulfill(response=r, json=body)
        old.route("**/api/notes", strip_features)
        old.goto(f"http://127.0.0.1:{port}/")
        old.wait_for_timeout(1200)
        ok("旧版服务" in old.inner_text("#mode"), "背后是旧版服务：状态处显示「旧版服务」")
        old.keyboard.press("p")
        ob = old.locator("#host section.page").bounding_box()
        old.mouse.move(ob["x"] + 40, ob["y"] + 40)
        old.mouse.down()
        old.mouse.move(ob["x"] + 240, ob["y"] + 160, steps=6)
        old.mouse.up()
        old.wait_for_timeout(200)
        ok(old.locator("#host .ae-region.pending").count() == 0 and "旧版本" in old.inner_text("#toast"),
           "旧版服务时拖框被拦下并提示停掉旧服务")
        old.close()

        # ---------- 5. 写入失败与坏字符 ----------
        os.chmod(sdir, 0o500)
        try:
            add_note(page, "写盘失败时的批注")
            ok("写盘失败时的批注" in page.inner_text("#nlist") and "待补传 1" in page.inner_text("#mode"),
               "服务端写入失败（5xx）时批注仍在页面上并进入待补传")
        finally:
            os.chmod(sdir, 0o700)
        page.wait_for_timeout(5500)
        ok(any(n["text"] == "写盘失败时的批注" for n in disk_notes(sdir)) and "待补传" not in page.inner_text("#mode"),
           "目录恢复可写后自动补传")
        page.evaluate("() => setQuote('甲'.repeat(299) + '📈尾巴', '讲稿')")
        add_note(page, "引用里有被截断的 emoji")
        add_note(page, "后面一条普通批注")
        page.wait_for_timeout(600)
        texts = [n["text"] for n in disk_notes(sdir)]
        ok("引用里有被截断的 emoji" in texts and "后面一条普通批注" in texts and "待补传" not in page.inner_text("#mode"),
           "引用含 emoji 被截断时照常保存，不卡住后面的批注")

        # ---------- 6. 服务卡死 ----------
        os.kill(srv.pid, signal.SIGSTOP)
        try:
            add_note(page, "服务卡住时写的批注")
            page.wait_for_timeout(9500)
            ok("待补传" in page.inner_text("#mode"), "服务卡住时请求超时，批注转入待补传")
        finally:
            os.kill(srv.pid, signal.SIGKILL)
            srv.communicate()
        (sdir / ".annotation-server.json").unlink(missing_ok=True)
        srv = start_server(sdir, port)
        page.wait_for_timeout(5500)
        ok(any(n["text"] == "服务卡住时写的批注" for n in disk_notes(sdir)), "重启服务后补传到 notes.json")

        # ---------- 7. 同一端口换成另一份审阅 ----------
        other = tmp / "other_review"
        build(other)
        stop(srv)
        srv_b = start_server(other, port)
        add_note(page, "切换后立刻写的 A 稿意见")   # 页面还以为连着 A
        page.wait_for_timeout(1500)
        ok(not any("A 稿" in n["text"] for n in disk_notes(other)), "服务刚切换就写，也不会写进 B 稿的 notes.json")
        ok("服务不匹配" in page.inner_text("#mode") and "待补传" in page.inner_text("#mode"), "页面显示「服务不匹配」，改动留在待补传")
        stop(srv_b)
        srv = start_server(sdir, port)
        page.wait_for_timeout(5500)
        ok(any(n["text"] == "切换后立刻写的 A 稿意见" for n in disk_notes(sdir)), "换回本审阅的服务后补传到正确的位置")

        # ---------- 8. 恶意网页与导入 ----------
        evil_dir = tmp / "evil"
        evil_dir.mkdir()
        (evil_dir / "index.html").write_text(f"""<!doctype html><script>
window.results = {{}};
const T = "http://127.0.0.1:{port}";
(async () => {{
  try {{ const r = await fetch(T + "/api/notes", {{method:"POST", headers:{{"Content-Type":"application/json"}},
        body: JSON.stringify({{page:1, text:"EVIL-json"}})}}); window.results.json = r.status; }}
  catch (e) {{ window.results.json = "blocked"; }}
  try {{ await fetch(T + "/api/notes", {{method:"POST", mode:"no-cors", headers:{{"Content-Type":"text/plain"}},
        body: JSON.stringify({{page:1, text:"EVIL-nocors"}})}}); window.results.nocors = "sent"; }}
  catch (e) {{ window.results.nocors = "blocked"; }}
  try {{ await fetch(T + "/api/handoff", {{method:"POST", mode:"no-cors", headers:{{"Content-Type":"text/plain"}},
        body: JSON.stringify({{message:"EVIL"}})}}); }} catch (e) {{}}
  try {{ const r = await fetch(T + "/api/notes"); window.results.read = await r.text(); }}
  catch (e) {{ window.results.read = "blocked"; }}
  const f = document.createElement("form"); f.method = "POST"; f.action = T + "/api/notes"; f.enctype = "text/plain";
  f.target = "sink"; const i = document.createElement("input"); i.name = '{{"page":1,"text":"EVIL-form","x":"'; i.value = '"}}';
  f.appendChild(i); const fr = document.createElement("iframe"); fr.name = "sink"; document.body.append(fr, f); f.submit();
  window.results.done = true;
}})();
</script>""", encoding="utf-8")
        eport = free_port()
        ehttp = ThreadingHTTPServer(("127.0.0.1", eport), partial(SimpleHTTPRequestHandler, directory=str(evil_dir)))
        threading.Thread(target=ehttp.serve_forever, daemon=True).start()
        before = len(disk_notes(sdir))
        hbefore = len(json.loads((sdir / "handoff.json").read_text())["items"])
        epage = ctx.new_page()
        epage.goto(f"http://localhost:{eport}/")  # 另一个来源
        epage.wait_for_function("window.results && window.results.done", timeout=10000)
        epage.wait_for_timeout(1500)
        res = epage.evaluate("window.results")
        after = disk_notes(sdir)
        ok(len(after) == before and not any("EVIL" in n["text"] for n in after)
           and len(json.loads((sdir / "handoff.json").read_text())["items"]) == hbefore,
           f"跨源网页的 fetch / no-cors / 表单写入全部失败（{res.get('json')}, {res.get('nocors')}）")
        ok(res.get("read") == "blocked", "跨源网页读不到批注内容")
        ok("[reject]" in (sdir / "server.log").read_text(), "被拒请求记录在 server.log")
        ehttp.shutdown()
        epage.close()
        page.bring_to_front()
        go_page(page, 2)
        evil = [{"id": 'z"><img src=x onerror="window.__p_id=1">', "page": 2, "target": "报告",
                 "text": '<img src=x onerror="window.__p_txt=1">注入测试', "quote": "<script>window.__p_q=1</script>"}]
        page.keyboard.press("n")
        page.click("#openImp")
        page.fill("#impText", json.dumps({"notes": evil}, ensure_ascii=False))
        page.click("#doImp")
        page.wait_for_timeout(900)
        page.keyboard.press("Escape")
        pwned = page.evaluate("[window.__p_id, window.__p_txt, window.__p_q]")
        ok(pwned == [None, None, None] and "注入测试" in page.inner_text("#nlist"), "导入带恶意 id / 内容的批注：照常显示为文字，不执行脚本")
        imported = [n for n in disk_notes(sdir) if "注入测试" in n["text"]]
        ok(imported and imported[0]["id"].replace("-", "").replace("_", "").isalnum(), "恶意 id 落盘前被重新生成")
        page.keyboard.press("n")
        page.click("#openImp")
        page.fill("#impText", (tmp / "export.md").read_text())
        page.click("#doImp")
        page.wait_for_timeout(900)
        texts = [n["text"] for n in disk_notes(sdir)]
        ok("旧版批注甲" in texts and "file 模式批注" in texts, "粘贴导出的 Markdown 导入到服务端 notes.json")
        resolved = [n for n in disk_notes(sdir) if n["text"] == "file 模式批注"]
        ok(resolved and resolved[0]["status"] == "resolved", "导入保留已处理状态")
        ctx.close()
        browser.close()
        stop(srv)
    failed = sum(not p for p, _ in RESULTS)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""浏览器端到端测试（真 Chrome）。测试专用依赖 playwright，skill 本身不依赖它。

  uv run --no-project --with playwright python tests/e2e_browser.py

覆盖：
1. 无服务（file://）：本地文件模式、批注存浏览器且刷新后仍在、发现旧版批注并导入、导出含已处理标记、
   报告样式在 Shadow DOM 里保真、CSP 拦住注入的内联事件；
2. 有服务：已连接、批注落盘、刷新后以服务端为准、Claude 用 CLI 标已处理后页面自动刷新、重建提示、
   轮询刷新不打断正在写的草稿、翻页草稿各自保留；
3. 服务中途断开：改动暂存浏览器，同文不同标点的两条都能补传；
4. 服务端写入失败（5xx）：改动不丢，恢复后补传；
5. 同一端口换成另一份审阅的服务：页面识别不匹配，不往别的审阅里写；换回后补传；
6. 恶意网页（另一个来源）用 fetch / 表单 / no-cors 写批注：全部失败；
7. 导入带恶意 id / 内容的批注：不执行脚本，id 被重新生成；粘贴旧版 Markdown 导入保留已处理状态。
"""
from __future__ import annotations

import json
import os
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
    for _ in range(50):
        info = d / ".annotation-server.json"
        if info.exists() and json.loads(info.read_text()).get("port") == port:
            return p
        time.sleep(0.1)
    p.terminate()
    raise RuntimeError("server did not start on the expected port: " + p.communicate()[1])


def stop(p):
    p.terminate()
    p.communicate(timeout=10)


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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ae-e2e-"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)

        # ---------- 1. file:// 退回模式 ----------
        fdir = tmp / "file_mode"
        build(fdir)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto((fdir / "index.html").as_uri())
        ok("本地文件模式" in page.inner_text("#mode"), "file:// 打开显示「本地文件模式」")
        go_page(page, 3)
        add_note(page, "file 模式批注", pin=(0.5, 0.5))
        page.reload()
        page.wait_for_timeout(300)
        ok("file 模式批注" in page.inner_text("#script"), "file:// 刷新后批注仍在（localStorage）")
        page.evaluate("""() => { const st = document.getElementById('host').shadowRoot.getElementById('stage');
            st.insertAdjacentHTML('beforeend', '<img src="x:y" onerror="window.__p_file=1">'); }""")
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
        ok(n == 3, f"旧批注导入后共 3 条（实际 {n}）")
        ok(not page.is_visible("#legacyBtn"), "导入后不再提示旧批注")
        page.keyboard.press("Escape")
        page.evaluate("document.querySelector('[data-note] [data-act=\"resolve\"]')?.click()")
        page.wait_for_timeout(200)
        md = page.evaluate("exportText()")
        ok("【已处理】file 模式批注" in md and "## 第 4 页" in md, "导出 Markdown 含页码与已处理标记")
        (tmp / "export.md").write_text(md, encoding="utf-8")
        ctx.close()

        # 报告样式保真：:root 变量、外壳同名变量不渗入
        css_dir = tmp / "css_src"
        css_dir.mkdir()
        (css_dir / "r.html").write_text(
            "<!doctype html><title>样式</title><style>:root{--brand:rgb(255, 0, 0)} body{font-size:30px}"
            " h2{color:var(--brand)} .acc{color:var(--accent, rgb(0, 128, 0))} .page{width:1280px;height:720px}</style>"
            '<section class="page" data-page="1" data-key="a"><h2>标题</h2><p class="acc">正文</p></section>', encoding="utf-8")
        build(tmp / "css_review", css_dir / "r.html", None)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto((tmp / "css_review" / "index.html").as_uri())
        colors = page.evaluate("""() => { const r = document.getElementById('host').shadowRoot;
            return [getComputedStyle(r.querySelector('h2')).color, getComputedStyle(r.querySelector('.acc')).color,
                    getComputedStyle(r.querySelector('.acc')).fontSize]; }""")
        ok(colors[0] == "rgb(255, 0, 0)", f"报告 :root 变量在 Shadow DOM 里生效（{colors[0]}）")
        ok(colors[1] == "rgb(0, 128, 0)", f"外壳的同名 CSS 变量不渗入报告（{colors[1]}）")
        ok(colors[2] == "30px", f"报告 body 样式生效（字号 {colors[2]}）")
        ctx.close()

        # ---------- 2. 服务模式 ----------
        sdir = tmp / "server_mode"
        build(sdir)
        port = free_port()
        srv = start_server(sdir, port)
        ctx = browser.new_context()
        page = ctx.new_page()
        base = f"http://127.0.0.1:{port}/"
        page.goto(base)
        page.wait_for_timeout(600)
        ok("已连接本地服务" in page.inner_text("#mode"), "经服务打开显示「已连接本地服务」")
        go_page(page, 3)
        page.evaluate("""() => { const p = document.querySelector('.talk p'); const r = document.createRange();
            r.selectNodeContents(p); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup')); }""")
        add_note(page, "服务模式批注：讲法第一句太长", pin=(0.3, 0.4))
        notes = disk_notes(sdir)
        ok(len(notes) == 1 and notes[0]["page"] == 3 and notes[0]["pin"] and notes[0]["quoteFrom"] == "讲稿",
           "批注即时写入 notes.json（页码、标点、讲稿引用）")
        log = (sdir / "notes.jsonl").read_text().splitlines()
        ok(len(log) == 1 and json.loads(log[0])["op"] == "add", "notes.jsonl 追加 add 记录")
        ok(page.evaluate("localStorage.getItem('ae.' + RID + '.notes')") is None, "服务模式不在浏览器里留整份批注副本")
        page.click('[data-act="edit"]')
        page.fill("#etext", "服务模式批注（已编辑）")
        page.click('[data-act="save"]')
        page.wait_for_timeout(400)
        ok(disk_notes(sdir)[0]["text"] == "服务模式批注（已编辑）", "页面编辑批注后落盘")
        page.reload()
        page.wait_for_timeout(600)
        ok("服务模式批注（已编辑）" in page.inner_text("#script"), "刷新后从服务端恢复批注")
        page.evaluate("""() => { const st = document.getElementById('host').shadowRoot.getElementById('stage');
            st.insertAdjacentHTML('beforeend', '<img src="x:y" onerror="window.__p_srv=1">'); }""")
        page.wait_for_timeout(300)
        ok(page.evaluate("window.__p_srv") is None, "服务模式下 CSP 拦住注入的内联事件")
        # 正在写草稿时，Claude 标已处理触发轮询刷新：草稿与焦点都要保住
        page.click("#ntext")
        page.keyboard.type("正在写的草稿")
        nid = disk_notes(sdir)[0]["id"]
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(sdir), nid, "--note", "已拆成两句"],
                       check=True, capture_output=True)
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
        page.fill("#ntext", "")
        time.sleep(1.1)
        subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), "--rebuild", str(sdir)], check=True, capture_output=True)
        page.wait_for_timeout(5000)
        ok(page.is_visible("#reloadBtn"), "Claude 重建对照页后页面提示「点此刷新」")

        # ---------- 3. 断线暂存与补传 ----------
        stop(srv)
        page.wait_for_timeout(4500)
        ok("服务未连接" in page.inner_text("#mode"), "服务停止后页面提示未连接")
        add_note(page, "删", pin=(0.2, 0.2))
        add_note(page, "删", pin=(0.7, 0.7))
        ok("待补传 2" in page.inner_text("#mode"), "断线时两条批注暂存浏览器并显示待补传")
        srv = start_server(sdir, port)
        page.wait_for_timeout(5500)
        dels = [n for n in disk_notes(sdir) if n["text"] == "删"]
        ok(len(dels) == 2 and "已连接" in page.inner_text("#mode"), f"服务恢复后补传，同文不同标点两条都在（{len(dels)} 条）")

        # ---------- 4. 服务端写入失败 ----------
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

        # ---------- 5. 同一端口换成另一份审阅 ----------
        other = tmp / "other_review"
        build(other)
        stop(srv)
        page.wait_for_timeout(4500)
        add_note(page, "只属于 A 稿的意见")
        srv_b = start_server(other, port)
        page.wait_for_timeout(5500)
        ok("服务不匹配" in page.inner_text("#mode"), "同一端口换成另一份审阅的服务，页面显示「服务不匹配」")
        ok(not any("A 稿" in n["text"] for n in disk_notes(other)), "不会把 A 稿的批注写进 B 稿的 notes.json")
        stop(srv_b)
        srv = start_server(sdir, port)
        page.wait_for_timeout(5500)
        ok(any(n["text"] == "只属于 A 稿的意见" for n in disk_notes(sdir)), "换回本审阅的服务后补传到正确的位置")

        # ---------- 6. 恶意网页 ----------
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
        epage = ctx.new_page()
        epage.goto(f"http://localhost:{eport}/")  # 另一个来源
        epage.wait_for_function("window.results && window.results.done", timeout=10000)
        epage.wait_for_timeout(1500)
        res = epage.evaluate("window.results")
        after = disk_notes(sdir)
        ok(len(after) == before and not any("EVIL" in n["text"] for n in after),
           f"跨源网页的 fetch / no-cors / 表单写入全部失败（{res.get('json')}, {res.get('nocors')}）")
        ok(res.get("read") == "blocked", "跨源网页读不到批注内容")
        ok("[reject]" in (sdir / "server.log").read_text(), "被拒请求记录在 server.log")
        ehttp.shutdown()
        epage.close()

        # ---------- 7. 导入：恶意 id / 内容、旧版 Markdown ----------
        page.bring_to_front()
        go_page(page, 2)
        evil = [{"id": 'z"><img src=x onerror="window.__p_id=1">', "page": 2, "target": "报告",
                 "text": '<img src=x onerror="window.__p_txt=1">注入测试', "quote": "<script>window.__p_q=1</script>"}]
        page.keyboard.press("n")
        page.click("#openImp")
        page.fill("#impText", json.dumps({"notes": evil}, ensure_ascii=False))
        page.click("#doImp")
        page.wait_for_timeout(800)
        page.keyboard.press("Escape")
        pwned = page.evaluate("[window.__p_id, window.__p_txt, window.__p_q]")
        ok(pwned == [None, None, None] and "注入测试" in page.inner_text("#nlist"), "导入带恶意 id / 内容的批注：照常显示为文字，不执行脚本")
        imported = [n for n in disk_notes(sdir) if "注入测试" in n["text"]]
        ok(imported and imported[0]["id"].replace("-", "").replace("_", "").isalnum(), "恶意 id 落盘前被重新生成")
        page.keyboard.press("n")
        page.click("#openImp")
        page.fill("#impText", (tmp / "export.md").read_text())
        page.click("#doImp")
        page.wait_for_timeout(800)
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

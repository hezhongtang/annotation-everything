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
9. 语言：单文件内嵌词典；file:// 与服务模式都能加批注；计数覆盖 0/1/2；切换语言不写批注、不改审阅标识；auto 跟随浏览器语言。
10. 讲稿语言：英文讲稿在两种界面语言下逐页浏览（栏目标签跟界面，正文和时长跟讲稿）；缺页时 --lenient 显示空讲稿。
11. 双语导出与跨语言导入：中英文 Markdown 往返、页面↔CLI 互导、解析与界面语言无关、旧中文格式不退步、认不出的文字有明确提示。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from e2e_servers import CLEANUP, free_port, install, start_server, stop

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURE = ROOT / "fixtures" / "sample"
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-e2e-home-")   # 测试不碰用户真实的审阅台账；子进程继承
os.environ["HOME"] = tempfile.mkdtemp(prefix="ae-e2e-user-")       # 技能扫描只使用合成目录
os.environ["CODEX_HOME"] = str(Path(os.environ["HOME"]) / ".codex")
os.environ["CLAUDE_SKILLS_DIR"] = str(Path(os.environ["HOME"]) / ".claude" / "skills")
install()   # 兜底清理：正常退出、未捕获异常、Ctrl-C 走 atexit，SIGTERM / SIGHUP 另接
CLEANUP.add_dir(Path(os.environ["AE_HOME"]))
CLEANUP.add_dir(Path(os.environ["HOME"]))
RESULTS: list[tuple[bool, str]] = []
STAGE = "document.getElementById('host').shadowRoot.getElementById('ae-stage')"


def ok(cond, label):
    RESULTS.append((bool(cond), label))
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)


def build(out: Path, report: Path = FIXTURE / "report.html", script: Path | None = FIXTURE / "script.md",
          name: str | None = None, locale: str | None = None, lenient: bool = False):
    cmd = [sys.executable, str(SCRIPTS / "build_viewer.py"), "--report", str(report), "--out", str(out)]
    if script:
        cmd += ["--script", str(script)]
    if name:
        cmd += ["--name", name]
    if locale:
        cmd += ["--locale", locale]
    if lenient:
        cmd += ["--lenient"]
    subprocess.run(cmd, check=True, capture_output=True)


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


def wait_until(cond, page, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        page.wait_for_timeout(200)
    return cond()


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


def skill_search_checks(browser, tmp: Path):
    """通过真实浏览器验证技能输入；目录内容均为合成数据。"""
    d = tmp / "skill_search"
    d.mkdir()
    (d / "skills.json").write_text(json.dumps({"version": 1, "skills": [
        {"name": "unsafe-name", "desc": '恶意 <img src=x onerror="window.__skill_xss=1">'},
        {"name": "writer", "desc": "文字和字体校对"},
        {"name": "chart", "desc": "图表设计"},
    ], "always": []}, ensure_ascii=False), encoding="utf-8")
    build(d)
    def exercise(page, mode):
        def check(value, label):
            ok(value, f"{mode}：{label}")
        page.fill("#nskills", "")
        options = page.locator('[role="listbox"] [role="option"]')
        check(options.locator(".skill-rec").count() == 0, "没有相关信号时不标记建议")
        page.keyboard.press("ArrowUp")
        page.keyboard.press("Enter")
        check(page.input_value("#nskills") == "chart", "向上键从最后一个候选开始")
        page.fill("#nskills", "字体")
        check(options.filter(has_text="writer").count() == 1, "按技能说明中的部分文字显示候选")
        page.keyboard.press("ArrowDown")
        if mode == "服务模式":
            page.wait_for_timeout(4500)  # 跨过一次服务轮询，选择不能丢失
        page.keyboard.press("Enter")
        check(page.input_value("#nskills") == "writer", "方向键和回车选择候选")
        page.fill("#nskills", "chart, 字体, custom")
        page.locator("#nskills").evaluate("el => { el.setSelectionRange(9, 9); el.dispatchEvent(new MouseEvent('click', {bubbles:true})); }")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        check(page.input_value("#nskills") == "chart, writer, custom", "替换光标所在技能并保留前后项")
        page.fill("#nskills", "CHa")
        check(options.filter(has_text="chart").count() == 1, "技能名称搜索不区分大小写")
        page.fill("#nskills", "恶意")
        check(options.filter(has_text="unsafe-name").count() == 1 and options.locator("img").count() == 0,
              "技能说明按文本展示，不注入 HTML")
        page.fill("#nskills", "字体")
        page.keyboard.press("ArrowDown")
        page.dispatch_event("#nskills", "keydown", {"key": "Enter", "isComposing": True, "keyCode": 229})
        check(page.input_value("#nskills") == "字体", "输入法组合时回车不选择候选")
        page.fill("#ntext", "合成搜索批注")
        page.fill("#nskills", "custom-skill")
        check(options.count() == 0, "无匹配时不强制选择")
        page.click('[data-act="add"]')   # 空结果浮层不能拦住添加按钮
        check("custom-skill" in page.inner_text("#nlist"), "无匹配的手工技能可随批注保存")
        page.locator('#nlist [data-act="edit"]').first.click()
        page.fill("#eskills", "字体")
        check(options.filter(has_text="writer").count() == 1, "编辑批注时也可按说明搜索")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        page.locator('#nlist [data-act="save"]').first.click()
        check("⚙ writer" in page.inner_text("#nlist"), "编辑批注可保存选择的技能")
        page.click("#alwaysBtn")
        page.fill("#alwaysRows .aname", "图表")
        check(options.filter(has_text="chart").count() == 1, "常驻技能按说明搜索")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        rows = page.locator("#alwaysRows .arow")
        check(rows.count() == 1 and rows.first.locator(".aname").input_value() == "chart",
              "选择高亮候选时回车不新增行")
        rows.first.locator(".ascope").select_option("讲稿")
        rows.first.locator(".aname").focus()
        page.keyboard.press("Enter")
        check(rows.count() == 2 and rows.first.locator(".ascope").input_value() == "讲稿",
              "未选择候选时回车新增行并保留原范围")
        last = rows.last.locator(".aname")
        check(last.evaluate("el => document.activeElement === el"), "新行自动获得焦点")
        last.fill("writer")
        last.dispatch_event("keydown", {"key": "Enter", "isComposing": True, "keyCode": 229})
        check(rows.count() == 2, "输入法组合时回车不新增行")
        page.keyboard.press("Control+Enter")
        check(wait_until(lambda: page.inner_text("#alwaysN") == "2", page, 5), "Ctrl+Enter 保存常驻技能")

    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto((d / "index.html").as_uri())
    exercise(page, "文件模式")
    ctx.close()
    port = free_port()
    server = start_server(d, port)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.getElementById('mode').classList.contains('server')")
        exercise(page, "服务模式")
        ctx.close()
    finally:
        stop(server)
def skill_recommendation_checks(browser, tmp: Path):
    d = tmp / "skill_recommendations"
    d.mkdir()
    report = tmp / "合成PPT报告.html"
    report.write_text((FIXTURE / "report.html").read_text(encoding="utf-8"), encoding="utf-8")
    skills = [{"name": "plain", "desc": "一般帮助"},
              {"name": "typography", "desc": "字体字号与专业排版"},
              {"name": "humanizer-zh", "desc": "中文写作与措辞润色"},
              {"name": "slides-format-check", "desc": "PPT 幻灯片格式检查"}]
    skills += [{"name": f"extra-{n:02d}", "desc": "其他技能"} for n in range(12)]
    (d / "skills.json").write_text(json.dumps({"version": 1, "skills": skills, "always": []}, ensure_ascii=False), encoding="utf-8")
    build(d, report=report, script=None)
    ok(str(tmp) not in (d / "index.html").read_text(encoding="utf-8"), "推荐上下文不暴露本机路径")
    def exercise(page, mode):
        def check(value, label):
            ok(value, f"{mode}：{label}")
        options = page.locator('[role="listbox"] [role="option"]')
        page.fill("#ntext", "请调整字体和对齐")
        page.click("#nskills")
        check(options.count() > 0 and "typography" in options.first.inner_text() and "建议" in options.first.inner_text(),
              "根据字体批注优先推荐相关技能")
        page.fill("#ntext", "请修正错别字并润色措辞")
        page.click("#nskills")
        check(options.count() > 0 and "humanizer-zh" in options.first.inner_text(), "批注变化后更新推荐")
        page.fill("#ntext", "")
        page.click("#nskills")
        check(options.count() > 0 and "slides-format-check" in options.first.inner_text(), "材料文件名可影响推荐")
        check(options.count() <= 8, "空输入只显示少量候选")
        more = page.get_by_role("button", name="查看全部技能")
        if more.count():
            more.click()
        check(options.count() == len(skills), "可浏览完整技能目录")
        page.fill("#nskills", "extra-11")
        check(options.filter(has_text="extra-11").count() == 1, "可搜索首屏之外的技能")
        page.fill("#nskills", "")
        page.fill("#ntext", "合成批注")
        page.click('[data-act="add"]')
        page.locator('#nlist [data-act="edit"]').first.click()
        page.fill("#etext", "修改错别字和措辞")
        page.click("#eskills")
        check(options.count() > 0 and "humanizer-zh" in options.first.inner_text(), "编辑批注时按当前内容推荐")
        page.fill("#ntext", "调整字体层级")
        page.click("#nskills")
        check(options.count() > 0 and "typography" in options.first.inner_text(), "新批注推荐不受另一个编辑草稿干扰")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto((d / "index.html").as_uri())
    exercise(page, "文件模式")
    ctx.close()
    port = free_port()
    server = start_server(d, port)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.getElementById('mode').classList.contains('server')")
        exercise(page, "服务模式")
        ctx.close()
    finally:
        stop(server)
    title_report = tmp / "neutral.html"
    title_report.write_text('<!doctype html><title>合成材料</title><section class="page" data-page="1" data-key="one"><h1>字体层级</h1></section>', encoding="utf-8")
    title_review = tmp / "title_recommendations"
    title_review.mkdir()
    (title_review / "skills.json").write_text(json.dumps({"version": 1, "skills": skills, "always": []}, ensure_ascii=False), encoding="utf-8")
    build(title_review, report=title_report, script=None)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto((title_review / "index.html").as_uri())
    page.click("#nskills")
    options = page.locator('[role="listbox"] [role="option"]')
    ok(options.count() > 0 and "typography" in options.first.inner_text(), "当前页面标题可影响技能推荐")
    ctx.close()


def skill_i18n_checks(browser, tmp: Path):
    """技能选择与常驻技能的中英界面：范围存储值不变、搜索覆盖原文、页面与 CLI 一致。

    目录全部合成：大目录、插件命名空间技能、中英混合说明和特殊字符候选。
    """
    d = tmp / "skill_i18n"
    d.mkdir()
    catalog = [{"name": f"skill-{n:03d}", "desc": f"合成说明 {n}"} for n in range(205)]
    catalog += [{"name": "sample:sample", "desc": "插件技能 plugin skill"},
                {"name": "润色（中文）", "desc": "中英混合说明 mixed-language"},
                {"name": 'odd"<>&', "desc": "特殊字符 <b>&</b>"}]
    always = [{"name": "humanizer-zh", "scope": "讲稿"}, {"name": "checker", "scope": "报告"},
              {"name": "stylish", "scope": "style"}, {"name": "every", "scope": "all"}]
    (d / "skills.json").write_text(json.dumps({"version": 1, "skills": catalog, "always": always},
                                              ensure_ascii=False), encoding="utf-8")
    build(d, locale="en")

    def exercise(page, mode):
        def check(value, label):
            ok(value, f"{mode}：{label}")
        options = page.locator('[role="listbox"] [role="option"]')
        check(page.get_attribute("#nskills", "placeholder") == "Optional, e.g. humanizer-zh",
              "英文技能输入提示")
        check(page.inner_text("#alwaysBtn").strip().startswith("Standing skills"), "英文常驻技能按钮")
        check(page.get_attribute("#skillList", "aria-label") == "Skill suggestions", "英文候选列表无障碍名称")
        page.fill("#ntext", "请改标题")
        page.click('[data-act="add"]')
        page.wait_for_timeout(400)
        check("Standing checker" in page.inner_text("#nlist"), "英文卡片上的常驻技能标记")
        check("Standing every" in page.inner_text("#nlist"), "全部批注范围的常驻技能也标出")
        page.fill("#nskills", "plugin")
        check(options.filter(has_text="sample:sample").count() == 1, "按原始说明搜索命中插件技能")
        page.fill("#nskills", "mixed-language")
        check(options.filter(has_text="润色（中文）").count() == 1, "按英文说明片段搜索命中混合语言技能")
        page.fill("#nskills", "SKILL-204")
        check(options.filter(has_text="skill-204").count() == 1, "首屏之外的大目录候选可搜索")
        page.fill("#nskills", "zzz-no-such")
        check(options.count() == 0 and "No matching skills" in page.inner_text("#skillList"),
              "英文空结果提示")
        page.click('[data-act="add"]')
        check(page.locator("#nlist [data-note]").count() >= 1,
              "空结果浮层不拦住下面的添加按钮")
        page.fill("#nskills", "")
        more = page.get_by_role("button", name="See all skills")
        if more.count():
            more.click()
        check(options.count() == len(catalog), "英文可浏览完整技能目录")
        page.locator("#nskills").press("Escape")

        page.click("#alwaysBtn")
        rows = page.locator("#alwaysRows .arow")
        check(rows.count() == 4, "英文常驻技能弹窗列出全部范围")
        check(rows.nth(0).locator(".ascope").input_value() == "讲稿", "范围存储值仍是讲稿")
        check(rows.nth(0).locator(".ascope option").first.inner_text() == "All annotations",
              "范围下拉显示英文标签")
        check(rows.nth(3).locator(".ascope").input_value() == "all", "全部批注的存储值仍是 all")
        check(page.inner_text("#always .hint").startswith("In this review"), "英文弹窗说明")
        rows.nth(0).locator(".aname").fill("sample:sample")
        rows.nth(0).locator(".ascope").select_option("style")
        draft_before = page.evaluate("""() => ({
          rows: [...document.querySelectorAll('#alwaysRows .arow')].map(r =>
            [r.querySelector('.aname').value, r.querySelector('.ascope').value]),
          saved: JSON.stringify(alwaysSkills), opened: alwaysOpenSig
        })""")
        writes = []

        def record_write(request):
            if request.method in ("POST", "PATCH", "DELETE"):
                writes.append(request.url)

        page.on("request", record_write)
        page.select_option("#localePick", "zh-CN")
        check(rows.nth(0).locator(".ascope option").nth(3).inner_text() == "风格优化"
              and rows.nth(0).locator(".aname").get_attribute("placeholder") == "技能名，如 humanizer-zh"
              and rows.nth(0).locator(".adel").get_attribute("aria-label") == "去掉",
              "弹窗开着切中文刷新范围、输入提示和删除名称")
        page.select_option("#localePick", "en")
        check(rows.nth(0).locator(".ascope option").nth(3).inner_text() == "Style"
              and rows.nth(0).locator(".aname").get_attribute("placeholder") == "Skill name, e.g. humanizer-zh"
              and rows.nth(0).locator(".adel").get_attribute("title") == "Remove this one",
              "弹窗开着切英文刷新行文案")
        draft_after = page.evaluate("""() => ({
          rows: [...document.querySelectorAll('#alwaysRows .arow')].map(r =>
            [r.querySelector('.aname').value, r.querySelector('.ascope').value]),
          saved: JSON.stringify(alwaysSkills), opened: alwaysOpenSig
        })""")
        page.remove_listener("request", record_write)
        check(draft_after == draft_before and not writes,
              "弹窗切语言保留临时值和冲突快照、不保存或发送写请求")
        rows.nth(0).locator(".aname").focus()
        page.keyboard.press("Control+Enter")
        check(wait_until(lambda: page.inner_text("#alwaysN") == "4", page, 5), "Ctrl+Enter 保存常驻技能")

        page.select_option("#localePick", "zh-CN")
        check(page.get_attribute("#nskills", "placeholder") == "可选，如 humanizer-zh", "中文技能输入提示")
        check(page.inner_text("#alwaysBtn").strip().startswith("常驻技能"), "中文常驻技能按钮")
        page.fill("#nskills", "mixed-language")
        check(options.filter(has_text="润色（中文）").count() == 1, "中文界面仍按原始说明搜索")
        page.click("#alwaysBtn")
        rows = page.locator("#alwaysRows .arow")
        check(rows.nth(0).locator(".ascope").input_value() == "style", "切换语言不改范围存储值")
        check(rows.nth(0).locator(".ascope option").nth(3).inner_text() == "风格优化", "中文范围标签")
        check(rows.nth(0).locator(".aname").input_value() == "sample:sample", "切换语言不改技能调用名")
        page.locator("#always").get_by_role("button", name="取消").click()
        check("sample:sample（风格优化）" in (page.get_attribute("#alwaysBtn", "title") or ""),
              "中文按钮提示列出范围标签")

    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto((d / "index.html").as_uri())
    exercise(page, "文件模式")
    ctx.close()
    port = free_port()
    server = start_server(d, port)
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.getElementById('mode').classList.contains('server')")
        exercise(page, "服务模式")
        disk = json.loads((d / "skills.json").read_text(encoding="utf-8"))
        ok([x for x in disk["always"] if x["name"] == "sample:sample"] == [{"name": "sample:sample", "scope": "style"}],
           "服务模式保存后 skills.json 里的范围存储值不变")
        ok(len(disk["skills"]) == len(catalog), "保存常驻技能不截断技能目录")
        ctx.close()
    finally:
        stop(server)

    cli = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(d), "--locale", "en"],
                         capture_output=True, text=True, timeout=60)
    ok(cli.returncode == 0 and "sample:sample (Style)" in cli.stdout
       and f"skill-204" in cli.stdout and "Standing skills" in cli.stdout,
       "命令行英文输出与页面保存的结果一致")
    zh = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(d), "--locale", "zh-CN"],
                        capture_output=True, text=True, timeout=60)
    ok(zh.returncode == 0 and "sample:sample（风格优化）" in zh.stdout, "命令行中文输出保持原来的范围说法")
    from_review = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(d)],
                                 capture_output=True, text=True, timeout=60)
    ok("Standing skills" in from_review.stdout, "不指定语言时命令行跟随审阅保存的选择")
    notes = disk_notes(d)
    ok(any(n.get("text") == "请改标题" for n in notes), "批注本身不随语言改写")


def full_check_checks(browser, tmp: Path):
    """真实页面提交全量勾稽，并检查 Agent 接收和处理记录。"""
    d = tmp / "full_check"
    build(d)
    port = free_port()
    server = start_server(d, port)
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.getElementById('mode').classList.contains('server')")
        page.click("#handBtn")
        checkbox = page.locator("#hFullCheck")
        if not checkbox.count():
            ok(False, "交给 Agent 时可选择全量勾稽")
            return
        ok(not checkbox.is_checked(), "全量勾稽默认不勾选")
        checkbox.check()
        page.fill("#handMsg", "请核对整份材料")
        page.click("#doHand")
        saved = wait_until(lambda: (d / "handoff.json").exists(), page, 5)
        item = json.loads((d / "handoff.json").read_text())["items"][-1] if saved else {}
        ok(item.get("fullCheck") is True and item.get("userMessage") == "请核对整份材料"
           and item.get("message", "").count("全量勾稽") == 1, "勾选全量勾稽后保存要求和原始附言")
        received = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(d), "--timeout", "5"],
                                  capture_output=True, text=True, timeout=15)
        ok(received.returncode == 0 and "全量勾稽" in received.stdout, "等待中的 Agent 收到全量勾稽要求")
        page.keyboard.press("j")
        page.wait_for_timeout(500)
        ok("全量勾稽" in page.inner_text("#batchList"), "处理记录显示全量勾稽要求")
        page.keyboard.press("Escape")
        page.click("#handBtn")
        ok(page.is_checked("#hFullCheck"), "再次提交时记住这份审阅的勾选状态")

        ctx.set_offline(True)
        page.click("#doHand")
        rid = json.loads((d / "review.json").read_text())["reviewId"]
        key = f"ae.{rid}.queue"
        queued = wait_until(lambda: page.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]').length", key) > 0, page, 5)
        ok(queued, "断线提交时全量勾稽要求留在待补传队列")
        page.close()
        ctx.set_offline(False)
        old = ctx.new_page()

        def no_full_check(route):
            response = route.fetch()
            body = response.json()
            body["features"] = [feature for feature in body.get("features", []) if feature != "full-check"]
            route.fulfill(response=response, json=body)

        old.route("**/api/notes", no_full_check)
        old.goto(f"http://127.0.0.1:{port}/")
        old.wait_for_timeout(1200)
        items = json.loads((d / "handoff.json").read_text())["items"]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(len(items) == 1 and remaining and remaining[0].get("fullCheck") is True,
           "旧服务不能静默丢弃暂存的全量勾稽要求")
        old.click("#handBtn")
        old.click("#doHand")
        ok(old.is_visible("#hFullOld") and old.is_visible("#hand"), "旧服务拒绝新提交的全量勾稽并提示升级")
        old.keyboard.press("Escape")
        old.unroute("**/api/notes", no_full_check)
        flushed = wait_until(lambda: len(json.loads((d / "handoff.json").read_text())["items"]) == 2, old, 7)
        items = json.loads((d / "handoff.json").read_text())["items"]
        ok(flushed and items[-1].get("fullCheck") is True, "新版服务恢复后，暂存的全量勾稽提交自动补传")
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(d)], capture_output=True, text=True, check=True)
        old.click("#handBtn")
        old.uncheck("#hFullCheck")
        old.click("#doHand")
        saved = wait_until(lambda: len(json.loads((d / "handoff.json").read_text())["items"]) == 3, old, 5)
        items = json.loads((d / "handoff.json").read_text())["items"]
        ok(saved and items[-1].get("fullCheck") is False, "取消勾选后下一批不要求全量勾稽")

        old.click("#handBtn")
        old.check("#hFullCheck")
        old.route("**/api/notes", no_full_check)
        old.click("#doHand")
        old.wait_for_timeout(800)
        items = json.loads((d / "handoff.json").read_text())["items"]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(items[-1].get("fullCheck") is False and remaining and remaining[0].get("fullCheck") is True,
           "服务换成旧版后立即提交，也不会丢掉全量勾稽要求")
        old.unroute("**/api/notes", no_full_check)
        flushed = wait_until(lambda: json.loads((d / "handoff.json").read_text())["items"][-1].get("fullCheck") is True, old, 7)
        ok(flushed, "重新连接新版服务后合并更新暂存的全量勾稽要求")

        old.evaluate("""key => localStorage.setItem(key, JSON.stringify([
            {kind:'handoff', message:'旧队列补充', target:'final', allowSubagents:false}
        ]))""", key)
        old.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        merged = wait_until(lambda: json.loads((d / "handoff.json").read_text())["items"][-1].get("userMessage") == "旧队列补充", old, 7)
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        ok(merged and item.get("fullCheck") is True, "旧版暂存队列没有该字段时保留原有勾选")

        old.click("#handBtn")
        old.uncheck("#hFullCheck")
        old.click("#doHand")
        wait_until(lambda: json.loads((d / "handoff.json").read_text())["items"][-1].get("fullCheck") is False, old, 5)
        old.click("#handBtn")
        old.check("#hFullCheck")
        long_message = "补" * 4000
        old.fill("#handMsg", long_message)

        def legacy_post(route):
            if route.request.method != "POST":
                route.continue_()
                return
            payload = route.request.post_data_json
            payload.pop("fullCheck", None)
            payload.pop("userMessage", None)
            payload["message"] = payload.get("message", "")[:4000]  # 旧服务的附言长度上限
            response = route.fetch(post_data=json.dumps(payload, ensure_ascii=False))
            body = response.json()
            body.pop("acceptedFullCheck", None)
            route.fulfill(response=response, json=body)

        old.route("**/api/handoff", legacy_post)
        old.click("#doHand")
        old.wait_for_timeout(800)
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(item.get("fullCheck") is False and all(word in item.get("message", "") for word in ("全量勾稽", "报告和讲稿", "有依据", "不确定"))
           and remaining and remaining[0].get("fullCheck") is True and "截短附言" in old.inner_text("#toast"),
           "提交时旧服务未确认勾选：保留待补传要求")
        old.unroute("**/api/handoff", legacy_post)
        flushed = wait_until(lambda: json.loads((d / "handoff.json").read_text())["items"][-1].get("fullCheck") is True, old, 7)
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        ok(flushed and item.get("userMessage") == long_message, "新版服务确认后才清除待补传要求，完整保留长附言")

        old.click("#handBtn")
        old.uncheck("#hFullCheck")
        old.route("**/api/handoff", legacy_post)
        old.click("#doHand")
        old.wait_for_timeout(800)
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(item.get("fullCheck") is True and remaining and remaining[0].get("fullCheck") is False,
           "旧服务未确认取消勾选：保留待补传要求")
        old.unroute("**/api/handoff", legacy_post)
        flushed = wait_until(lambda: json.loads((d / "handoff.json").read_text())["items"][-1].get("fullCheck") is False, old, 7)
        ok(flushed, "新版服务确认后才清除待补传的取消勾选要求")
    finally:
        ctx.close()
        stop(server)


def locale_edit_checks(page, mode, read_notes):
    """语言切换保留已有批注的编辑草稿，保存和取消都以用户的选择为准。"""
    original = read_notes()[0]
    nid = original["id"]
    source = original["page"]
    target = page.evaluate("source => P.find(p => p.n !== source).n", source)
    card = page.locator(f'#nlist [data-note="{nid}"]')
    edited_text = "切换语言后保存的合成批注"

    card.locator('[data-act="edit"]').click()
    page.fill("#etext", edited_text)
    page.fill("#eskills", "locale-test-skill")
    page.select_option("#epage", str(target))
    for locale in ("en", "zh-CN"):
        page.select_option("#localePick", locale)
        ok(page.input_value("#epage") == str(target)
           and page.input_value("#etext") == edited_text
           and page.input_value("#eskills") == "locale-test-skill",
           f"{mode}：切换到 {locale} 保留编辑中的正文、技能和目标页")
    ok(read_notes() == [original], f"{mode}：切换语言不会提前保存编辑草稿")
    card.locator('[data-act="save"]').click()

    def saved():
        note = next((n for n in read_notes() if n["id"] == nid), {})
        return (note.get("page") == target and note.get("text") == edited_text
                and note.get("skills") == ["locale-test-skill"])

    ok(wait_until(saved, page, 5), f"{mode}：保存后批注移动到所选页并保留正文和技能")
    go_page(page, target)
    # 保存后的编辑缓存必须清除；取消也不能被重绘时重新缓存的旧卡片覆盖。
    card.locator('[data-act="edit"]').click()
    ok(page.input_value("#epage") == str(target), f"{mode}：再次编辑使用已保存的页码")
    page.fill("#etext", "这段草稿应被取消")
    page.fill("#eskills", "cancelled-test-skill")
    page.select_option("#epage", str(source))
    page.select_option("#localePick", "en")
    card.locator('[data-act="cancel"]').click()
    card.locator('[data-act="edit"]').click()
    ok(page.input_value("#epage") == str(target)
       and page.input_value("#etext") == edited_text
       and page.input_value("#eskills") == "locale-test-skill",
       f"{mode}：取消后重新编辑不会恢复已取消的正文、技能或目标页")
    card.locator('[data-act="cancel"]').click()


def locale_live_controls_checks(browser, tmp):
    """同一页切语言只改系统标签，原文、选区和待提交标注留在原位。"""
    d = tmp / "locale_live_controls"
    build(d, locale="zh-CN")
    # 没有讲稿前言时 auto 也必须先解析成词典支持的具体语言。
    build(tmp / "locale_auto_without_intro", script=None, locale="auto")
    ok(True, "auto 语言在仅报告模式下生成默认简介")
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto((d / "index.html").as_uri())
    go_page(page, 3)
    page.evaluate("window.__originalTalk = document.querySelector('#pageScript .talk')")
    body = page.inner_text("#pageScript .talk")
    page.fill("#ntext", "未提交的合成草稿 <&>")
    for lang, start, labels, qa in (
        ("en", "Page 3", ("Talking points", "Transition", "Likely questions"), "Q: "),
        ("zh-CN", "第 3", ("讲法", "过渡", "可能被问"), "问　"),
    ):
        page.select_option("#localePick", lang)
        ok(page.inner_text("#stat").startswith(start)
           and all(label in page.inner_text("#pageScript") for label in labels),
           f"同页切到 {lang} 后页码与讲稿标签立即更新")
        ok(page.locator("#pageScript .q").first.get_attribute("data-label") == qa,
           f"同页切到 {lang} 后问答前缀立即更新")
        ok(page.evaluate("window.__originalTalk === document.querySelector('#pageScript .talk')")
           and page.inner_text("#pageScript .talk") == body
           and page.input_value("#ntext") == "未提交的合成草稿 <&>",
           f"同页切到 {lang} 不替换材料节点或丢失草稿")
        ok(page.get_attribute("#host", "lang") == "zh-CN"
           and page.get_attribute("#pageScript .talk", "lang") == "",
           f"切到 {lang} 后保留报告语言，未知讲稿语言不冒用界面语言")
    special = '<img src=x onerror="window.__quoteInjected=1"> & "原文"'
    page.evaluate("""text => {
      quote = text; quoteFrom = '报告';
      sel = {text, from:'报告', pair:{side:'讲稿', text, match:'exact'}};
      quotePair = sel.pair; document.getElementById('pairOn').checked = false;
      showQuote(); updatePairText();
      pendingRegion = {x:10,y:10,w:20,h:20}; drawPins();
      openPinPop('', stage.querySelector('.ae-region.pending'));
    }""", special)
    for lang, tip in (("en", "drag"), ("zh-CN", "拖动")):
        # 程序触发 select change，保留已打开的标注浮层来验证重刷。
        page.locator("#localePick").evaluate("(el, lang) => {el.value=lang;el.dispatchEvent(new Event('change'))}", lang)
        ok(special in page.inner_text("#qbox") and page.locator("#qbox img").count() == 0
           and page.evaluate("window.__quoteInjected") is None,
           f"{lang} 引用和对应句的特殊字符只转义一次且不执行 HTML")
        ok(tip in page.locator(".ae-region.pending").get_attribute("title"),
           f"{lang} 标注工具提示跟随当前语言")
        ok(("New selected area" if lang == "en" else "新框选") in page.inner_text("#pinpop")
           and ("Speaker notes" if lang == "en" else "讲稿") in page.inner_text("#pairText"),
           f"{lang} 已打开浮层与双侧对应句提示刷新")
        ok(not page.is_checked("#pairOn") and page.evaluate("pendingRegion.w") == 20,
           f"{lang} 切换不改双侧选择或标注坐标")
    ctx.close()


def locale_editing_checks(page, mode, read_notes):
    """#30：中英文下完成编辑、标注与风格优化的常用操作，存储值与协议说法不变。"""
    def texts(sel):
        return [x.strip() for x in page.locator(sel).all_inner_texts()]

    def has(sel, want):
        return any(want in x for x in texts(sel))

    def visible(sel):
        try:
            page.wait_for_selector(sel, timeout=3000)
            return page.locator(sel).first.is_visible()
        except Exception:
            return False

    dialogs: list[str] = []
    answer = ["dismiss"]

    def on_dialog(dlg):
        dialogs.append(dlg.message)
        (dlg.accept() if answer[0] == "accept" else dlg.dismiss())

    page.on("dialog", on_dialog)

    # ---- 英文：卡片动作与删除确认 ----
    page.select_option("#localePick", "en")
    notes = read_notes()
    nid = notes[0]["id"]
    card = page.locator(f'#nlist [data-note="{nid}"]')
    ok(has(f'#nlist [data-note="{nid}"] .acts button', "Edit"), f"{mode}：英文卡片有编辑按钮")
    ok(has(f'#nlist [data-note="{nid}"] .acts button', "Delete"), f"{mode}：英文卡片有删除按钮")
    dialogs.clear()
    card.locator('[data-act="del"]').click()
    ok(wait_until(lambda: bool(dialogs), page, 5), f"{mode}：删除时弹出确认框")
    ok("Delete this annotation?" in (dialogs[0] if dialogs else ""),
       f"{mode}：英文删除确认用当前语言（实际 {dialogs[:1]}）")
    ok(len(read_notes()) == len(notes), f"{mode}：取消删除不改批注")

    # ---- 英文：选中文字后的操作条与双侧同步 ----
    close_overlays(page)
    go_page(page, 1)
    drag_select(page, page.locator("#pageScript .talk, #slide p, #slide li").first)
    ok(visible("#selbar.on"), f"{mode}：选中文字弹出操作条")
    ok(has("#selbar .sbtns button", "Delete") and has("#selbar .sbtns button", "Replace")
       and has("#selbar .sbtns button", "Annotate"),
       f"{mode}：英文操作条是删除/改为/批注（实际 {texts('#selbar .sbtns button')}）")
    for sel, attr, want in (("#selbar [data-sel='delete']", "title", "Delete the selected text (D)"),
                            ("#selbar [data-sel='replace']", "title", "Replace the selected text with… (R)"),
                            ("#selbar [data-sel='comment']", "title", "Annotate using the selected text as a quote (C)"),
                            ("#pairRow", "title", "(B toggles)"),
                            ("#pinBtn", "title", "Annotate on the report: click to drop a pin, or drag to select an area (P)"),
                            ("#styleBtn", "title", "(S)"),
                            ("#prev", "title", "(←)"), ("#next", "title", "(→)"),
                            ("#helpBtn", "title", "(H)"), ("#addBtn", "title", "Add annotation")):
        got = page.get_attribute(sel, attr) or ""
        want = want.replace("{key_placeholder}", "")
        ok(want in got, f"{mode}：{sel} 的 {attr} 是「{want}」（实际 {got!r}）")
        ok("{" not in got, f"{mode}：{sel} 的 {attr} 里残留了未替换的占位符（实际 {got!r}）")
    ok("The other side has the same or a similar sentence"
       in (page.get_attribute("#pairRow", "title") or ""), f"{mode}：英文双侧提示说明勾选后果")
    page.locator("#selbar [data-sel='replace']").click()
    ok(visible("#repbox.on"), f"{mode}：改写框弹出")
    ok("Replace the selected text with:" in page.inner_text("#repbox"), f"{mode}：英文改写框标题")
    ok(has("#repbox button", "OK") and has("#repbox button", "Cancel"),
       f"{mode}：英文改写框按钮（实际 {texts('#repbox button')}）")
    close_overlays(page)

    # ---- 英文：标注与风格优化 ----
    ok("Annotate on the report" in page.inner_text("#pinBtn"), f"{mode}：英文标注按钮")

    page.click("#styleBtn")
    ok(visible("#stylemenu.on"), f"{mode}：风格菜单弹出")
    ok(has("#stylemenu button", "Whole page") and has("#stylemenu button", "At the mark"),
       f"{mode}：英文风格菜单（实际 {texts('#stylemenu button')}）")
    ok("Style scope:" in page.inner_text("#stylemenu .hint"), f"{mode}：英文风格菜单标题")
    page.click('#stylemenu [data-sm="page"]')
    ok(visible("#styleChip.on"), f"{mode}：英文风格条出现")
    ok(page.inner_text("#styleChip").startswith("Style · whole page"),
       f"{mode}：英文风格条写明范围（实际 {page.inner_text('#styleChip')}）")
    ok(page.get_attribute("#ntext", "placeholder").startswith("The direction you want"),
       f"{mode}：英文风格输入提示")
    page.locator("#styleChip button").click()
    ok(page.get_attribute("#ntext", "placeholder").startswith("Write your change request"),
       f"{mode}：取消风格后输入提示回到常规")

    # ---- 英文：页码与讲稿栏 ----
    ok(page.inner_text("#prev").strip().startswith("Previous"), f"{mode}：英文上一页")
    ok(page.inner_text("#next").strip().startswith("Next"), f"{mode}：英文下一页")
    ok("Jump to page" == (page.get_attribute("#jump", "aria-label") or ""), f"{mode}：英文页码下拉无障碍名称")
    stat = page.inner_text("#stat")
    ok(stat.startswith("Page ") and " · About " in stat and "in total" in stat,
       f"{mode}：英文统计行页码与时长都完整（实际 {stat!r}）")
    ok("Talking points" in page.inner_text("#pageScript"), f"{mode}：英文讲法标签")
    ok("Page 1" in page.inner_text("#pageScript"), f"{mode}：英文页码")
    jump = texts("#jump option")
    ok(bool(jump) and all(o.startswith("Page ") for o in jump),
       f"{mode}：英文页码下拉（实际 {jump[:3]}）")

    # ---- 英文长标签在窄窗口下仍可读可操作 ----
    close_overlays(page)
    page.set_viewport_size({"width": 720, "height": 780})
    for sel, want in (("#addBtn", "Add annotation"), ("#pinBtn", "Annotate on the report"),
                      ("#styleBtn", "Style"), ("#prev", "Previous"), ("#next", "Next")):
        box = page.locator(sel).bounding_box()
        ok(box is not None and box["width"] > 0 and box["height"] > 0
           and page.locator(sel).is_visible(),
           f"{mode}：窄窗口下 {sel} 仍可见可操作（实际 {box}）")
        ok(want in page.locator(sel).inner_text(), f"{mode}：窄窗口下 {sel} 文字仍是 {want}")
    page.set_viewport_size({"width": 1280, "height": 900})

    # ---- 「本页批注」按钮把列表滚进来 ----
    close_overlays(page)
    scrolled = page.evaluate("() => { const s = document.getElementById('script'); s.scrollTop = 0; return s.scrollHeight > s.clientHeight }")
    page.click("#seeList")
    ok(not scrolled or wait_until(lambda: page.evaluate(
        "() => document.getElementById('script').scrollTop > 0"), page, 5),
       f"{mode}：点「本页批注」滚到批注列表（内容是否可滚：{scrolled}）")
    ok("On this page" in page.inner_text("#seeList"), f"{mode}：英文界面的本页批注按钮")

    # ---- 说明正文按语言整段切换 ----
    close_overlays(page)
    page.click("#helpBtn")
    ok(visible("#help.on"), f"{mode}：说明弹窗打开")
    ok(not page.locator('#help [data-ae-lang="en"]').evaluate("e => e.hidden")
       and page.locator('#help [data-ae-lang="zh-CN"]').evaluate("e => e.hidden"),
       f"{mode}：英文界面只显示英文说明")
    # 说明正文在可滚动容器里，inner_text 拿不全，直接断言对应的语言块
    en_body = page.locator('#help [data-ae-lang="en"]').inner_text()
    ok("Shortcuts:" in en_body and "Annotate on the report (P)" in en_body,
       f"{mode}：英文说明正文覆盖快捷键与标注（实际 {en_body[:40]!r}）")
    page.select_option("#localePick", "zh-CN")
    ok("快捷键：" in page.locator('#help [data-ae-lang="zh-CN"]').inner_text()
       and page.locator('#help [data-ae-lang="en"]').evaluate("e => e.hidden"),
       f"{mode}：切回中文只显示中文说明")
    page.select_option("#localePick", "en")
    close_overlays(page)

    # ---- 英文界面不允许残留未替换的占位符 ----
    leftovers = page.evaluate("""() => {
      const bad = [];
      document.querySelectorAll('[title],[aria-label],[placeholder]').forEach(el => {
        for (const v of [el.getAttribute('title'), el.getAttribute('aria-label'), el.getAttribute('placeholder')]) {
          if (v && v.includes('{')) bad.push(el.tagName + '#' + el.id + ' = ' + v);
        }
      });
      return bad;
    }""")
    ok(not leftovers, f"{mode}：英文界面没有残留占位符（实际 {leftovers}）")

    # ---- 存储值与协议说法不随界面语言改写 ----
    page.fill("#ntext", "英文界面写的批注")
    page.select_option("#ntarget", "报告")
    page.click("#addBtn")
    ok(wait_until(lambda: any(n["text"] == "英文界面写的批注" for n in read_notes()), page, 5),
       f"{mode}：英文界面能添加批注")
    added = next((n for n in read_notes() if n["text"] == "英文界面写的批注"), {})
    ok(added.get("target") == "报告",
       f"{mode}：英文界面写批注，对象存储值仍是报告（实际 {added.get('target')!r}）")
    ok(added.get("text") == "英文界面写的批注", f"{mode}：正文按所写保存，不被系统文案顶替")
    ok('value="报告"' in page.inner_html("#ntarget"), f"{mode}：对象下拉的存储值不变")
    aid = added.get("id")
    ok(aid and has(f'#nlist [data-note="{aid}"] .meta', "Report"), f"{mode}：英文界面显示 Report")

    # ---- 确认后删除生效 ----
    dialogs.clear()
    answer[0] = "accept"
    page.locator(f'#nlist [data-note="{aid}"] [data-act="del"]').click()
    ok(wait_until(lambda: all(n["id"] != aid for n in read_notes()), page, 5),
       f"{mode}：确认后删除生效")
    answer[0] = "dismiss"


def close_overlays(page):
    """关掉挡在页头上的弹窗：先把焦点移出输入框，再用 Esc（快捷键本身保持不变）。"""
    page.evaluate("() => { if (document.activeElement) document.activeElement.blur() }")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)


def locale_checks(browser, tmp: Path):
    """最小语言闭环：内嵌词典、0/1/2、切换不写批注，file:// 与服务模式都能加一条普通批注。"""
    d = tmp / "locale_loop"
    d.mkdir()
    build(d, locale="en")
    html = (d / "index.html").read_text(encoding="utf-8")
    ok('function pluralCat(locale, count)' in html and 'REVIEW_LOCALE="en";' in html,
       "英文审阅把词典运行时嵌进单文件")
    url = (d / "index.html").as_uri()

    ctx = browser.new_context()
    page = ctx.new_page()
    seen: list[str] = []
    page.on("request", lambda req: seen.append(req.url))
    page.goto(url)
    ok(page.evaluate("document.documentElement.lang") == "en", "file:// 按审阅默认显示英文")
    ok(page.inner_text("#addLabel") == "Add annotation", "file:// 添加按钮是英文")
    ok(page.inner_text("#targetLabel") == "Which side to change", "file:// 对象名称是英文")
    ok(page.get_attribute("#ntext", "placeholder") == "Write your change request", "file:// 输入提示是英文")
    ok("No annotations yet." in page.inner_text("#nlist"), "file:// 空列表是英文")
    ok(page.locator("#ntarget option").first.inner_text() == "Report", "英文只改对象的显示文字")
    ok(page.locator("#ntarget option").first.get_attribute("value") == "报告", "对象的存储值仍是报告")
    counts = page.evaluate("() => [0, 1, 2].map(n => tMsg('cli.show.count', {count: n}))")
    ok(counts == ["0 annotations", "1 annotation", "2 annotations"], f"file:// 英文计数 0/1/2（实际 {counts}）")
    ok(page.evaluate("tMsg('viewer.page', {page: 3})") == "Page 3", "file:// 命名占位符按译文替换")
    ok(page.evaluate("interpolate('{{count}} 与 {count}', {count: 2})") == "{count} 与 2", "成对花括号保持字面量")
    fallback = page.evaluate("""() => {
      const backup = CATALOGS.en['viewer.add'];
      delete CATALOGS.en['viewer.add'];
      const before = localeMissing().length;
      const text = tMsg('viewer.add');
      CATALOGS.en['viewer.add'] = backup;
      return {text, recorded: localeMissing().slice(before)};
    }""")
    ok(fallback["text"] == "添加批注" and fallback["recorded"] == ["viewer.add"],
       f"缺译回退到另一种语言并留下记录（实际 {fallback}）")
    page.evaluate("setQuote('原句', '报告')")
    en_chip = page.inner_text("#qbox")
    ok("Quote" in en_chip and "Report" in en_chip and "报告" not in en_chip,
       f"英文引用来源用显示名（实际 {en_chip}）")
    page.fill("#ntext", "草稿还在")
    page.select_option("#localePick", "zh-CN")
    ok(page.input_value("#ntext") == "草稿还在", "file:// 切换语言保留正在写的草稿")
    ok(page.inner_text("#addLabel") == "添加批注", "file:// 切换后按钮回到中文")
    ok(page.inner_text("#targetLabel") == "对象", "file:// 切换后对象名称仍是「对象」")
    ok((page.get_attribute("#ntext", "placeholder") or "").startswith("写修改意见（C）"),
       "file:// 中文输入提示保持原来的长句")
    zh_chip = page.inner_text("#qbox")
    ok("引用（报告）" in zh_chip and "原句" in zh_chip, f"中文引用来源保持原文格式（实际 {zh_chip}）")
    zh_counts = page.evaluate("() => [0, 1, 2].map(n => tMsg('cli.show.count', {count: n}))")
    ok(zh_counts == ["0 条批注", "1 条批注", "2 条批注"], f"file:// 中文计数 0/1/2（实际 {zh_counts}）")
    page.fill("#ntext", "请改标题")
    page.select_option("#ntarget", "报告")
    page.click("#addBtn")
    page.wait_for_timeout(200)
    notes = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(len(notes) == 1 and notes[0]["text"] == "请改标题" and notes[0]["target"] == "报告"
       and notes[0]["quote"] == "原句" and notes[0]["quoteFrom"] == "报告",
       "file:// 批注原文、引用和对象值不随语言改写")
    nid, rid, key = notes[0]["id"], page.evaluate("RID"), page.evaluate("P[0].key")
    zh_quote = page.locator("#nlist blockquote").inner_text()
    ok(zh_quote.startswith("报告：") and "原句" in zh_quote, f"中文批注引用保持「报告：」（实际 {zh_quote}）")
    ok("请改标题" in page.inner_text("#nlist") and "报告" in page.inner_text("#nlist"), "中文列表显示原文和对象")
    page.select_option("#localePick", "en")
    en_quote = page.locator("#nlist blockquote").inner_text()
    ok(en_quote.startswith("Report:") and "原句" in en_quote and "报告：" not in en_quote,
       f"英文批注引用改显示名、原文不变（实际 {en_quote}）")
    ok("Report" in page.inner_text("#nlist") and "请改标题" in page.inner_text("#nlist"), "切回英文只改对象显示")
    again = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(again[0]["id"] == nid and again[0]["text"] == "请改标题" and page.evaluate("RID") == rid
       and page.evaluate("P[0].key") == key, "file:// 切换语言不改批注 ID、审阅标识和页面键")
    ok(page.evaluate("localStorage.getItem('ae-locale:' + RID)") == "en", "页面把语言选择存在本机")
    ok(not any(u.endswith(".json") or "/locales/" in u for u in seen), "file:// 不请求词典文件")
    page.evaluate("localStorage.setItem('ae-locale:' + RID, 'fr')")
    page.reload()
    ok(page.evaluate("document.documentElement.lang") == "en", "本机存了无法识别的语言时沿用审阅默认")
    page.evaluate("localStorage.setItem('ae-locale:' + RID, 'zh-CN')")
    page.reload()
    ok(page.evaluate("document.documentElement.lang") == "zh-CN" and page.inner_text("#addLabel") == "添加批注",
       "本机保存的语言优先于审阅默认英文")
    locale_edit_checks(page, "file://", lambda: page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))"))
    locale_editing_checks(page, "file://", lambda: page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))"))
    ctx.close()

    auto = tmp / "locale_auto"
    auto.mkdir()
    build(auto, locale="auto")
    auto_url = (auto / "index.html").as_uri()
    for browser_locale, expect_lang, label in (("en-US", "en", "Add annotation"),
                                                ("zh-CN", "zh-CN", "添加批注"),
                                                ("fr-FR", "en", "Add annotation")):
        actx = browser.new_context(locale=browser_locale)
        ap = actx.new_page()
        ap.goto(auto_url)
        langs = ap.evaluate("navigator.languages")
        ok(ap.evaluate("document.documentElement.lang") == expect_lang and ap.inner_text("#addLabel") == label,
           f"auto + {browser_locale} → {expect_lang}（languages={langs}）")
        actx.close()

    port = free_port()
    server = start_server(d, port)
    try:
        sctx = browser.new_context()
        sp = sctx.new_page()
        net: list[tuple[str, str]] = []
        sp.on("request", lambda req: net.append((req.method, req.url)))
        sp.goto(f"http://127.0.0.1:{port}/")
        sp.wait_for_function("document.getElementById('mode').classList.contains('server')")
        ok(sp.inner_text("#addLabel") == "Add annotation", "服务模式沿用审阅默认英文")
        sp.fill("#ntext", "服务里的批注")
        sp.select_option("#ntarget", "讲稿")
        sp.click("#addBtn")
        ok(wait_until(lambda: len(disk_notes(d)) == 1, sp, 5), "服务模式批注落盘")
        stored = disk_notes(d)[0]
        posts = sum(1 for method, _url in net if method == "POST")
        info = json.loads((d / ".annotation-server.json").read_text(encoding="utf-8"))
        rid = sp.evaluate("RID")
        key = sp.evaluate("P[0].key")
        sp.fill("#ntext", "草稿还在")
        sp.select_option("#localePick", "zh-CN")
        sp.wait_for_timeout(400)
        kept = disk_notes(d)
        manifest = json.loads((d / "review.json").read_text(encoding="utf-8"))
        ok(sp.input_value("#ntext") == "草稿还在", "服务模式切换语言保留草稿")
        ok(len(kept) == 1 and kept[0]["id"] == stored["id"] and kept[0]["text"] == "服务里的批注"
           and kept[0]["target"] == "讲稿", "切换语言不改已落盘的批注")
        ok(sum(1 for method, _url in net if method == "POST") == posts, "切换语言没有新的写请求")
        ok(manifest["locale"] == "en" and manifest["reviewId"] == rid and sp.evaluate("P[0].key") == key
           and info.get("port") == port, "切换语言不改审阅标识、页面键、默认语言和端口")
        ok(not any("/locales/" in url or url.endswith("en.json") or url.endswith("zh-CN.json")
                   for _method, url in net), "服务模式不请求词典文件")
        locale_edit_checks(sp, "服务模式", lambda: disk_notes(d))
        locale_editing_checks(sp, "服务模式", lambda: disk_notes(d))
        sctx.close()
    finally:
        stop(server)


E2E_NOTES = [
    {"id": "e2enote000000001", "page": 3, "title": "结论：三季度收入同比增长 12%", "target": "报告",
     "text": "把 +12% 改成「同比 +12%」\n第二行也要改", "quote": "+12%", "quoteFrom": "报告",
     "pin": {"x": 38.8, "y": 59.8}, "status": "open", "at": "2026-10-27T10:19:14Z"},
    {"id": "e2enote000000002", "page": 7, "title": "成本", "target": "讲稿",
     "text": "改为：删掉整段", "quote": "原句甲乙", "quoteFrom": "讲稿", "status": "drafted",
     "at": "2026-10-27T11:00:00Z", "resolution": "已缩短", "region": {"x": 5, "y": 20, "w": 60, "h": 30},
     "style": {"scope": "region"}, "anchor": "table「渠道」", "skills": ["humanizer-zh", "dataviz"],
     "pair": {"side": "报告", "match": "exact", "text": "对应句子"},
     "edit": {"op": "replace", "from": "原句甲乙", "to": "删掉整段"}},
]


def write_disk_notes(d: Path, notes: list[dict]) -> None:
    """往审阅目录的 notes.json 写批注（走 notes_store，和页面经服务落盘一致）。"""
    code = ("import sys,json;sys.path.insert(0,sys.argv[1]);import notes_store as s;from pathlib import Path;"
            "d=Path(sys.argv[2]);"
            "[s.add(d, n) for n in json.loads(sys.argv[3])]")
    subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(d), json.dumps(notes, ensure_ascii=False)],
                   check=True, capture_output=True)


def notes_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), *args], capture_output=True, text=True)


def export_import_checks(browser, tmp: Path):
    """#35：双语导出与跨语言导入。页面导出→CLI、CLI 导出→页面、两种语言各自与交叉往返、旧格式不退步。"""
    d = tmp / "export_import"
    build(d, locale="en")
    url = (d / "index.html").as_uri()
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(url)
    page.evaluate("v => localStorage.setItem('ae.' + RID + '.notes', JSON.stringify(v))", E2E_NOTES)
    page.reload()
    page.wait_for_timeout(400)

    def open_import():
        page.keyboard.press("Escape")     # 先收起可能开着的弹窗（焦点也可能在输入框里）
        page.keyboard.press("n")
        page.click("#openImp")

    switched = page.evaluate("""() => [
      cleanItem({page:1,text:REP_PREFIX+'新句',edit:{op:'delete',from:'原句'}}),
      cleanItem({page:1,text:DEL_TEXT,edit:{op:'replace',from:'原句',to:'旧句'}})
    ].map(n => n.edit)""")
    ok(switched == [{"op": "replace", "from": "原句", "to": "新句"},
                    {"op": "delete", "from": "原句"}],
       "JSON 可见正文能双向切换删除和改写，隐藏结构不能恢复旧操作")

    # 界面是英文：默认导出英文，可显式选中文
    page.click("#notesBtn")
    ok(page.evaluate("document.getElementById('expLang').value") == "auto", "导出语言默认「跟随界面」")
    md_en = page.evaluate("exportText()")
    ok(md_en.splitlines()[0] == "<!-- annotation-everything export v1 lang=en -->",
       "英文界面默认导出英文，第一行是格式标记")
    ok("# Annotations · " in md_en, "英文导出用英文包装文字")
    ok("[Report · Pin 1 (x 38.8%, y 59.8%)]" in md_en, "英文导出的标注标签是英文")
    ok("[Script · Region 1 (x 5%, y 20%, w 60%, h 30%)] [Drafted] 改为：删掉整段" in md_en,
       "英文导出保留中文存储值与用户正文原文")
    ok("   > Quote (Script): 原句甲乙" in md_en and "   Draft note: 已缩短" in md_en
       and "   Style: Region" in md_en and "   Position hint: table「渠道」" in md_en
       and "   Skills: humanizer-zh, dataviz" in md_en
       and "   ↔ Other side (Report · identical): 对应句子" in md_en,
       "英文导出的引用、说明、风格、线索、技能、对应句都是英文标签配中文原文")
    name = page.evaluate("expName('md')")
    ok(name.startswith("annotations_") and name.endswith(".md")
       and len(name[:-3].rsplit("_", 1)[-1]) == 12
       and page.evaluate("NS") in name,
       f"英文导出的文件名用英文前缀，保留审阅识别与 12 位机器时间（{name}）")
    page.select_option("#expLang", "zh-CN")
    md_zh = page.evaluate("exportText()")
    ok(md_zh.splitlines()[0] == "<!-- annotation-everything export v1 lang=zh-CN -->",
       "英文界面可显式选中文导出")
    ok("# 批注 · " in md_zh and "【已初改·待复核】改为：删掉整段" in md_zh
       and "   风格优化：框选处" in md_zh and "   技能：humanizer-zh、dataviz" in md_zh,
       "中文导出与改动前一致")

    # 常驻技能管理和双语导出组合后，所有范围仍能从实际导出正文往返。
    standing = [{"name": f"sample-{i}", "scope": scope}
                for i, scope in enumerate(("all", "报告", "讲稿", "style"))]
    standing_roundtrips = page.evaluate("""items => {
      const saved = alwaysSkills, choice = $("expLang").value;
      try {
        alwaysSkills = items;
        return ["zh-CN", "en"].map(lang => {
          $("expLang").value = lang;
          const text = exportText();
          return {lang, text, parsed: importAlways(text)};
        });
      } finally { alwaysSkills = saved; $("expLang").value = choice; }
    }""", standing)
    for result in standing_roundtrips:
        ok(result["parsed"] == standing,
           f"{result['lang']} 导出的常驻技能保留全部范围与调用名，跨语言导入可还原")
    legacy_standing = standing_roundtrips[0]["text"].split("\n", 1)[1]
    ok(page.evaluate("text => importAlways(text)", legacy_standing) == standing,
       "没有格式标记的旧中文常驻技能仍可导入")

    # 解析按格式标记选文法，与导入界面 locale 无关：两种导出在中文界面下都读得对
    page.select_option("#localePick", "zh-CN")
    page.wait_for_timeout(150)
    got_en = page.evaluate("t => parseImport(t)", md_en)
    got_zh = page.evaluate("t => parseImport(t)", md_zh)
    ok(got_en == got_zh, "同一批注的中英文导出，在中文界面下解析结果一致")
    ok([n["target"] for n in got_en] == ["报告", "讲稿"]
       and [n["status"] for n in got_en] == ["open", "drafted"]
       and got_en[1]["style"] == {"scope": "region"}
       and got_en[1]["edit"] == {"op": "replace", "from": "原句甲乙", "to": "删掉整段"}
       and got_en[1]["pair"] == {"side": "报告", "match": "exact", "text": "对应句子"}
       and got_en[1]["skills"] == ["humanizer-zh", "dataviz"]
       and got_en[0]["text"] == "把 +12% 改成「同比 +12%」\n第二行也要改",
       "英文导出在中文界面导入后还原出中文存储值、结构化字段与多行正文")

    # 中文界面导入英文导出 → 按中文协议值落库
    open_import()
    page.fill("#impText", md_en)
    page.click("#doImp")
    page.wait_for_timeout(700)
    landed = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(len(landed) == 2 and [n["target"] for n in landed] == ["报告", "讲稿"]
       and landed[1]["status"] == "drafted", "中文界面导入英文导出：批注按中文协议值落库")

    # 页面导出 → CLI 导入（导出的语言与界面不同）
    md_from_page = page.evaluate("exportText()")
    f = tmp / "from_page.md"
    f.write_text(md_from_page, encoding="utf-8")
    cli_dir = tmp / "cli_review"
    build(cli_dir)
    write_disk_notes(cli_dir, E2E_NOTES[:1])
    r = notes_cli("import", str(cli_dir), str(f))
    ok(r.returncode == 0 and "新增 1 条" in r.stdout and "跳过重复 1 条" in r.stdout,
       f"页面导出交给 CLI 导入成功，内容相同的按既有规则跳过（{r.stdout.strip()[:46]}）")
    ok(all(n["target"] in ("报告", "讲稿", "报告和讲稿") for n in disk_notes(cli_dir)),
       "CLI 落库的协议值仍是中文")

    # CLI 导出（另一种语言）→ 页面导入
    r = notes_cli("export", str(cli_dir), "--lang", "en")
    ok(r.returncode == 0 and r.stdout.splitlines()[0] == "<!-- annotation-everything export v1 lang=en -->",
       "CLI 可显式导出英文")
    page.evaluate("v => localStorage.setItem('ae.' + RID + '.notes', '[]')")
    page.reload()
    page.wait_for_timeout(300)
    open_import()
    page.fill("#impText", r.stdout)
    page.click("#doImp")
    page.wait_for_timeout(700)
    back = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(len(back) == 2 and [n["target"] for n in back] == ["报告", "讲稿"]
       and back[1]["status"] == "drafted", "CLI 的英文导出能在中文页面里导入，状态与协议值都对")

    # 旧中文格式不退步；认不出的文字有明确规则
    legacy = ("## 第 4 页｜收入\n1. 【报告和讲稿】删除引用的这段文字\n   > 引用（报告）：甲乙丙\n"
              "   ↔ 另一侧（讲稿·相近）：丁戊己\n"
              "2. 【讲稿】讲法太绕\n"
              "   技能：humanizer-zh（常驻·讲稿）、dataviz（这条指定）——处理这条前先调用这些技能\n")
    got_legacy = page.evaluate("t => parseImport(t)", legacy)
    ok(got_legacy and got_legacy[0]["edit"] == {"op": "delete", "from": "甲乙丙"}
       and got_legacy[0]["pair"]["side"] == "讲稿", "没有标记的旧中文导出仍按中文文法读")
    ok(got_legacy[1]["skills"] == ["dataviz"],
       "技能行里的来源标签与命令行一致地剥掉，常驻技能不写进单条")
    page.evaluate("v => localStorage.setItem('ae.' + RID + '.notes', '[]')")
    page.reload()
    page.wait_for_timeout(300)
    open_import()
    page.fill("#impText", legacy)
    page.click("#doImp")
    page.wait_for_timeout(700)
    ok(len(page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")) == 2, "旧中文格式能导进页面")
    page.fill("#impText", "完全不是批注导出的文字")
    page.click("#doImp")
    page.wait_for_timeout(400)
    ok("无法识别" in page.inner_text("#toast"), f"认不出的文字给出明确提示（{page.inner_text('#toast')[:30]}）")
    page.fill("#impText", "<!-- annotation-everything export v9 lang=en -->\n")
    page.click("#doImp")
    page.wait_for_timeout(400)
    ok("v9" in page.inner_text("#toast"), "不认识的格式版本也给出明确提示")
    ctx.close()


def english_script_checks(browser, tmp: Path):
    """#34：英文讲稿在两种界面语言下都能逐页浏览；缺页时按宽容模式显示空讲稿。"""
    d = tmp / "english_script"
    d.mkdir()
    build(d, script=FIXTURE / "script.en.md", locale="en")
    url = (d / "index.html").as_uri()
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(url)
    go_page(page, 3)
    script = page.inner_text("#pageScript")
    ok("Talking points" in script and "Transition" in script and "Likely questions" in script,
       f"英文讲稿用英文栏目标签（实际 {script[:40]!r}）")
    ok("Conclusion: Q3 revenue up 12% year on year" in script, "英文讲稿的标题按原文显示")
    ok("Revenue grew 12% year on year" in script and "Same-store growth was 4%" in script, "英文讲稿正文按原文显示")
    ok("1.5 min" in script, "英文时长按原文显示")
    ok("In the revenue mix, delivery keeps taking share" in script, "英文过渡按原文显示")
    ok("About two thirds of the increase" in script, "英文问答按原文显示")
    # 栏目条里的「开篇 结论与收入」是报告目录推出来的板块名（报告是中文的），不属于讲稿翻译
    ok("三季度收入同比增长 12%" not in script and "主要来自新开的 3 家门店" not in script,
       "英文讲稿不被换成中文讲稿的内容")
    page.select_option("#localePick", "zh-CN")
    go_page(page, 4)   # 栏目条在翻页时按当前语言重画
    go_page(page, 3)
    zh_script = page.inner_text("#pageScript")
    ok("讲法" in zh_script and "过渡" in zh_script and "可能被问" in zh_script,
       f"切到中文界面只换栏目标签（实际 {zh_script[:60]!r}）")
    ok("Revenue grew 12% year on year" in zh_script and "1.5 min" in zh_script,
       "切到中文界面讲稿正文和时长仍是英文原文")
    ok("Conclusion: Q3 revenue up 12% year on year" in zh_script, "切到中文界面标题仍是英文原文")
    page.select_option("#localePick", "en")

    # 缺页：--lenient 以报告页序为准，缺讲稿的那一页显示空讲稿，其余页不受影响
    missing = tmp / "script-missing.md"
    missing.write_text((FIXTURE / "script.en.md").read_text(encoding="utf-8").split("### Page 6")[0],
                       encoding="utf-8")
    md = tmp / "english_missing"
    md.mkdir()
    build(md, script=missing, locale="en", name="en-missing", lenient=True)
    mpage = ctx.new_page()
    mpage.goto((md / "index.html").as_uri())
    go_page(mpage, 5)
    ok("Green coffee is up 9%" in mpage.inner_text("#pageScript"), "缺页不影响前面各页的讲稿")
    go_page(mpage, 6)
    ok("(No speaker notes on this page)" in mpage.inner_text("#pageScript"),
       "缺讲稿的那一页显示空讲稿，不报错")
    mpage.close()
    ctx.close()


def handoff_i18n_checks(browser, tmp: Path):
    """#32：提交、复核与处理记录在中英两种语言下同一含义；切语言不重新提交、不改选项。"""
    d = tmp / "handoff_i18n"
    d.mkdir()
    build(d, locale="en", name="handoff-i18n")
    port = free_port()
    server = start_server(d, port)
    ctx = browser.new_context()
    net: list[tuple[str, str]] = []
    try:
        page = ctx.new_page()
        page.on("request", lambda req: net.append((req.method, req.url)))
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function("document.getElementById('mode').classList.contains('server')")
        add_note(page, "把口径改成同比")
        go_page(page, 4)
        add_note(page, "表格加上单位")
        ok(page.inner_text("#mode") == "● Connected to the local service",
           f"英文服务态徽标（实际 {page.inner_text('#mode')!r}）")
        ok(page.locator("#handBtn span").first.inner_text() == "Hand to the agent", "英文提交按钮")
        page.click("#handBtn")
        ok(page.inner_text("#handTitle") == "Hand to the agent", "英文提交弹窗标题")
        ok(page.inner_text("#doHand").split("⌘")[0] == "Hand to the agent", "英文提交按钮（弹窗内）")
        labels = [page.locator("#hTargetRow label").nth(k).inner_text() for k in range(2)]
        ok(labels == ["be finalized", "stop at drafted, waiting for review"],
           f"英文「这批做到」两个选项（实际 {labels}）")
        ok(page.inner_text("#hSubRow") == "Allow subagents", "英文子代理选项")
        ok(page.inner_text("#hFullRow") == "Full check (consistency across the whole material)", "英文全量勾稽选项")
        ok(page.get_attribute("#handMsg", "placeholder", ) ==
           "A note for the agent (optional): for example “start with pages 7–12, leave the numbers alone”",
           "英文附言提示")
        ok("Once you submit" in page.inner_text("#handHelp"), "英文提交说明")
        ok(page.locator("#handHist span").first.inner_text() == "Processing history", "英文处理记录入口")

        # 结构化选项与显示语言无关：提交后落盘的字段是同一套
        page.check('input[name="htarget"][value="review"]')
        page.check("#hSub")
        page.check("#hFullCheck")
        page.fill("#handMsg", "先改第 2 页")
        open_lines = page.inner_text("#handInfo")
        ok("open annotations, on pages" in open_lines, f"英文批次范围（实际 {open_lines!r}）")
        posts = sum(1 for method, _url in net if method == "POST")
        page.click("#doHand")
        ok(wait_until(lambda: (d / "handoff.json").exists(), page, 5), "提交写进 handoff.json")
        item = json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"][-1]
        ok((item["target"], item["allowSubagents"], item["fullCheck"], item["userMessage"]) ==
           ("review", True, True, "先改第 2 页"), "结构化选项和原始附言不随语言改写")
        ok(item["message"].count("全量勾稽") == 1, "全量勾稽的固定兼容提示保持原文")
        ok(wait_until(lambda: "batch 1 waiting to be claimed" in page.inner_text("#handState"), page, 5),
           f"英文提交状态徽标（实际 {page.inner_text('#handState')!r}）")
        ok("Note: 先改第 2 页" in (page.get_attribute("#handState", "title") or ""), "徽标提示里附言保持原文")

        received = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(d),
                                   "--timeout", "5", "--locale", "en"], capture_output=True, text=True, timeout=20)
        out = received.stdout
        ok(received.returncode == 0 and "# Handed to the agent: batch 1" in out, "等待中的 Agent 收到英文提交")
        ok("Note from the user: 先改第 2 页" in out, "英文输出里原始附言保持原文")
        ok("Stop at “drafted, waiting for review” and wait for the user" in out, "英文说明停在待复核")
        ok("you may dispatch subagents" in out, "英文说明子代理权限")
        ok("full check: go through the whole report and the speaker notes" in out
           and "evidence" in out and "uncertain or you did not check" in out, "英文全量勾稽保留全部约束")
        ok("At submit time 2 annotations were open, on pages 1, 4." in out, "英文说明批次范围")
        ok(wait_until(lambda: "Agent working · batch 1" in page.inner_text("#handState"), page, 8),
           f"接收后英文徽标（实际 {page.inner_text('#handState')!r}）")

        page.keyboard.press("j")
        page.wait_for_timeout(600)
        history = page.inner_text("#batchList") + page.inner_text("#batchSum")
        posts_after_submit = sum(1 for method, _url in net if method == "POST")
        ok("Batch 1" in history and "in progress" in history, f"英文处理记录（实际 {history[:60]!r}）")
        ok("Asks for: stop at drafted, waiting for review, subagents allowed, full check" in history,
           "英文处理记录里的提交选项")
        ok("Note: 先改第 2 页" in history, "英文处理记录里附言保持原文")
        ok(("Draft · " in history or "Handling · " in history) and "claimed" in history,
           f"英文阶段轨迹（实际 {history!r}）")
        page.keyboard.press("Escape")

        # 切到中文：只改显示，不重新提交、不改选项、不请求新接口
        before = json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"]
        page.click("#handBtn")
        page.select_option("#localePick", "zh-CN")
        page.wait_for_timeout(500)
        ok(len(json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"]) == len(before),
           "切换语言不重新提交")
        ok(sum(1 for method, _url in net if method == "POST") == posts_after_submit,
           "切换语言没有新的写请求")
        ok(page.is_checked("#hFullCheck") and page.is_checked("#hSub")
           and page.is_checked('input[name="htarget"][value="review"]'), "切换语言不改变已选选项")
        ok(page.inner_text("#handTitle") == "交给 Agent", "切换后弹窗标题回到中文")
        ok(page.inner_text("#hFullRow") == "全量勾稽（检查全文前后一致性）", "切换后全量勾稽选项回到中文")
        ok(page.inner_text("#mode") == "● 已连接本地服务", "切换后服务态徽标回到中文")
        ok("第 1 批" in page.inner_text("#handState"), "切换后提交状态徽标回到中文")
        ok("还没交给 Agent" not in page.inner_text("#resume") and "第 1 批" in page.inner_text("#resume"),
           f"切换后恢复提示回到中文（实际 {page.inner_text('#resume')!r}）")
        # 关掉弹窗再切语言：提交按钮是常驻元素，文字也要跟上（不能留着上一种语言）
        page.keyboard.press("Escape")
        for pick, want in (("en", "Hand to the agent"), ("zh-CN", "交给 Agent")):
            page.select_option("#localePick", pick)
            page.wait_for_timeout(400)
            ok(page.text_content("#doHandLabel") == want,
               f"关掉弹窗后切到 {pick}，提交按钮文字是「{want}」（实际 {page.text_content('#doHandLabel')!r}）")

        # 中文下把这一批走到「已初改·待复核」：状态在两种语言下都核对得上
        note_id = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(d), "--json"],
                                 capture_output=True, text=True, timeout=20).stdout
        first = json.loads(note_id)["notes"][0]["id"]
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(d), first,
                        "--note", "改成同比", "--seq", "1"], capture_output=True, text=True, timeout=20, check=True)
        done = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(d), "--seq", "1",
                               "--summary", "初改 1 条", "--locale", "zh-CN"],
                              capture_output=True, text=True, timeout=20)
        ok("初改完成" in done.stdout and "按用户要求停在这里" in done.stdout,
           f"中文收尾走到待复核（rc={done.returncode} out={done.stdout[:120]!r} err={done.stderr[:200]!r}）")
        ok(wait_until(lambda: "待复核" in page.inner_text("#handState"), page, 8),
           f"中文待复核徽标（实际 {page.inner_text('#handState')!r}）")
        page.select_option("#localePick", "en")
        ok(wait_until(lambda: "awaiting review" in page.inner_text("#handState"), page, 8),
           f"英文待复核徽标（实际 {page.inner_text('#handState')!r}）")
        page.click("#handBtn")
        ok("Batch 1 is drafted · awaiting review (1)" in page.inner_text("#handDrafts")
           and "Ask the agent to review" in page.inner_text("#handDrafts"),
           f"英文待复核批次行（实际 {page.inner_text('#handDrafts')!r}）")
        page.click('#handDrafts [data-hreview="1"]')
        ok("Batch 1 has 1 annotation drafted and waiting for review" in page.inner_text("#handInfo"),
           f"英文复核说明（实际 {page.inner_text('#handInfo')!r}）")
        ok(page.inner_text("#handTitle") == "Ask the agent to review batch 1", "英文请求复核标题")
        ok(page.inner_text("#doHand").split("⌘")[0] == "Ask for the review", "英文请求复核按钮")
        page.keyboard.press("Escape")

        # 复核定稿：跳过复核直接定稿，确认框与结果状态都用当前语言
        page.click("#handBtn")
        confirms: list[str] = []
        page.on("dialog", lambda dlg: (confirms.append(dlg.message), dlg.accept()))
        page.click('#handDrafts [data-hfinal="1"]')
        ok(confirms == ["Skip the review and mark the 1 drafts in batch 1 as resolved?"],
           f"英文直接定稿的确认框（实际 {confirms}）")
        ok(wait_until(lambda: "Agent finished · batch 1" in page.text_content("#handState"), page, 8),
           f"复核定稿后英文徽标（实际 {page.text_content('#handState')!r}）")
        page.keyboard.press("Escape")

        # 断线提交、待补传与重连重试：英文说清状态与恢复方式，不把未确认的提交说成已保存
        items = json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"]
        ctx.set_offline(True)
        page.click("#handBtn")
        page.fill("#handMsg", "断线时补一条")
        page.click("#doHand")
        ok(wait_until(lambda: page.text_content("#handState") == "Hand to the agent (held for sending)", page, 6),
           f"断线提交英文说清还在暂存（实际 {page.text_content('#handState')!r}）")
        ok("held for sending" in page.inner_text("#mode"), f"待补传英文计数（实际 {page.inner_text('#mode')!r}）")
        ok(len(json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"]) == len(items),
           "未确认的提交不写进 handoff.json")
        ok(wait_until(lambda: "Nothing is running on port" in page.inner_text("#srvBar")
                      and "serve.py" in page.inner_text("#srvBar"), page, 10),
           f"断线时英文提示条给出恢复方式（实际 {page.inner_text('#srvBar')[:80]!r}）")
        page.keyboard.press("Escape")
        ctx.set_offline(False)
        ok(wait_until(lambda: len(json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"])
                      == len(items) + 1, page, 10), "重连后暂存的提交自动补传")
        ok(wait_until(lambda: "Held changes have been sent" in (page.inner_text("#toast") or ""), page, 10),
           f"重连后英文提示（实际 {page.inner_text('#toast')!r}）")

        # 服务不匹配：英文说清端口上现在是谁、改动没有写进别人的审阅
        def wrong_review(route):
            response = route.fetch()
            body = response.json()
            body["reviewId"] = "0123456789abcdef"
            route.fulfill(response=response, json=body)

        page.route("**/api/notes", wrong_review)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        ok(wait_until(lambda: "Wrong service" in page.inner_text("#mode"), page, 8),
           f"服务不匹配英文徽标（实际 {page.inner_text('#mode')!r}）")
        ok("not this review's service" in (page.get_attribute("#mode", "title") or ""),
           "服务不匹配英文提示说清改动没有写进别人的审阅")
        ok("has some other program on it" in page.inner_text("#srvBar")
           and "serve.py" in page.inner_text("#srvBar"),
           f"不匹配时英文提示条说清端口上是谁、怎么恢复（实际 {page.inner_text('#srvBar')[:90]!r}）")
        page.unroute("**/api/notes", wrong_review)

        ctx.close()

        # 旧服务：选项不被静默丢掉，提示用当前语言说清怎么恢复
        old = browser.new_context()
        opage = old.new_page()

        def no_full_check(route):
            response = route.fetch()
            body = response.json()
            body["features"] = [f for f in body.get("features", []) if f not in ("full-check", "marks")]
            route.fulfill(response=response, json=body)

        opage.route("**/api/notes", no_full_check)
        opage.goto(f"http://127.0.0.1:{port}/")
        opage.wait_for_timeout(1200)
        ok("Old service" in opage.inner_text("#mode"), f"旧服务英文徽标（实际 {opage.inner_text('#mode')!r}）")
        ok("does not know about area marks" in (opage.get_attribute("#mode", "title") or ""), "旧服务英文提示")
        opage.click("#handBtn")
        ok(opage.is_visible("#hFullOld") and "does not support the full check" in opage.inner_text("#hFullOld"),
           "旧服务英文提示说清怎么恢复")
        opage.keyboard.press("Escape")
        opage.unroute("**/api/notes", no_full_check)
        old.close()
        ctx.close()
    finally:
        ctx.close()
        stop(server)


def installed_delivery_checks(browser, tmp: Path):
    """#36：从唯一的临时安装副本完成三种语言组合的真实审阅流程。

    AE_DELIVERY_ARTIFACTS 可保留合成演示的命令、源文件、导出与截图。
    这是确定性的产品流程演示，不冒充实际模型的发现率或改稿质量评测。
    """
    installed = tmp / "installed-skills" / "annotation-everything"
    env = {**os.environ, "CLAUDE_SKILLS_DIR": str(installed.parent)}
    subprocess.run([sys.executable, str(ROOT / "ae.py"), "install", "--locale", "en"],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    scripts = installed / "scripts"
    artifacts = Path(os.environ["AE_DELIVERY_ARTIFACTS"]) if os.environ.get("AE_DELIVERY_ARTIFACTS") else None
    evidence = {"python": platform.python_version(), "platform": platform.platform(),
                "browser": browser.version, "scenarios": []}
    for label, ui, material in (("english", "en", "en"), ("chinese", "zh-CN", "zh-CN"),
                                ("mixed", "en", "zh-CN")):
        case = tmp / ("installed-" + label)
        case.mkdir()
        source_report = FIXTURE / ("report.en.html" if material == "en" else "report.html")
        source_script = FIXTURE / ("script.en.md" if material == "en" else "script.md")
        report, script = case / "report.html", case / "script.md"
        shutil.copyfile(source_report, report)
        shutil.copyfile(source_script, script)
        review = case / "sample-review"
        transcript = []
        start_result = len(RESULTS)
        output = artifacts / label if artifacts else None
        if output:
            output.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report, output / "report.before.html")
            shutil.copyfile(script, output / "script.before.md")

        def cli(tool, *args):
            result = subprocess.run([sys.executable, str(scripts / tool), *map(str, args)],
                                    env=env, capture_output=True, text=True, timeout=30)
            transcript.append({"tool": tool, "args": list(map(str, args)), "exit": result.returncode,
                               "stdout": result.stdout, "stderr": result.stderr})
            if result.returncode:
                raise RuntimeError(f"installed {tool} failed: {result.stderr or result.stdout}")
            return result.stdout

        def check(condition, text):
            ok(condition, f"安装副本 {label}：{text}")

        cli("build_viewer.py", "--report", report, "--script", script, "--out", review,
            "--locale", ui, "--name", "Sample Coffee", "--no-scan-skills")
        rid = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
        port = free_port()
        server = start_server(review, port, scripts=scripts)
        ctx = browser.new_context(locale=ui, viewport={"width": 1440, "height": 1000})
        try:
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_function("document.getElementById('mode').classList.contains('server')")
            check(page.get_attribute("html", "lang") == ui, "页面语言来自生成参数")
            if ui == "en":
                check(page.inner_text("#notesBtn").startswith("Annotations"), "英文批注入口没有遗漏翻译")
                page.click("#notesBtn")
                page.click("#openImp")
                check(page.inner_text("#imp h2") == "Import annotations"
                      and page.inner_text("#doImp") == "Import", "导入弹窗与操作按钮使用英文")
                page.fill("#impText", "Unsubmitted import draft: 原文 <tag> {count}")
                page.locator("#imp [data-close]").click()
                page.locator("#allnotes [data-close]").click()
                page.select_option("#localePick", "zh-CN")
                page.select_option("#localePick", "en")
                page.click("#notesBtn")
                page.click("#openImp")
                check(page.input_value("#impText") == "Unsubmitted import draft: 原文 <tag> {count}",
                      "弹窗内切换语言保留导入草稿")
                page.locator("#imp [data-close]").click()
                page.locator("#allnotes [data-close]").click()
            go_page(page, 3)
            annotation = ('Change +12% to YoY +12% in the report; make the same wording change in the speaker notes.'
                          if material == "en" else '报告的 +12% 改成「同比 +12%」，讲稿同步补上「同比」。')
            page.select_option("#ntarget", "报告和讲稿")
            add_note(page, annotation, pin=(0.55, 0.35))
            check(wait_until(lambda: len(disk_notes(review)) == 1, page, 5), "浏览器批注自动落盘")
            received_notes = json.loads(cli("notes.py", "show", review, "--json"))["notes"]
            note_id = received_notes[0]["id"]
            check(received_notes[0]["text"] == annotation and received_notes[0]["target"] == "报告和讲稿",
                  "原文与 v1 对象值经安装后的 CLI 保持不变")
            if label == "english":
                page.locator('#nlist [data-act="edit"]').first.click()
                page.fill("#etext", "X" * 20001)
                page.locator('#nlist [data-act="save"]').first.click()
                check(wait_until(lambda: "bad-note" in page.inner_text("#toast"), page, 5)
                      and "Changes remain in the browser" not in page.inner_text("#toast")
                      and "Check the input and try again" in page.inner_text("#toast"),
                      "不可重试的字段拒绝提示核对输入，不虚称改动已保留")
            if output:
                page.locator("#nlist").scroll_into_view_if_needed()
                page.screenshot(path=str(output / "viewer.png"))
                page.keyboard.press("n")
                page.screenshot(path=str(output / "notes-export.png"))
                page.keyboard.press("Escape")

            # wait 必须在提交前启动，证明浏览器能唤醒安装副本中的接收命令。
            waiter = subprocess.Popen([sys.executable, str(scripts / "notes.py"), "wait", str(review),
                                       "--timeout", "20", "--locale", ui], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            CLEANUP.add(waiter)
            page.click("#handBtn")
            page.check('input[name="htarget"][value="review"]')
            page.uncheck("#hSub")
            page.check("#hFullCheck")
            message = ("Apply my annotations. Stop after the initial edit. Do not use subagents."
                       if ui == "en" else "按批注改稿，初改完成后停在待复核，不派发子代理。")
            page.fill("#handMsg", message)
            page.click("#doHand")
            out, err = waiter.communicate(timeout=25)
            CLEANUP.discard(waiter)
            transcript.append({"tool": "notes.py wait", "exit": waiter.returncode, "stdout": out, "stderr": err})
            check(waiter.returncode == 0 and annotation in out, "实际提交唤醒等待命令并交付原始批注")
            batch = json.loads((review / "handoff.json").read_text(encoding="utf-8"))["items"][-1]
            check((batch["status"], batch["target"], batch["allowSubagents"], batch["fullCheck"], batch["userMessage"]) ==
                  ("claimed", "review", False, True, message), "接受的批次保留停止条件、主代理权限、全量勾稽和原始附言")
            if ui == "en":
                check("Stop at “drafted, waiting for review” and wait for the user" in out
                      and "evidence" in out and "uncertain or you did not check" in out,
                      "英文交接保留停止条件、证据与不确定性要求")

            report_text = report.read_text(encoding="utf-8")
            script_text = script.read_text(encoding="utf-8")
            report_text = report_text.replace('<p class="kpi">+12%</p>',
                                              '<p class="kpi">YoY +12%</p>' if material == "en"
                                              else '<p class="kpi">同比 +12%</p>')
            if material == "en":
                script_text = script_text.replace("Revenue grew 12% year on year", "Revenue grew 12% YoY")
            else:
                script_text = script_text.replace("收入增长 12%", "收入同比增长 12%")
            report.write_text(report_text, encoding="utf-8")
            script.write_text(script_text, encoding="utf-8")
            cli("build_viewer.py", "--rebuild", review)
            # 改动必须能从产品的差异与状态接口读回，不能只检查测试直接写过的文件。
            diff = cli("notes.py", "diff", review, "--batch", "1", "--to", "current", "--locale", ui)
            check(("YoY +12%" if material == "en" else "同比 +12%") in diff,
                  "源报告修改出现在按页差异中")
            check(("Revenue grew 12% YoY" if material == "en" else "收入同比增长 12%") in diff,
                  "配套讲稿同步修改出现在按页差异中")
            resolution = "Updated both sources without changing figures." if ui == "en" else "报告与讲稿同步，未改数字。"
            cli("notes.py", "resolve", review, note_id, "--seq", "1", "--note", resolution, "--locale", ui)
            summary = ("Checked all six synthetic report pages and their notes. Changed the page 3 wording only; no figures changed."
                       if ui == "en" else "核对合成报告和讲稿全部 6 页，仅改第 3 页表述，未改数字。")
            cli("notes.py", "done", review, "--seq", "1", "--summary", summary, "--locale", ui)
            batches = json.loads(cli("notes.py", "batches", review, "--json"))["history"]
            check(batches[-1]["status"] == "drafted", "初改收尾停在待复核，没有自行定稿")
            page.reload()
            go_page(page, 3)
            check(("YoY +12%" if material == "en" else "同比 +12%") in page.locator(".kpi").inner_text(),
                  "重建后浏览器看到源报告修改")
            check(("Revenue grew 12% YoY" if material == "en" else "收入同比增长 12%") in page.inner_text("#pageScript"),
                  "重建后浏览器看到讲稿修改")

            # 由浏览器中的用户动作授权复核；再次接收、逐条通过、收尾。
            page.click("#handBtn")
            page.click('#handDrafts [data-hreview="1"]')
            page.click("#doHand")
            review_out = cli("notes.py", "wait", review, "--stage", "review", "--timeout", "5", "--locale", ui)
            check(annotation in review_out, "显式请求复核后 CLI 收到同一批批注")
            cli("notes.py", "review", review, note_id, "--seq", "1", "--ok", "--locale", ui)
            cli("notes.py", "done", review, "--seq", "1", "--summary", summary, "--locale", ui)
            final = json.loads(cli("notes.py", "show", review, "--all", "--json"))["notes"][0]
            final_batch = json.loads(cli("notes.py", "batches", review, "--json"))["history"][-1]
            check(final["status"] == "resolved" and final["id"] == note_id and final_batch["status"] == "done"
                  and final_batch["resolvedCount"] == 1, "批注身份、复核结论和处理计数一致")
            check(json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"] == rid,
                  "生成、提交、重建、复核保留审阅身份")
            if output:
                for name in ("report.html", "script.md"):
                    shutil.copyfile(case / name, output / name)
                for name in ("notes.json", "handoff.json"):
                    shutil.copyfile(review / name, output / name)
                (output / "commands.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            ctx.close()
            stop(server)

        # 直接打开同一单文件：界面语言与材料原文保留；下载后通过 CLI 导回。
        ctx = browser.new_context(locale=ui, viewport={"width": 1000, "height": 800})
        try:
            page = ctx.new_page()
            page.goto((review / "index.html").as_uri())
            check("file" in page.inner_text("#mode").lower() if ui == "en" else "本地文件模式" in page.inner_text("#mode"),
                  "无服务时明确显示本地文件模式")
            go_page(page, 4)
            offline_text = "Offline annotation: 原文 <tag> {count}" if ui == "en" else "离线批注：原文 <tag> {count}"
            add_note(page, offline_text)
            page.reload()
            go_page(page, 4)
            check(offline_text in page.inner_text("#nlist"), "文件模式刷新后保留未翻译的批注")
            page.keyboard.press("n")
            page.select_option("#expLang", ui)
            for selector, extension in (("#dlAll", "md"), ("#dlJson", "json")):
                with page.expect_download() as event:
                    page.click(selector)
                export = case / ("offline." + extension)
                event.value.save_as(export)
                if extension == "md":
                    check(export.read_text(encoding="utf-8").startswith(f"<!-- annotation-everything export v1 lang={ui} -->"),
                          "文件模式下载带语言标记的 Markdown")
                    cli("notes.py", "import", review, export, "--locale", "zh-CN" if ui == "en" else "en")
                else:
                    check(json.loads(export.read_text(encoding="utf-8"))["reviewId"] == rid,
                          "JSON 下载保留审阅身份")
                if output:
                    shutil.copyfile(export, output / export.name)
            imported = json.loads(cli("notes.py", "show", review, "--all", "--json"))["notes"]
            check(any(note["text"] == offline_text for note in imported), "文件模式批注可跨语言导入原审阅")
            if ui == "en":
                page.click("#openImp")
                page.fill("#impText", json.dumps({"reviewId": "0123456789abcdef", "name": '<img src=x onerror="window.__deliveryXss=1">',
                                                  "notes": [{"page": 1, "text": "Do not import this review"}]}))
                dialogs = []

                def reject_wrong_review(dialog):
                    dialogs.append(dialog.message)
                    dialog.dismiss()

                page.on("dialog", reject_wrong_review)
                page.click("#doImp")
                page.remove_listener("dialog", reject_wrong_review)
                check(len(dialogs) == 1 and "another review" in dialogs[0], "导入身份冲突的确认提示使用当前语言")
                check(page.evaluate("window.__deliveryXss") is None
                      and "Do not import this review" not in page.inner_text("#nlist"),
                      "取消跨审阅导入保留数据隔离，名称中的恶意属性不执行")
            offline_batches = json.loads(cli("notes.py", "batches", review, "--json"))["history"]
            check(len(offline_batches) == 1 and offline_batches[0]["status"] == "done",
                  "文件模式未凭空制造服务端提交")
        finally:
            ctx.close()
        evidence["scenarios"].append({"name": label, "ui": ui, "material": material,
                                       "checks": RESULTS[start_result:]})
        if output:
            (output / "commands.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    if artifacts:
        (artifacts / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ae-e2e-"))
    CLEANUP.add_dir(tmp)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        installed_delivery_checks(browser, tmp)
        if "--delivery-only" in sys.argv:
            browser.close()
            failed = sum(not p for p, _ in RESULTS)
            print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} 通过")
            return 1 if failed else 0
        locale_live_controls_checks(browser, tmp)
        if "--full-check-only" in sys.argv:
            full_check_checks(browser, tmp)
            browser.close()
            return 1 if any(not p for p, _ in RESULTS) else 0
        if "--handoff-only" in sys.argv:
            handoff_i18n_checks(browser, tmp)
            browser.close()
            failed = sum(not p for p, _ in RESULTS)
            print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} 通过")
            return 1 if failed else 0
        skill_search_checks(browser, tmp)
        skill_recommendation_checks(browser, tmp)
        skill_i18n_checks(browser, tmp)
        if "--skills-only" in sys.argv:
            browser.close()
            failed = sum(not p for p, _ in RESULTS)
            print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} 通过")
            return 1 if failed else 0
        full_check_checks(browser, tmp)
        locale_checks(browser, tmp)
        english_script_checks(browser, tmp)
        handoff_i18n_checks(browser, tmp)
        export_import_checks(browser, tmp)

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
        ok(page.locator('#skillList [role="option"]').filter(has_text="humanizer-zh").count() == 1, "技能目录出现在「技能」输入栏的候选里")
        go_page(page, 2)   # 放在第 2 页，不影响第 3 页「全部已处理」的检查
        page.fill("#ntext", "这段口气太硬，润色一下")
        page.fill("#nskills", "humanizer-zh, dataviz")
        page.click('[data-act="add"]')
        page.wait_for_timeout(600)
        sk = [n for n in disk_notes(sdir) if n["text"] == "这段口气太硬，润色一下"]
        ok(sk and sk[0].get("skills") == ["humanizer-zh", "dataviz"] and "⚙ humanizer-zh" in page.inner_text("#nlist"),
           "批注带上指定技能并显示")
        shown = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(sdir)], capture_output=True, text=True).stdout
        ok("技能：humanizer-zh（这条指定）、dataviz（这条指定）——处理这条前先调用这些技能" in shown, "notes.py show 列出这条批注要调用的技能")
        # 常驻技能：G 打开，按范围设置，保存后每条批注卡片都标出、notes.py show 每条都列出
        page.keyboard.press("Escape")
        page.keyboard.press("g")
        ok(page.is_visible("#always"), "按 G 打开常驻技能设置")
        page.fill("#alwaysRows .arow:last-child .aname", "checker-x")
        page.click("#alwaysAdd")
        page.fill("#alwaysRows .arow:last-child .aname", "humanizer-zh")
        page.select_option("#alwaysRows .arow:last-child .ascope", "讲稿")
        page.click("#alwaysSave")
        page.wait_for_timeout(500)
        sj = json.loads((sdir / "skills.json").read_text())
        ok(sj.get("always") == [{"name": "checker-x", "scope": "all"}, {"name": "humanizer-zh", "scope": "讲稿"}]
           and sj.get("skills"), "常驻技能存进审阅目录的 skills.json，技能目录不受影响")
        ok("常驻 checker-x" in page.inner_text("#nlist") and page.inner_text("#alwaysN") == "2",
           "保存后：批注卡片标出适用的常驻技能，按钮显示个数")
        shown = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(sdir)], capture_output=True, text=True).stdout
        ok("checker-x（常驻·全部批注）" in shown and "常驻技能（用户要求" in shown, "notes.py show 每条批注都列出常驻技能")
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
        other_page = page.evaluate("P.find(p => p.n !== P[i].n).n")
        page.select_option("#epage", str(other_page))
        page.click(f'#nlist [data-note="{B}"] [data-act="edit"]')
        ok(page.input_value("#etext") != "AAA 改到一半", "从批注 A 的编辑直接点批注 B 的「编辑」，B 不会带上 A 的草稿")
        ok(page.input_value("#epage") == str(cur[B]["page"]), "批注 B 不会带上 A 尚未保存的目标页")
        page.click(f'#nlist [data-note="{A}"] [data-act="edit"]')
        ok(page.input_value("#etext") == "AAA 改到一半", "切回 A，A 的草稿还在")
        ok(page.input_value("#epage") == str(other_page), "切回 A，尚未保存的目标页还在")
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
        kept_destination = True
        for locale in ("en", "zh-CN"):
            page.locator("#localePick").evaluate("(el, value) => {el.value=value;el.dispatchEvent(new Event('change'))}", locale)
            kept_destination = kept_destination and page.input_value(f'#orphanBox select[data-mv="{lost_id}"]') == "2"
        ok(kept_destination, "孤立批注选择目标页后切换语言，保留尚未提交的目标页")
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
        CLEANUP.add(waiter)
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
        # ---------- 先改后审：停在待复核 → 请求复核 → 复核者退回；再一批直接定稿 ----------
        def cli(*args):
            return subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), *args], capture_output=True, text=True, check=True)
        def last_batch():
            return json.loads((sdir / "handoff.json").read_text())["items"][-1]
        cli("done", str(sdir), "--summary", "收尾上一批")
        page.keyboard.press("j")
        page.wait_for_timeout(400)
        page.click("#batches [data-close] kbd")
        ok(not page.is_visible("#batches"), "点关闭按钮上的「Esc」标签也能关掉处理记录")
        page.keyboard.press("a")
        page.check('input[name="htarget"][value="review"]')
        page.uncheck("#hSub")
        page.fill("#handMsg", "先初改，我看过再复核")
        page.click("#doHand")
        page.wait_for_timeout(4800)
        lb = last_batch()
        ok(lb["target"] == "review" and lb["allowSubagents"] is False and lb["status"] == "pending",
           "交给 Agent 时选「已初改·待复核」、不允许子代理，选项随提交落盘")
        ok(page.inner_text("#nflight").startswith("处理中") and page.inner_text("#ncount") == "",
           f"交给 Agent 后，「批注」按钮显示「处理中 N」而不是未处理红点（{page.inner_text('#notesBtn')!r}）")
        cli("claim", str(sdir), "--by", "sonnet-初改")
        target = [n for n in disk_notes(sdir) if n["id"] in lb["openIds"]][0]
        cli("resolve", str(sdir), target["id"], "--note", "标题改短", "--by", "sonnet-初改")
        cli("done", str(sdir), "--summary", "初改 1 条", "--by", "sonnet-初改")
        go_page(page, target["page"])
        page.wait_for_timeout(4800)
        card = page.inner_text(f'#nlist [data-note="{target["id"]}"]')
        ok("已初改 · 待复核" in page.inner_text("#handState") and page.inner_text("#ndraft") == "待复核 1",
           f"初改收尾后：状态显示「已初改 · 待复核」，「批注」按钮显示「待复核 1」（{page.inner_text('#handState')!r} / {page.inner_text('#notesBtn')!r}）")
        ok("已初改·待复核" in card and "初改说明：标题改短" in card and "痕迹：初改（sonnet-初改 · 第" in card,
           "待复核的批注卡片显示状态、初改说明与处理痕迹")
        page.keyboard.press("j")
        page.wait_for_timeout(600)
        bl = page.inner_text("#batchList")
        ok("要求：做到已初改·待复核" in bl and "初改 · sonnet-初改" in bl and "已初改·待复核的 1 条" in bl,
           "处理记录显示要求、初改阶段与待复核的批注")
        page.click(f'#batchList [data-breview="{lb["seq"]}"]')
        ok(page.is_visible("#hand") and f"请 Agent 复核第 {lb['seq']} 批" in page.inner_text("#handTitle")
           and not page.is_visible("#hTargetRow"), "点「请 Agent 复核」打开复核请求（不再问做到哪一步）")
        page.fill("#handMsg", "重点看标题")
        page.click("#doHand")
        page.wait_for_timeout(600)
        lb = last_batch()
        ok(lb["status"] == "review" and lb["reviewMessage"] == "重点看标题", "请求复核落盘：等待复核接收")
        wout = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(sdir), "--timeout", "5", "--stage", "review",
                               "--by", "opus-复核"], capture_output=True, text=True).stdout
        ok("收到复核请求" in wout and "重点看标题" in wout and target["id"] in wout, "复核者 wait --stage review 收到复核请求与待复核批注")
        cli("review", str(sdir), target["id"], "--return", "--note", "口径要你定", "--by", "opus-复核")
        cli("done", str(sdir), "--summary", "退回 1 条", "--by", "opus-复核")
        page.wait_for_timeout(4800)
        card = page.inner_text(f'#nlist [data-note="{target["id"]}"]')
        ok("退回给你" in card and "口径要你定" in card and "已初改" not in card.split("痕迹")[0],
           "复核者退回：批注回到未处理，痕迹里写明退回原因")
        page.keyboard.press("Escape")
        page.keyboard.press("a")
        page.check('input[name="htarget"][value="review"]')
        page.click("#doHand")
        page.wait_for_timeout(400)
        cli("claim", str(sdir), "--by", "claude")
        cli("resolve", str(sdir), target["id"], "--note", "按你上次说的口径改", "--by", "claude")
        cli("done", str(sdir), "--by", "claude")
        page.wait_for_timeout(4800)
        page.keyboard.press("a")
        page.wait_for_timeout(200)
        seq2 = last_batch()["seq"]
        page.click(f'#handDrafts [data-hfinal="{seq2}"]')
        page.wait_for_timeout(800)
        lb = last_batch()
        fin = [n for n in disk_notes(sdir) if n["id"] == target["id"]][0]
        ok(lb["status"] == "done" and fin["status"] == "resolved" and fin["trail"][-1]["action"] == "accepted",
           "「交给 Agent」弹窗顶部的「直接定稿」：跳过复核，批注定为已处理并记为你定稿")
        page.keyboard.press("Escape")

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
        build(other, name="B 稿票面")
        stop(srv)
        srv_b = start_server(other, port)
        add_note(page, "切换后立刻写的 A 稿意见")   # 页面还以为连着 A
        page.wait_for_timeout(1500)
        ok(not any("A 稿" in n["text"] for n in disk_notes(other)), "服务刚切换就写，也不会写进 B 稿的 notes.json")
        # A 停掉的空档里若刚好轮询过，页面先是「服务未连接」，下一次轮询（4 秒内）才认出端口上换成了 B
        # （对照页的 CSP 不放行 eval，wait_for_function 用不了，在这边轮询）
        wait_until(lambda: "服务不匹配" in page.inner_text("#mode"), page, 8)
        ok("服务不匹配" in page.inner_text("#mode") and "待补传" in page.inner_text("#mode"), "页面显示「服务不匹配」，改动留在待补传")
        rid_a = json.loads((sdir / "review.json").read_text())["reviewId"]
        wait_until(lambda: "《B 稿票面》" in page.inner_text("#srvText"), page, 5)   # 提示条先显示，再异步问端口上是谁
        bar = page.inner_text("#srvBar")
        ok("《B 稿票面》" in bar and f"serve.py {rid_a}" in bar and str(sdir) not in bar,
           "不匹配提示说出端口上现在是哪份审阅，给出按标识启动的命令，不含本机路径")
        # 同一地址打开 B 稿的页面：发现 A 稿旧标签页留下的未补传改动，只能下载导回 A 稿
        pb = ctx.new_page()
        pb.goto(f"http://127.0.0.1:{port}/")
        pb.wait_for_selector("#strayBtn", state="visible", timeout=8000)
        ok("其他审阅的未保存改动" in pb.inner_text("#strayBtn"), "B 稿页面提示这个地址下有别的审阅的未保存改动")
        pb.click("#strayBtn")
        ok("另一个名字" not in pb.inner_text("#strays") and rid_a[:8] in pb.inner_text("#strays"), "列出 A 稿（按标识）")
        with pb.expect_download() as dl:
            pb.click("#strays [data-sdl]")
        rescued = json.loads(Path(dl.value.path()).read_text(encoding="utf-8"))
        ok(rescued["reviewId"] == rid_a and any(n["text"] == "切换后立刻写的 A 稿意见" for n in rescued["notes"]),
           "下载的 JSON 带 A 稿标识和那条未补传的批注")
        rf = tmp / "rescued.json"
        rf.write_text(json.dumps(rescued, ensure_ascii=False), encoding="utf-8")
        wrong = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "import", str(other), str(rf)],
                               capture_output=True, text=True)
        ok(wrong.returncode == 2 and "另一份审阅" in wrong.stderr, "导进 B 稿被拒绝")
        ok(not any("A 稿" in n["text"] for n in disk_notes(other)), "B 稿的 notes.json 始终没有 A 稿的批注")
        pb.close()
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

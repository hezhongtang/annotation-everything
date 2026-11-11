"""浏览器端到端测试（真 Chrome）。测试专用依赖 playwright，skill 本身不依赖它。

  uv run --no-project --with playwright python tests/e2e_browser.py             # 全部注册场景
  uv run --no-project --with playwright python tests/e2e_browser.py --list     # 场景清单
  uv run --no-project --with playwright python tests/e2e_browser.py --only skill-i18n

场景注册表（稳定 ID、行为、fixture、隔离与旧参数别名）在 tests/e2e_scenarios.py，
无浏览器依赖、单元发现可导入；本文件只挂执行函数（SCENARIO_RUNNERS），两侧由
入口启动时的一致性检查把关。每个场景独立浏览器上下文与临时审阅目录，能单独运行。

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

import hashlib
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
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import e2e_scenarios as scenarios
import e2e_waits
from e2e_servers import CLEANUP, free_port, install, start_server, stop

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURE = ROOT / "fixtures" / "sample"
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-e2e-home-")   # 测试不碰用户真实的审阅台账；子进程继承
os.environ["HOME"] = tempfile.mkdtemp(prefix="ae-e2e-user-")       # 技能扫描只使用合成目录
os.environ["CODEX_HOME"] = str(Path(os.environ["HOME"]) / ".codex")
os.environ["CLAUDE_SKILLS_DIR"] = str(Path(os.environ["HOME"]) / ".claude" / "skills")
sys.path.insert(0, str(ROOT))

import host_isolation

# 直跑本脚本（uv run …）与 verify.py 的浏览器组共用同一隔离协议：启动任何真实服务前先取宿主机资源锁；
# 被 verify.py 拉起时父入口已持锁，环境变量会让这次获取按借入处理，不会自锁
host_isolation.require_for_module("tests.e2e_browser")

# 等待层的超时识别：playwright 的 TimeoutError 视为「等待超时」，其他异常原样传播
e2e_waits.register_timeout_errors(PlaywrightTimeoutError)

install()   # 兜底清理：正常退出、未捕获异常、Ctrl-C 走 atexit，SIGTERM / SIGHUP 另接
CLEANUP.add_dir(Path(os.environ["AE_HOME"]))
CLEANUP.add_dir(Path(os.environ["HOME"]))
RESULTS: list[tuple[bool, str]] = []
STAGE = "document.getElementById('host').shadowRoot.getElementById('ae-stage')"


def ok(cond, label):
    RESULTS.append((bool(cond), label))
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)


def describe_page(page):
    """超时诊断用的「最后观察到的状态」快照：徽标、提示条、提交状态与批注列表。"""
    try:
        state = page.evaluate("""() => {
            const g = id => { const el = document.getElementById(id); return el ? el.textContent : null };
            return {mode: g('mode'), toast: g('toast'), hand: g('handState'),
                    notes: (g('nlist') || '').slice(0, 120)};
        }""")
        return "mode=%r toast=%r handState=%r nlist=%r" % (
            state.get("mode"), state.get("toast"), state.get("hand"), state.get("notes"))
    except Exception as exc:   # 页面已关闭等：快照失败不能掩盖等待超时本身
        return "（页面快照失败：%s: %s）" % (type(exc).__name__, exc)


def wait_dom(page, expression, seconds, arg=None, condition=None):
    """等浏览器里的可观察状态（wait_for_function 浏览器侧自动重试），超时 False + 诊断。"""
    return e2e_waits.wait_dom(page, expression, seconds, arg=arg, condition=condition,
                              describe=lambda: describe_page(page))


def wait_poll(page, seconds=10, condition=None):
    """等一次页面轮询往返确实发生（GET /api/notes 请求与应答都到达）。"""
    return e2e_waits.wait_poll(page, seconds, condition=condition,
                               describe=lambda: describe_page(page))


def wait_rev(page, before, seconds=10):
    """等页面应用一次新的服务端修订（全局 rev 离开基线），作为「轮询推进」的证据。"""
    return e2e_waits.wait_rev(page, before, seconds, describe=lambda: describe_page(page))


def wait_response(page, predicate, seconds, condition=None, action=None):
    """等下一个匹配的网络事件完成（expect_response 事件驱动，不靠固定间隔）。"""
    return e2e_waits.wait_response(page, predicate, seconds, condition=condition,
                                   describe=lambda: describe_page(page), action=action)


# 常用可观察条件的 JS 表达式（全部是页面自身的状态，不需要往页面加测试钩子）
JS_MODE_CLASS = "c => document.getElementById('mode').classList.contains(c)"
JS_TOAST_TEXT = "t => document.getElementById('toast').textContent.includes(t)"
JS_CARD_MORE = "n => document.querySelectorAll('#nlist [data-note]').length > n"
JS_LIST_TEXT = "t => document.getElementById('nlist').textContent.includes(t)"
JS_PAGE_IS = "n => localStorage.getItem('ae.' + RID + '.page') === String(n)"
JS_BATCHES_ON = "() => document.getElementById('batches').classList.contains('on')"


def wait_card(page, before, seconds=5, condition=None):
    """等批注列表出现新卡片：添加动作的可观察结果（比固定等待短，也不会误等）。"""
    return wait_dom(page, JS_CARD_MORE, seconds, arg=before,
                    condition=condition or "批注列表出现新卡片")


def wait_mode(page, class_name, seconds=10, condition=None):
    """等 mode 徽标进入指定状态（server / offline / mismatch / old）。"""
    return wait_dom(page, JS_MODE_CLASS, seconds, arg=class_name,
                    condition=condition or "mode 徽标进入 %r 状态" % class_name)


def wait_toast(page, text, seconds=10, condition=None):
    """等提示条出现指定文字（toast 文案是页面处理结果的直接证据）。"""
    return wait_dom(page, JS_TOAST_TEXT, seconds, arg=text,
                    condition=condition or "提示条出现 %r" % text)


def wait_batches(page, text, seconds=8, condition=None):
    """等处理记录弹窗打开且载入了期望内容。

    openBatches 先显示弹窗，再经 GET /api/handoff 异步取全量历史：弹窗打开 ≠ 内容就绪，
    读 #batchList 前必须等内容（此前靠固定等待垫住这个时差）。
    """
    return wait_until(lambda: page.is_visible("#batches") and text in page.inner_text("#batchList"),
                      page, seconds,
                      condition=condition or "处理记录显示 %r" % text)


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
    """添加一条批注，并等到它作为新卡片出现在列表里（添加动作的可观察结果）。

    列表重绘是 mutate 的同步结果；服务模式下的落盘由调用方按磁盘状态另行等待。
    """
    before = page.locator("#nlist [data-note]").count()
    if pin:
        page.click('[data-act="pin"]')
        box = page.locator("#slide").bounding_box()
        page.mouse.click(box["x"] + box["width"] * pin[0], box["y"] + box["height"] * pin[1])
    page.fill("#ntext", text)
    page.click('[data-act="add"]')
    wait_card(page, before, condition="新批注 %r 出现在列表里" % text[:30])


def wait_until(cond, page, seconds: float, condition=None) -> bool:
    """有界轮询 Python 侧条件（磁盘文件、DOM 与磁盘的组合），超时 False + 诊断。

    每圈先做一次不等待的 evaluate，让 playwright 事件循环跑一圈：排队的网络
    事件（request / response 回调）及时进入测试侧计数器——时序与旧的
    wait_for_timeout 轮询一致，但不引入任何固定时间等待。
    """
    def probe():
        try:
            page.evaluate("() => null")
        except Exception:
            pass   # 页面跳转 / 关闭中的抖动让位给条件本身或后续异常路径
        return cond()
    return e2e_waits.bounded_poll(probe, seconds, condition=condition,
                                  describe=lambda: describe_page(page))


def go_page(page, n):
    page.select_option("#jump", str(n - 1))
    # render() 同步把当前页写进 localStorage（ae.<RID>.page），与界面语言无关
    wait_dom(page, JS_PAGE_IS, 5, arg=n, condition="翻页后当前页是第 %d 页" % n)


def drag_select(page, locator):
    """真实的鼠标拖选（报告在 Shadow DOM 里，locator 能穿透）。

    mouseup 的处理是同步的：选区条、待确认状态的出现与否在返回时已确定，
    由调用方按各自的可观察状态断言（不再垫固定等待）。
    """
    b = locator.bounding_box()
    y = b["y"] + min(12, b["height"] / 2)   # 第一行文字上，避免落在两行之间的空隙
    page.mouse.move(b["x"] + 2, y)
    page.mouse.down()
    page.mouse.move(b["x"] + b["width"] - 2, y, steps=8)
    page.mouse.up()


def capture_screenshot(page, path):
    # 视口变化后等字体和布局帧就绪，避免截到 Chrome 尚未更新的合成帧。
    page.evaluate("""async () => {
        await document.fonts.ready;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    page.screenshot(path=str(path), animations="disabled")


def console_layout_checks(browser, tmp: Path):
    review, other = tmp / "console", tmp / "console-other"
    build(review, name="合成布局审阅")
    build(other)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    artifacts = os.environ.get("AE_LAYOUT_ARTIFACTS")
    output = Path(artifacts) if artifacts else None
    if output:
        output.mkdir(parents=True, exist_ok=True)
    try:
        page = ctx.new_page()
        page.goto((review / "index.html").as_uri())
        ready = page.locator("#sideToggle").count() == 1
        ok(ready, "标题栏提供三个抽屉与全屏入口")
        if not ready:
            return
        ok(page.inner_text("#reviewTitle") == "合成布局审阅", "标题栏显示审阅名")
        for locale in ("zh-CN", "en"):
            page.select_option("#localePick", locale)
            for width, height in ((1280, 800), (1440, 900)):
                page.set_viewport_size({"width": width, "height": height})
                metrics = page.evaluate("""() => {
                    const header = document.querySelector('header'), button = document.getElementById('handBtn');
                    const h = header.getBoundingClientRect(), b = button.getBoundingClientRect();
                    return {width: innerWidth, headerHeight: h.height, buttonTop: b.top, buttonBottom: b.bottom,
                            buttonRight: b.right, labelWidth: button.scrollWidth, buttonWidth: button.clientWidth,
                            fits: h.height <= 52 && b.top >= h.top && b.bottom <= h.bottom && b.right <= innerWidth
                                  && button.scrollWidth <= button.clientWidth};
                }""")
                ok(metrics["fits"], "%s：%spx 标题栏单行，主按钮完整显示（%s）" % (locale, width, metrics))
                page.locator("#modeSwitch").evaluate("el => el.hidden = false")
                ok(page.evaluate("""() => {
                    const r = document.getElementById('modeSwitch').getBoundingClientRect();
                    const title = document.getElementById('reviewTitle').getBoundingClientRect();
                    const controls = document.querySelector('.layout-controls').getBoundingClientRect();
                    const hand = document.getElementById('handBtn').getBoundingClientRect();
                    return Math.abs((r.left + r.right) / 2 - innerWidth / 2) < 1
                           && title.right <= r.left && r.right <= controls.left && hand.right <= innerWidth;
                }"""), "%s：%spx 分析模式插槽居中，标题与右侧按钮不重叠" % (locale, width))
                page.locator("#modeSwitch").evaluate("el => el.hidden = true")
                if output:
                    stem = "%s.%s" % (width, locale)
                    (output / (stem + ".json")).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
                    capture_screenshot(page, output / (stem + ".png"))
            for width in (720, 960):
                page.set_viewport_size({"width": width, "height": 780})
                page.locator("#modeSwitch").evaluate("el => el.hidden = false")
                metrics = page.evaluate("""() => {
                    const ids=['annotationMode','analysisMode','sideToggle','scriptToggle','panelToggle','focusBtn','handBtn'];
                    const targets=ids.map(id=>{
                        const element=document.getElementById(id),r=element.getBoundingClientRect();
                        const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
                        return {id,inside:r.left>=0&&r.right<=innerWidth,hit:hit?.closest('button')?.id||null};
                    });
                    return {targets,fits:targets.every(target=>target.inside&&target.hit===target.id)};
                }""")
                ok(metrics["fits"], "%s：%spx 模式与抽屉按钮都可见且无遮挡（%s）" % (locale, width, metrics))
                page.locator("#modeSwitch").evaluate("el => el.hidden = true")
            page.set_viewport_size({"width": 1440, "height": 900})
        ok(page.locator("#modeSwitch").is_hidden() and page.locator("#analysisQueue").is_hidden(),
           "服务未支持分析时，模式切换和分析队列插槽隐藏")
        page.fill("#ntext", "\n".join("合成的长批注意见" for _ in range(60)))
        ok(page.evaluate("""() => {
            const f = document.getElementById('nform').getBoundingClientRect();
            const p = document.getElementById('panelBody').getBoundingClientRect();
            const add = document.getElementById('addBtn').getBoundingClientRect();
            return f.bottom <= p.bottom + 1 && add.bottom <= p.bottom + 1;
        }"""), "长批注输入不挤出底部面板，添加按钮仍在面板内")
        page.fill("#ntext", "")
        for toggle, panel, key in (("sideToggle", "sidebar", "["),
                                   ("scriptToggle", "scriptPane", "]"),
                                   ("panelToggle", "panelBody", "\\")):
            page.click("#" + toggle)
            ok(page.locator("#" + panel).is_hidden(), "%s：鼠标收起抽屉" % toggle)
            page.reload()
            ok(page.locator("#" + panel).is_hidden(), "%s：刷新保留收起状态" % toggle)
            page.keyboard.press(key)
            ok(page.locator("#" + panel).is_visible(), "%s：快捷键展开抽屉" % toggle)
        page.fill("#ntext", "[ ] \\ f")
        page.keyboard.press("[")
        ok(page.locator("#sidebar").is_visible(), "输入框聚焦时不触发布局快捷键")
        page.fill("#ntext", "")
        page.keyboard.press("Escape")
        for toggle in ("sideToggle", "scriptToggle", "panelToggle"):
            page.click("#" + toggle)
        ok(page.locator("#seeList").is_visible() and page.locator("#scriptHandle").is_visible(),
           "抽屉收起后保留底部页签和讲稿展开条")
        if output:
            capture_screenshot(page, output / "drawers-closed.png")
        page.goto((other / "index.html").as_uri())
        ok(page.locator("#sidebar").is_visible() and page.locator("#panelBody").is_visible(),
           "另一份审阅不继承本审阅的抽屉状态")
        page.set_viewport_size({"width": 820, "height": 900})
        page.reload()
        ok(page.locator("#sidebar").is_hidden() and page.locator("#scriptPane").is_hidden(),
           "窄屏侧栏和讲稿默认收起")
        page.click("#scriptHandle")
        ok(page.evaluate("""() => document.getElementById('scriptPane').getBoundingClientRect().top
            >= document.getElementById('reportPane').getBoundingClientRect().bottom - 1"""),
           "窄屏展开讲稿后报告与讲稿上下排列")
        if output:
            capture_screenshot(page, output / "narrow.png")
        page.click("#seeList")
        add_note(page, "窄屏合成批注")
        ok("窄屏合成批注" in page.inner_text("#nlist"), "窄屏仍能添加批注")
        page.click("#notesBtn")
        ok(page.is_visible("#allnotes"), "窄屏活动栏可打开全部批注")
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})
        mobile = page.evaluate("""() => {
            const b = document.getElementById('handBtn').getBoundingClientRect();
            return {fits: b.left >= 0 && b.right <= innerWidth && document.documentElement.scrollWidth === innerWidth,
                    buttonRight: b.right, documentWidth: document.documentElement.scrollWidth, viewport: innerWidth,
                    overflow: [...document.querySelectorAll('body *')].filter(el => el.getClientRects().length
                        && el.getBoundingClientRect().right > innerWidth + 1).slice(0, 8)
                        .map(el => ({tag: el.tagName, id: el.id, width: el.getBoundingClientRect().width}))};
        }""")
        ok(mobile["fits"], "390px 窄屏标题栏分行，主按钮和入口仍在视口内（%s）" % mobile)
        if output:
            capture_screenshot(page, output / "narrow-mobile.png")
    finally:
        ctx.close()


def focus_mode_checks(browser, tmp: Path):
    review = tmp / "focus"
    build(review)
    for fallback in (False, True):
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        server = None
        try:
            if fallback:
                ctx.add_init_script("Element.prototype.requestFullscreen = () => Promise.reject(new Error('synthetic unavailable'))")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            if fallback:
                page.goto((review / "index.html").as_uri())
            else:
                port = free_port()
                server = start_server(review, port)
                page.goto(f"http://127.0.0.1:{port}/")
                ok(wait_mode(page, "server"), "全屏场景连接真实本地服务")
            ready = page.locator("#focusBtn").count() == 1
            ok(ready, "全屏专注入口存在（fallback=%s）" % fallback)
            if not ready:
                continue
            page.click("#focusBtn")
            ok(wait_dom(page, "() => document.body.classList.contains('focus-mode')", 3),
               "进入全屏专注（fallback=%s）" % fallback)
            ok(wait_dom(page, "expected => Boolean(document.fullscreenElement) === expected", 3,
                        arg=not fallback), "优先原生全屏，不可用时使用 CSS 退路")
            ok(page.locator("header").is_hidden() and page.locator("#statusbar").is_hidden()
               and page.locator("#slide").is_visible(), "全屏只显示报告和快捷键提示")
            page.keyboard.press("c")
            page.fill("#ntext", "全屏合成批注")
            ok(page.evaluate("document.activeElement.id") == "ntext", "全屏 C 打开原有输入框")
            artifacts = os.environ.get("AE_LAYOUT_ARTIFACTS")
            if artifacts and not fallback:
                capture_screenshot(page, Path(artifacts) / "focus-composer.png")
            page.keyboard.press("Control+Enter")
            ok(wait_dom(page, "() => notes.some(n => n.text === '全屏合成批注')", 3),
               "全屏 Ctrl+Enter 成功添加批注")
            if not fallback:
                ok(wait_until(lambda: any(n["text"] == "全屏合成批注" for n in disk_notes(review)), page, 5),
                   "全屏添加的批注经服务落盘")
            page.keyboard.press("c")
            page.fill("#ntext", "全屏未完成草稿")
            page.keyboard.press("Escape")
            ok(page.locator("#nform").is_hidden() and page.evaluate("document.body.classList.contains('focus-mode')"),
               "Esc 先关闭浮动输入框，仍保留全屏")
            page.keyboard.press("ArrowRight")
            page.keyboard.press("c")
            ok(page.input_value("#ntext") == "", "全屏翻页不把上一页草稿带进空白页")
            page.fill("#ntext", "第二页自己的草稿")
            page.keyboard.press("Escape")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("c")
            ok(page.input_value("#ntext") == "全屏未完成草稿", "全屏返回上一页恢复该页草稿")
            page.keyboard.press("Escape")
            page.keyboard.press("ArrowRight")
            page.keyboard.press("c")
            ok(page.input_value("#ntext") == "第二页自己的草稿", "全屏翻页保留目的页原有草稿")
            page.keyboard.press("Escape")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("p")
            ok(page.evaluate("markMode"), "全屏 P 进入标注")
            page.keyboard.press("Escape")
            ok(not page.evaluate("markMode") and page.evaluate("document.body.classList.contains('focus-mode')"),
               "Esc 先取消标注，仍保留全屏")
            page.keyboard.press("p")
            stage_box = page.locator("#host #ae-stage").bounding_box()
            page.mouse.click(stage_box["x"] + stage_box["width"] * .4,
                             stage_box["y"] + stage_box["height"] * .5)
            page.fill("#ntext", "全屏标注批注")
            page.keyboard.press("Control+Enter")
            mark = page.locator("#host .ae-pin:not(.pending)").first
            mark.click()
            ok(page.is_visible("#pinpop") and page.evaluate("focusMode"), "全屏点击已有标注只预览，保持专注")
            page.keyboard.press("Escape")
            ok(page.locator("#pinpop").is_hidden() and page.evaluate("focusMode"), "Esc 只关闭标注预览")
            mark.click()
            page.click('#pinpop [data-pp="edit"]')
            ok(not page.evaluate("focusMode") and page.is_visible("#etext"), "明确编辑标注时退出专注并显示原有编辑卡片")
            page.click('#nlist [data-act="cancel"]')
            page.keyboard.press("f")
            page.keyboard.press("s")
            ok(page.is_visible("#stylemenu"), "全屏 S 打开风格菜单")
            page.keyboard.press("1")
            ok(page.locator("#nform").is_visible(), "选择风格后可在全屏浮层写批注")
            page.keyboard.press("Escape")
            # 真实鼠标选中报告文字，沿用原有 D / R / C 与「同时改」的流程。
            for action in ("d", "r", "c"):
                drag_select(page, page.locator("#host h1").first)
                selected = page.is_visible("#selbar")
                ok(selected, "全屏选区浮层可见（%s）" % action.upper())
                if not selected:
                    continue
                page.keyboard.press(action)
                if action == "d":
                    page.keyboard.press("d")
                    ok(page.evaluate("notes.some(n => n.edit?.op === 'delete')"), "全屏 D 两次确认删除")
                elif action == "r":
                    page.fill("#repText", "合成改写标题")
                    page.keyboard.press("Enter")
                    ok(page.evaluate("notes.some(n => n.edit?.to === '合成改写标题')"), "全屏 R 改写选区")
                else:
                    page.fill("#ntext", "全屏选区引用批注")
                    page.keyboard.press("Control+Enter")
                    ok(page.evaluate("notes.some(n => n.text === '全屏选区引用批注' && n.quote)"),
                       "全屏 C 引用选中文字添加批注")
            page.keyboard.press("ArrowRight")
            ok(page.evaluate("i") == 1, "全屏方向键翻页")
            page.keyboard.press("Escape")
            ok(wait_dom(page, "() => !document.body.classList.contains('focus-mode')", 3),
               "没有浮层时 Esc 退出全屏")
            ok(page.locator("header").is_visible() and not errors, "退出全屏恢复布局，无页面异常")
        finally:
            ctx.close()
            if server:
                stop(server)


def review_overview_checks(browser, tmp: Path):
    workspace = tmp / "overview-workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    current, stopped, missing = [workspace / name for name in ("current", "stopped", "missing")]
    build(current, name="Synthetic current review")
    material = workspace / "synthetic-material"
    material.mkdir()
    report = material / "report.html"
    shutil.copyfile(FIXTURE / "report.en.html", report)
    record = {"schema": "ae-report-record/1", "generatedAt": "2026-11-03T12:00:00Z",
              "summary": "Revenue grew. <b>Review costs.</b>",
              "request": {"purpose": "Quarterly decision", "audience": "经营委员会",
                          "coreQuestions": ["Why growth?", "Which costs?"]},
              "sources": [], "method": {},
              "links": {"report": "report.html", "sha256": {"report": hashlib.sha256(report.read_bytes()).hexdigest()}},
              "findings": [], "gaps": [], "checks": [], "openItems": [], "verdict": {}}
    (material / "report-record.json").write_text(json.dumps(record), encoding="utf-8")
    build(stopped, report=report, script=FIXTURE / "script.en.md", name="Synthetic stopped review")
    build(missing, name="Synthetic missing review")
    shutil.rmtree(missing)
    srv = start_server(current, free_port())
    port = json.loads((current / ".annotation-server.json").read_text())["port"]
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/#overview")
        ready = wait_dom(page, "() => document.getElementById('overviewList')?.querySelector('[data-review-id]') != null", 8,
                         condition="总览显示合成审阅列表")
        ok(ready, "地址 #overview 直接打开审阅总览")
        if not ready:
            return
        detail = page.inner_text("#overviewDetail")
        ok("（无摘要）" in detail and "示例咖啡 2030 年三季度经营回顾" in detail,
           "无记录审阅显示无摘要和报告封面真实标题")
        ok(page.is_visible("#overviewBack") and page.is_visible("#overviewScope"), "总览提供范围切换和返回当前审阅")
        for state in ("running", "stopped", "invalid"):
            ok(page.locator('[data-overview-group="%s"]' % state).count() == 1, "总览显示 %s 分组" % state)
        stopped_id = json.loads((stopped / "review.json").read_text())["reviewId"]
        page.click('[data-review-id="%s"]' % stopped_id)
        ok("Synthetic stopped review" in page.inner_text("#overviewDetail"), "选择审阅后详情与列表一致")
        detail = page.inner_text("#overviewDetail")
        ok(all(text in detail for text in (record["summary"], "Quarterly decision", "经营委员会",
                                          "Why growth?", "Which costs?")),
           "详情显示摘要、用途、读者和核心问题原文")
        ok(record["summary"] in detail and page.locator("#overviewDetail b, #overviewDetail img").count() == 0,
           "核验记录中的 HTML 只显示为文字")
        ok("stopped" in page.inner_text("#overviewDetail") and str(workspace) not in page.inner_text("#overview"),
           "详情显示相对路径，不暴露工作区绝对路径")
        start = page.locator("#overviewStart")
        ok(start.count() == 1, "已停审阅提供启动并打开操作")
        if start.count():
            pending = []
            ctx.route("**/api/overview/start", lambda route: pending.append(route))
            with page.expect_popup() as first_popup:
                start.click()
            empty = first_popup.value
            ok(wait_until(lambda: bool(pending), page, 5) and empty.url == "about:blank",
               "点击时同步预留新标签页，启动请求尚未完成")
            ok(page.locator("#overviewStart").is_disabled()
               and "启动中" in page.inner_text("#overviewStart"), "等待启动时显示启动中并阻止重复点击")
            pending.pop(0).fulfill(status=503, content_type="application/json",
                                   body=json.dumps({"app": "annotation-everything", "code": "overview-start-failed",
                                                    "error": "Synthetic launch failure"}))
            ok(wait_until(empty.is_closed, page, 5), "启动失败关闭预留的空白标签页")
            ok(page.locator("#overviewStart").is_enabled()
               and "重试" in page.inner_text("#overviewStartError"), "启动失败提供可重试提示并恢复操作")
            for status, code, zh, en in (
                (409, "overview-port-busy", "请释放端口后重试", "Free the port and retry"),
                (404, "overview-review-unknown", "不在本机台账里", "not in the local ledger"),
                (409, "overview-review-invalid", "目录或标识已失效", "directory or identity is invalid"),
            ):
                with page.expect_popup() as failed_popup:
                    page.click("#overviewStart")
                failed = failed_popup.value
                if not wait_until(lambda: bool(pending), page, 5):
                    ok(False, "启动请求到达：" + code)
                    failed.close()
                    continue
                pending.pop(0).fulfill(status=status, content_type="application/json",
                                       body=json.dumps({"app": "annotation-everything", "code": code,
                                                        "error": "Synthetic recovery guidance"}))
                ok(wait_until(failed.is_closed, page, 5), "失败关闭空白标签页：" + code)
                ok(page.locator("#overviewStart").is_enabled()
                   and zh in page.inner_text("#overviewStartError"), "保留具体启动恢复提示：" + code)
                page.click("#overviewBack")
                page.select_option("#localePick", "en")
                page.click("#overviewBtn")
                ok(wait_dom(page, "text => document.getElementById('overviewStartError')?.textContent.includes(text)",
                            5, arg=en, condition="启动错误的英文恢复提示"),
                   "启动恢复提示随界面切换英文：" + code)
                page.click("#overviewBack")
                page.select_option("#localePick", "zh-CN")
                page.click("#overviewBtn")
            with page.expect_popup() as second_popup:
                page.click("#overviewStart")
            opened = second_popup.value
            ok(wait_until(lambda: bool(pending), page, 5), "失败后可以再次发起启动")
            pending.pop(0).continue_()
            navigated = wait_until(lambda: opened.url.startswith("http://127.0.0.1:"), page, 10)
            owned = json.loads((Path(os.environ["AE_HOME"]) / "reviews.json").read_text())["reviews"][stopped_id]["port"]
            ok(navigated and opened.url == f"http://127.0.0.1:{owned}/"
               and wait_mode(opened, "server"), "重试成功后在专属端口的新标签页打开目标审阅")
            ok(opened.evaluate("window.opener === null"), "新审阅标签页不保留 opener")
            opened.close()
            ctx.unroute("**/api/overview/start")
        invalid = page.locator('[data-overview-group="invalid"] [data-review-id]').first
        invalid.click()
        ok("serve.py forget " in page.inner_text("#overviewDetail") and page.locator("#overviewDetail a").count() == 0,
           "失效审阅只能复制清理命令，不能打开")
        current_id = json.loads((current / "review.json").read_text())["reviewId"]
        page.click('[data-review-id="%s"]' % current_id)
        archive_available = page.locator("#overviewArchive").count() == 1
        ok(archive_available, "当前审阅详情提供归档操作")
        if not archive_available:
            return
        page.click("#overviewArchive")
        archived = wait_dom(page, "() => !document.getElementById('overviewArchiveBanner').hidden", 8,
                            condition="归档当前审阅后显示总览横幅")
        ok(archived, "归档当前审阅后总览显示已归档横幅")
        if not archived:
            return
        ok(not page.is_checked("#overviewShowArchived") and page.is_visible("#overviewHiddenArchived")
           and page.locator('[data-overview-group="archived"] [data-review-id]').count() == 0,
           "已归档分组默认隐藏，只保留可展开提示")
        page.click("#overviewExpandArchived")
        ok(page.is_checked("#overviewShowArchived")
           and page.locator('[data-overview-group="archived"] [data-review-id]').count() == 1,
           "展开提示打开开关并显示已归档审阅")
        page.uncheck("#overviewShowArchived")
        page.click("#overviewBack")
        viewer_banner = wait_dom(page, "() => !document.body.classList.contains('overview-mode') && !document.getElementById('archiveBanner').hidden", 5,
                                 condition="回到已归档审阅后显示对照页横幅")
        ok(viewer_banner and page.is_visible("#ntext"), "已归档对照页仍显示批注区和取消归档按钮")
        page.click("#archiveUnarchive")
        restored = wait_dom(page, "() => document.getElementById('archiveBanner').hidden && document.getElementById('overviewArchiveBanner').hidden", 8,
                            condition="对照页取消归档后横幅消失")
        ok(restored and not (current / ".annotation-archived").exists(), "对照页取消归档移除标记和两处横幅")
        page.click("#overviewBtn")
        page.click('[data-review-id="%s"]' % current_id)
        page.click("#overviewArchive")
        archived_again = wait_dom(page, "() => !document.getElementById('overviewArchiveBanner').hidden", 8,
                                  condition="再次归档后可在总览撤销")
        if archived_again:
            page.click("#overviewUnarchive")
        ok(archived_again and wait_dom(page, "() => document.getElementById('overviewArchiveBanner').hidden", 8,
                                      condition="总览取消归档后横幅消失"),
           "总览横幅也能取消归档")
        page.click("#overviewBack")
        returned = wait_dom(page, "() => document.getElementById('overview').hidden && !document.body.classList.contains('overview-mode')", 5,
                            condition="hashchange 后恢复当前审阅")
        ok(returned and page.is_visible("#ntext"), "返回当前审阅恢复批注界面")
        page.select_option("#localePick", "en")
        page.click("#overviewBtn")
        ok(wait_dom(page, """() => document.body.classList.contains('overview-mode')
                   && !document.getElementById('overview').hidden
                   && Array.from(document.querySelectorAll('#overviewDetail dt')).some(el => el.textContent === 'Status')
                   && document.querySelector('[data-overview-group="running"] h2')?.textContent.includes('Running')""", 5,
                   condition="hashchange 后总览详情和分组已按英文重绘"),
           "英文界面的总览标题已翻译")
        current_id = json.loads((current / "review.json").read_text())["reviewId"]
        page.click('[data-review-id="%s"]' % current_id)
        detail = page.inner_text("#overviewDetail")
        ok("(No summary)" in detail and "示例咖啡 2030 年三季度经营回顾" in detail,
           "英文无摘要提示保留中文封面原文")
        page.click('[data-review-id="%s"]' % stopped_id)
        detail = page.inner_text("#overviewDetail")
        ok(all(text in detail for text in ("Summary", "Purpose", "Audience", "Core questions", "经营委员会")),
           "英文详情翻译系统标签并保留核验记录原文")
        system_copy = page.locator(".overview-top, .overview-group h2, .overview-row-meta, "
                                   "#overviewDetail dt, #overviewDetail h3, .overview-actions").all_text_contents()
        ok(not any("\u4e00" <= char <= "\u9fff" for char in "".join(system_copy)),
           "英文总览的系统文案没有中文残留")
        artifacts = os.environ.get("AE_OVERVIEW_ARTIFACTS")
        if artifacts:
            output = Path(artifacts)
            output.mkdir(parents=True, exist_ok=True)
            # 行为断言完成后，用同一组合成审阅采集文档截图。
            # 保留真实列表与 API，只把安全用例的正文换成可读的报告样例。
            page.click("#overviewBack")
            page.wait_for_function("() => !document.body.classList.contains('overview-mode')")
            add_note(page, "请写清收入增长的对比期间（合成批注）")
            page.click("#handBtn")
            page.fill("#handMsg", "请同步核对报告和讲稿（合成提交）")
            page.click("#doHand")
            target_info = stopped / ".annotation-server.json"
            target_port = json.loads(target_info.read_text())["port"]
            target_url = f"http://127.0.0.1:{target_port}"
            response = ctx.request.post(target_url + "/api/server/stop", data={},
                                        headers={"Origin": target_url, "X-AE-Review": stopped_id,
                                                 "Content-Type": "application/json"})
            if not response.ok or not wait_until(lambda: not target_info.exists(), page, 5):
                raise RuntimeError("截图前未能停止合成目标审阅")
            archived_sample = workspace / "archived"
            build(archived_sample, name="Sample Coffee · Previous review")
            archived_id = json.loads((archived_sample / "review.json").read_text())["reviewId"]
            source_url = f"http://127.0.0.1:{port}"
            response = ctx.request.post(source_url + "/api/overview/archive",
                                        data={"reviewId": archived_id, "archived": True},
                                        headers={"Origin": source_url, "X-AE-Review": current_id,
                                                 "Content-Type": "application/json"})
            if not response.ok:
                raise RuntimeError("截图前未能归档合成样例")
            samples = (
                ("zh-CN", "zh", "示例咖啡 · 三季度经营回顾", "示例咖啡 · 成本复盘",
                 "汇总三季度经营表现，重点复核增长来源与成本变化。",
                 {"purpose": "为下一季度经营安排提供依据", "audience": "经营委员会",
                  "coreQuestions": ["收入增长由哪些因素带动？", "哪些成本需要进一步核实？"]}),
                ("en", "en", "Sample Coffee · Q3 business review", "Sample Coffee · Cost review",
                 "Review Q3 business performance, with a focus on growth drivers and changes in costs.",
                 {"purpose": "Inform next quarter's operating plan", "audience": "Operating committee",
                  "coreQuestions": ["What drove revenue growth?", "Which costs need further verification?"]}),
            )
            for locale, suffix, current_name, stopped_name, summary, request in samples:
                source_report = FIXTURE / ("report.en.html" if locale == "en" else "report.html")
                source_script = FIXTURE / ("script.en.md" if locale == "en" else "script.md")
                shutil.copyfile(source_report, report)
                record.update(summary=summary, request=request)
                record["links"]["sha256"]["report"] = hashlib.sha256(report.read_bytes()).hexdigest()
                (material / "report-record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                build(current, report=source_report, script=source_script, name=current_name, locale=locale)
                build(stopped, report=report, script=source_script, name=stopped_name, locale=locale)
                page.goto(source_url + "/")
                page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
                page.select_option("#localePick", locale)
                page.click("#overviewBtn")
                page.wait_for_function("name => document.getElementById('overviewList').textContent.includes(name)",
                                       arg=stopped_name)
                page.click('[data-review-id="%s"]' % stopped_id)
                page.wait_for_function("summary => document.getElementById('overviewDetail').textContent.includes(summary)",
                                       arg=summary)
                page.locator("#toast").wait_for(state="hidden")
                page.screenshot(path=str(output / ("overview.%s.png" % suffix)))
    finally:
        info_path = stopped / ".annotation-server.json"
        if info_path.exists():
            info = json.loads(info_path.read_text())
            target_url = f"http://127.0.0.1:{info['port']}"
            try:
                health = ctx.request.get(target_url + "/api/health").json()
                if health.get("reviewId") == stopped_id:
                    ctx.request.post(target_url + "/api/server/stop", data={},
                                     headers={"Origin": target_url, "X-AE-Review": stopped_id,
                                              "Content-Type": "application/json"})
                    wait_until(lambda: not info_path.exists(), page, 5,
                               condition="回收总览启动的合成审阅服务")
            except Exception as error:
                ok(False, "总览启动服务清理失败：%s" % error)
        ctx.close()
        stop(srv)



def receiver_binding_checks(browser, tmp: Path):
    d = tmp / "receiver-binding"
    build(d)
    subprocess.run([sys.executable, str(SCRIPTS / "wakeup.py"), "bind", str(d), "--adapter", "wait",
                    "--harness", "Claude Code", "--session", "browser-session-a"], check=True, capture_output=True)
    srv = start_server(d, free_port())
    port = json.loads((d / ".annotation-server.json").read_text())["port"]
    waiter = None
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{json.loads((d / '.annotation-server.json').read_text())['port']}/")
        wait_mode(page, "server")
        visible = page.locator("#receiver").count() > 0
        ok(visible, "页面常显接收方身份")
        if not visible:
            return
        text = page.inner_text("#receiver")
        ok("Claude Code" in text and "browser-" in text and "未等待" in text, "harness、会话短标识与未等待状态同时可见")
        page.click("#handBtn")
        ok("Claude Code" in page.inner_text("#handReceiver"), "提交前再次明确接收方")
        page.keyboard.press("Escape")
        add_note(page, "请简化这一页的标题（合成批注）")
        identity_flags = ["--harness", "Claude Code", "--session", "browser-session-a"]
        def start_waiter():
            process = subprocess.Popen([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(d),
                                        "--timeout", "30", "--interval", ".1", *identity_flags],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            CLEANUP.add(process)
            return process
        waiter = start_waiter()
        waiting = "() => document.getElementById('receiver').textContent.includes('等待中')"
        ok(wait_dom(page, waiting, 8), "真实绑定等待者启动后显示等待中")
        artifacts = Path(os.environ["AE_RECEIVER_ARTIFACTS"]) if os.environ.get("AE_RECEIVER_ARTIFACTS") else None
        if artifacts:
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / "receiver.zh.png"))
        page.select_option("#localePick", "en")
        ok("Receiver: Claude Code" in page.inner_text("#receiver") and "Waiting" in page.inner_text("#receiver"),
           "切换英文只翻译接收状态，harness 与会话身份不变")
        ok("browser-session-a" in page.get_attribute("#receiver", "title"), "完整会话标识可核对")
        if artifacts:
            page.screenshot(path=str(artifacts / "receiver.en.png"))
        page.click("#handBtn")
        page.fill("#handMsg", "Synthetic receiver batch")
        page.click("#doHand")
        out, err = waiter.communicate(timeout=15)
        CLEANUP.discard(waiter)
        ok(waiter.returncode == 0, "页面提交由绑定会话接收：" + (err if waiter.returncode else "batch 1"))
        ok(wait_dom(page, "() => document.getElementById('receiver').textContent.includes('Processing')", 8),
           "领取后显示接收方与处理中状态")
        shown = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(d), "--json"],
                               capture_output=True, text=True, check=True)
        nid = json.loads(shown.stdout)["notes"][0]["id"]
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(d), nid, *identity_flags],
                       capture_output=True, check=True)
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(d), "--seq", "1", *identity_flags],
                       capture_output=True, check=True)
        ok(wait_dom(page, "() => document.getElementById('receiver').textContent.includes('Not waiting')", 8),
           "批次收尾后保留接收方，显示未等待")
        page.select_option("#localePick", "zh-CN")
        waiter = start_waiter()
        ok(wait_dom(page, waiting, 8), "下次等待仍归原会话")
        page.click("#handBtn")
        ok("等待中" in page.inner_text("#handReceiver"), "提交窗口显示当前等待状态")
        stop(waiter)
        waiter = None
        ok(wait_dom(page, "() => document.getElementById('receiver').textContent.includes('未等待')", 8)
           and "Claude Code" in page.inner_text("#receiver"), "停止等待不解除绑定、不自动改接收方")
        ok(wait_dom(page, "() => document.getElementById('handReceiver').textContent.includes('未等待')", 4),
           "同一绑定停止等待后，已打开的提交窗口也更新接收状态")
        page.keyboard.press("Escape")
        stop(srv)
        ok(wait_mode(page, "offline", 12) and "待确认" in page.inner_text("#receiver"), "服务断开后不沿用在线状态")
        srv = start_server(d, port)
        page.reload()
        wait_mode(page, "server")
        def old_service(route):
            response = route.fetch()
            data = response.json()
            data.pop("receiver", None)
            data["features"] = [f for f in data["features"] if f != "receiver-binding"]
            route.fulfill(response=response, json=data)
        page.route("**/api/notes", old_service)
        ok(wait_dom(page, "() => document.getElementById('receiver').textContent.includes('请升级')", 8),
           "旧版服务明确提示接收方未知")
        page.unroute("**/api/notes", old_service)
        subprocess.run([sys.executable, str(SCRIPTS / "wakeup.py"), "unbind", str(d)], check=True, capture_output=True)
        stop(srv)
        srv = start_server(d, port)
        page.reload()
        wait_mode(page, "server")
        ok("未绑定接收方" in page.inner_text("#receiver"), "旧审阅未绑定时不猜测 harness")
        local = ctx.new_page()
        local.goto((d / "index.html").as_uri())
        ok("本地文件" in local.inner_text("#receiver"), "文件模式不冒充连接到任何 harness")
    finally:
        if waiter is not None and waiter.poll() is None:
            stop(waiter)
        ctx.close()
        stop(srv)


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
        poll_applied = True
        if mode == "服务模式":
            # 用本次修改独有的目录说明证明 applyServer 已应用新轮询并重绘候选；
            # 仅收到响应头还不能证明页面处理完成，超时也不能让不变量空转通过。
            description = "文字和字体校对（本次轮询已应用）"
            subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(d),
                            "--add", "writer=" + description], check=True, capture_output=True)
            poll_applied = wait_dom(
                page, "text => skillCatalog.some(s => s.name === 'writer' && s.desc === text)",
                10, arg=description, condition="轮询应用本次技能目录修改并重绘候选")
        page.keyboard.press("Enter")
        check(poll_applied and page.input_value("#nskills") == "writer", "方向键和回车选择候选")
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
        page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
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
        page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
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
        before = page.locator("#nlist [data-note]").count()
        page.click('[data-act="add"]')
        wait_card(page, before, condition="英文批注卡片出现在列表里")
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
        page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
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
        page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
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
        wait_batches(page, "第 1 批")
        page.check("#currentTaskToggle")
        ok(wait_batches(page, "全量勾稽"), "处理记录显示全量勾稽要求")
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
        # 该路由只剥掉 full-check 功能（marks 保留），徽标不会变「旧版服务」；
        # 就绪信号是首次轮询应用后徽标从「未连接」变为「已连接」，此后功能集判定完成
        ready = wait_dom(old, "() => document.getElementById('mode').textContent.includes('已连接')", 10,
                         condition="首次轮询应用（徽标从「未连接」变为「已连接」）")
        items = json.loads((d / "handoff.json").read_text())["items"]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(ready and len(items) == 1 and remaining and remaining[0].get("fullCheck") is True,
           "旧服务不能静默丢弃暂存的全量勾稽要求")
        old.click("#handBtn")
        old.click("#doHand")
        ok(old.is_visible("#hFullOld") and old.is_visible("#hand"), "旧服务拒绝新提交的全量勾稽并提示升级")
        old.keyboard.press("Escape")
        old.unroute("**/api/notes", no_full_check)
        flushed = wait_until(lambda: len(json.loads((d / "handoff.json").read_text())["items"]) == 2, old, 7)
        items = json.loads((d / "handoff.json").read_text())["items"]
        ok(flushed and items[-1].get("fullCheck") is True, "新版服务恢复后，暂存的全量勾稽提交自动补传")
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(d), "--seq", "1",
                        "--summary", "首批合成全量勾稽已核对"], capture_output=True, text=True, check=True)
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(d)], capture_output=True, text=True, check=True)
        items = json.loads((d / "handoff.json").read_text())["items"]
        ok([(item["seq"], item["status"]) for item in items] == [(1, "done"), (2, "claimed")],
           "先收尾第一批再领取第二批，一次只处理一项")
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
        # 负面观察：提交经旧服务裁决后必须仍在页面暂存；以旧服务明确拒绝的提示为事件证据
        rejected = wait_toast(old, "本地服务不支持修改全量勾稽",
                              condition="旧服务拒绝全量勾稽的提示出现（提交留在页面暂存）")
        items = json.loads((d / "handoff.json").read_text())["items"]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(rejected and items[-1].get("fullCheck") is False and remaining and remaining[0].get("fullCheck") is True,
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
        # 旧服务截短附言且未确认勾选：以「未确认」提示为裁决完成的事件证据
        rejected = wait_toast(old, "服务未确认全量勾稽",
                              condition="旧服务未确认全量勾稽的提示出现（提交留在页面暂存）")
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(rejected and item.get("fullCheck") is False and all(word in item.get("message", "") for word in ("全量勾稽", "报告和讲稿", "有依据", "不确定"))
           and remaining and remaining[0].get("fullCheck") is True and "截短附言" in old.inner_text("#toast"),
           "提交时旧服务未确认勾选：保留待补传要求")
        old.unroute("**/api/handoff", legacy_post)
        flushed = wait_until(lambda: json.loads((d / "handoff.json").read_text())["items"][-1].get("fullCheck") is True, old, 7)
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        ok(flushed and item.get("userMessage") == long_message, "新版服务确认后才清除待补传要求，完整保留长附言")

        old.click("#handBtn")
        old.uncheck("#hFullCheck")
        old.route("**/api/handoff", legacy_post)
        # 先证明旧服务确实收到并改写了这次提交，再等这次补传处理结束；
        # 监听先于 click 注册（action 在注册后执行），毫秒级完成的本地应答不会漏事件
        responded = wait_response(old, lambda r: r.request.method == "POST" and r.url.endswith("/api/handoff"), 10,
                                  condition="本次提交的 POST /api/handoff 响应到达（旧服务被咨询）",
                                  action=lambda: old.click("#doHand"))
        settled = wait_dom(old, "() => flushing === null", 5,
                           condition="本次提交的补传处理已结束（不接受上一次的旧提示）")
        item = json.loads((d / "handoff.json").read_text())["items"][-1]
        remaining = old.evaluate("key => JSON.parse(localStorage.getItem(key) || '[]')", key)
        ok(responded and settled and item.get("fullCheck") is True and remaining and remaining[0].get("fullCheck") is False,
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
    page.locator("#sidebar").wait_for(state="hidden")
    page.click("#sideToggle")
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
    if page.locator("#panelBody").is_visible():
        page.click("#panelToggle")
    page.click("#seeList")
    ok(page.locator("#panelBody").is_visible() and page.locator("#nlist").is_visible(),
       f"{mode}：点「本页批注」展开底部批注列表")
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
    """关掉挡在页头上的弹窗：先把焦点移出输入框，再用 Esc（快捷键本身保持不变）。

    Esc 处理同步关闭弹窗，随后的断言按各自条件等待，不需要垫固定等待。
    """
    page.evaluate("() => { if (document.activeElement) document.activeElement.blur() }")
    page.keyboard.press("Escape")


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
    ok(zh_counts == ["共 0 条", "共 1 条", "共 2 条"], f"file:// 中文计数 0/1/2（实际 {zh_counts}）")
    page.fill("#ntext", "请改标题")
    page.select_option("#ntarget", "报告")
    page.click("#addBtn")
    # 文件模式：mutate 同步写 localStorage，以「批注已入存储」为添加动作的可观察结果
    stored = wait_until(lambda: page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')) || []"), page, 5,
                        condition="批注写入本地存储")
    notes = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(stored and len(notes) == 1 and notes[0]["text"] == "请改标题" and notes[0]["target"] == "报告"
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
        sp.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
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
        # 保留的固定观察窗口（0.7s，覆盖页面 400ms 的草稿防抖定时器）：证明切换语言
        # 不产生任何写请求是负面观察，窗口结束后核对请求计数（原因与预算见 docs/verify/browser-waits.md）
        sp.wait_for_timeout(700)
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
    page.reload()   # 文件模式初始化同步渲染（无 await），goto / reload 返回时 DOM 已就绪

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
    page.select_option("#localePick", "zh-CN")   # 界面语言切换同步重绘，无需等待
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
    landed = wait_until(lambda: page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')) || []"), page, 5,
                        condition="导入的批注写入本地存储")
    landed = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(landed and len(landed) == 2 and [n["target"] for n in landed] == ["报告", "讲稿"]
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
    open_import()
    page.fill("#impText", r.stdout)
    page.click("#doImp")
    back = wait_until(lambda: page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')) || []"), page, 5,
                      condition="导入的批注写入本地存储")
    back = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    ok(back and len(back) == 2 and [n["target"] for n in back] == ["报告", "讲稿"]
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
    open_import()
    page.fill("#impText", legacy)
    page.click("#doImp")
    ok(wait_until(lambda: len(page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')) || []")) == 2, page, 5,
                  condition="旧格式导入的 2 条批注写入本地存储"), "旧中文格式能导进页面")
    page.fill("#impText", "完全不是批注导出的文字")
    page.click("#doImp")
    ok(wait_toast(page, "无法识别", condition="认不出文字的提示出现"), f"认不出的文字给出明确提示（{page.inner_text('#toast')[:30]}）")
    page.fill("#impText", "<!-- annotation-everything export v9 lang=en -->\n")
    page.click("#doImp")
    ok(wait_toast(page, "v9", condition="不认识格式的提示出现"), "不认识的格式版本也给出明确提示")
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
        page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
        add_note(page, "把口径改成同比")
        go_page(page, 4)
        add_note(page, "表格加上单位")
        # 服务态徽标回到「已连接」= 这两条批注的写请求都已确认（不再是 held for sending）
        ok(wait_until(lambda: page.inner_text("#mode") == "● Connected to the local service", page, 8,
                      condition="两条批注的写请求均已确认（徽标回到已连接）"),
           f"英文服务态徽标（实际 {page.inner_text('#mode')!r}）")
        ok(page.locator("#handBtn span").first.inner_text() == "Hand to the agent for revision", "英文改稿提交按钮")
        page.click("#handBtn")
        ok(page.inner_text("#handTitle") == "Hand to the agent for revision", "英文提交弹窗标题")
        ok(page.inner_text("#doHandLabel") == "Hand to the agent for revision", "英文提交按钮（弹窗内）")
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
        ok(wait_until(lambda: "Agent claimed batch 1" in page.inner_text("#handState"), page, 8),
           f"接收后英文徽标（实际 {page.inner_text('#handState')!r}）")
        ok("Chat receipt unconfirmed" in page.inner_text("#agentDelivery"), "英文区分后台认领与聊天接手回执")

        delivery_cases = page.evaluate("""async () => {
          const original = await getNotes(), result = [];
          try {
            for (const [seq, status, event] of [[1, "claimed", "handoff"],
                [2, "pending", "handoff"], [1, "review", "handoff"],
                [1, "reviewing", "review"]]) {
              applyServer({...original,
                handoff: {...original.handoff, latest: {...original.handoff.latest, seq, status}},
                agentDelivery: {adapter: "codex-desktop", seq: 1, event, state: "received"}});
              result.push({receipt: document.getElementById("agentDelivery").textContent,
                state: document.getElementById("handState").textContent});
            }
          } finally { applyServer(original); }
          return result;
        }""")
        ok(delivery_cases[0]["receipt"] == "Chat receipt confirmed", "同批同阶段的聊天回执可确认接手")
        ok("unconfirmed" in delivery_cases[1]["receipt"], "新批次不能沿用上一批的接手回执")
        ok("unconfirmed" in delivery_cases[2]["receipt"], "请求复核不能沿用初改的接手回执")
        ok(delivery_cases[3]["receipt"] == "Chat receipt confirmed"
           and "claimed review" in delivery_cases[3]["state"], "复核回执独立确认，认领状态只表示已认领")

        page.keyboard.press("j")
        wait_batches(page, "Batch 1")
        page.check("#currentTaskToggle")
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
        page.select_option("#localePick", "zh-CN")   # 界面语言切换同步重绘；后续断言按可观察状态核对
        ok(len(json.loads((d / "handoff.json").read_text(encoding="utf-8"))["items"]) == len(before),
           "切换语言不重新提交")
        ok(sum(1 for method, _url in net if method == "POST") == posts_after_submit,
           "切换语言没有新的写请求")
        ok(page.is_checked("#hFullCheck") and page.is_checked("#hSub")
           and page.is_checked('input[name="htarget"][value="review"]'), "切换语言不改变已选选项")
        ok(page.inner_text("#handTitle") == "交给 Agent 改稿", "切换后弹窗标题回到中文")
        ok(page.inner_text("#hFullRow") == "全量勾稽（检查全文前后一致性）", "切换后全量勾稽选项回到中文")
        ok(page.inner_text("#mode") == "● 已连接本地服务", "切换后服务态徽标回到中文")
        ok("第 1 批" in page.inner_text("#handState"), "切换后提交状态徽标回到中文")
        ok("还没交给 Agent" not in page.inner_text("#resume") and "第 1 批" in page.inner_text("#resume"),
           f"切换后恢复提示回到中文（实际 {page.inner_text('#resume')!r}）")
        # 关掉弹窗再切语言：提交按钮是常驻元素，文字也要跟上（不能留着上一种语言）
        page.keyboard.press("Escape")
        for pick, want in (("en", "Hand to the agent for revision"), ("zh-CN", "交给 Agent 改稿")):
            page.select_option("#localePick", pick)
            ok(wait_dom(page, "w => document.getElementById('doHandLabel').textContent === w", 5, arg=want,
                        condition=f"提交按钮文字变为「{want}」"),
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
        ok(page.inner_text("#doHandLabel") == "Ask for the review", "英文请求复核按钮")
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
        wait_mode(opage, "old", condition="旧服务徽标出现（首次轮询按功能集判定）")
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


def finalize_all_checks(browser, tmp: Path):
    """#132：只通过对照页确认定稿；合成状态经真实存储接口准备。"""
    def seed(directory, *, active=False):
        code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import notes_store as store
d = Path(sys.argv[2])
for state in (['drafted', 'review', 'claimed', 'reviewing'] if sys.argv[3] == 'active' else ['drafted', 'review']):
    nid = store.add(d, {'page': 1, 'text': 'Synthetic draft'})[1]['id']
    seq = store.handoff_submit(d, '', target='review')[1]['seq']
    store.handoff_claim(d, seq=seq)
    store.set_status(d, nid, 'resolved', by='Synthetic', resolution='Synthetic change')
    if state != 'claimed':
        store.handoff_done(d, seq=seq)
    if state in ('review', 'reviewing'):
        store.handoff_request_review(d, seq)
    if state == 'reviewing':
        store.handoff_claim(d, seq=seq, stage='review')
"""
        subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(directory),
                        "active" if active else "ready"], check=True, capture_output=True)

    for locale in ("zh-CN", "en"):
        d = tmp / ("finalize_all_" + locale)
        build(d, locale=locale, name="Synthetic finalization")
        seed(d, active=locale == "zh-CN")
        port = free_port()
        server = start_server(d, port)
        ctx = browser.new_context()
        try:
            page = ctx.new_page()
            base = f"http://127.0.0.1:{port}"
            posts = []
            page.on("request", lambda req: posts.append(req.url) if req.method == "POST" and req.url.endswith("/api/handoff/finalize-all") else None)
            page.goto(base)
            button = page.locator("#finalizeAllBtn")
            button.wait_for(state="visible")
            label = "全部定稿（2 条）" if locale == "zh-CN" else "Finalize all (2)"
            ok(page.get_by_role("button", name=label, exact=True).count() == 1,
               f"全部定稿按钮显示跨批条数并有无障碍名称（{locale}）")
            artifacts = os.environ.get("AE_FINALIZE_ARTIFACTS")
            if artifacts:
                output = Path(artifacts)
                output.mkdir(parents=True, exist_ok=True)
                capture_screenshot(page, output / ("finalize-all." + ("zh" if locale == "zh-CN" else "en") + ".png"))
            before = page.request.get(base + "/api/notes").json()
            confirmations = []

            def cancel(dialog):
                confirmations.append(dialog.message)
                dialog.dismiss()

            page.once("dialog", cancel)
            button.focus()
            button.press("Enter")
            page.wait_for_function("() => !document.getElementById('finalizeAllBtn').disabled")
            after = page.request.get(base + "/api/notes").json()
            ok(len(confirmations) == 1 and not posts and before["notes"] == after["notes"]
               and before["handoff"] == after["handoff"], f"键盘打开一次确认，取消不改变状态（{locale}）")
            text = confirmations[0] if confirmations else ""
            ok(("第 1 批（1 条）" in text and "第 2 批（1 条）" in text and "跳过 2 条" in text)
               if locale == "zh-CN" else ("Batch 1 (1)" in text and "Batch 2 (1)" in text and "Drafts to skip: 0" in text),
               f"确认列出将定稿条数、批次和跳过条数（{locale}）")
            page.once("dialog", lambda dialog: (confirmations.append(dialog.message), dialog.accept()))
            button.click()
            expected = "已定稿 2 条初改，完成 2 批；跳过 2 条。" if locale == "zh-CN" else "Drafts finalized: 2; batches completed: 2; skipped: 0."
            ok(wait_dom(page, "expected => document.getElementById('finalizeAllInfo').textContent.includes(expected)", 8, arg=expected),
               f"完成提示以实际定稿和跳过条数为准（{locale}）")
            ok(len(confirmations) == 2 and len(posts) == 1 and not button.is_visible(),
               f"确认一次写入，完成后按钮和计数自动更新（{locale}）")
            actual = page.request.get(base + "/api/notes").json()
            ok(sum(note["status"] == "resolved" for note in actual["notes"]) == 2,
               f"跨批初改通过服务落盘为已处理（{locale}）")
            if locale == "zh-CN":
                info = page.inner_text("#finalizeAllInfo")
                ok("正在处理" in info and "正在复核" in info, "部分成功说明处理中与复核中的跳过原因")
                page.select_option("#localePick", "en")
                ok("Drafts finalized: 2" in page.inner_text("#finalizeAllInfo")
                   and "being reviewed" in page.inner_text("#finalizeAllInfo"), "切换语言后结果与跳过原因按当前语言显示")
                continue

            # 确认框已打开时接收其中一批复核：仍只发送一次写请求。
            seed(d)
            button.wait_for(state="visible")
            preview = page.request.get(base + "/api/notes").json()["handoff"]["finalizable"]
            review_seq = preview["batches"][-1]["seq"]

            def claim_then_accept(dialog):
                result = notes_cli("claim", str(d), "--stage", "review", "--seq", str(review_seq))
                if result.returncode:
                    raise AssertionError(result.stderr)
                dialog.accept()

            page.once("dialog", claim_then_accept)
            button.click()
            ok(wait_dom(page, "() => document.getElementById('finalizeAllInfo').textContent.includes('Drafts finalized: 1; batches completed: 1; skipped: 1.')", 8),
               "确认期间被接收复核的批次跳过，部分成功不显示全部成功")
            ok("being reviewed" in page.inner_text("#finalizeAllInfo"), "竞态跳过结果说明正在复核")

            # 功能协商：旧服务即使还有初改，也不会出现可点击的全部定稿按钮。
            old = ctx.new_page()

            def legacy(route):
                response = route.fetch()
                doc = response.json()
                doc["features"] = [feature for feature in doc.get("features", []) if feature != "finalize-all"]
                doc.get("handoff", {}).pop("finalizable", None)
                route.fulfill(response=response, json=doc)

            old.route("**/api/notes", legacy)
            old.goto(base)
            ok(wait_dom(old, "() => document.getElementById('finalizeAllInfo').textContent.includes('restart')", 8)
               and not old.locator("#finalizeAllBtn").is_visible(), "旧服务隐藏按钮并说明更新、重启与刷新方式")
            old.close()

            snapshot = page.request.get(base + "/api/notes").json()
            file_page = ctx.new_page()
            file_page.add_init_script("localStorage.setItem(" + json.dumps("ae." + snapshot["reviewId"] + ".notes") + ", "
                                      + json.dumps(json.dumps(snapshot["notes"])) + ");")
            file_page.goto((d / "index.html").as_uri())
            ok(wait_dom(file_page, "() => document.getElementById('finalizeAllInfo').textContent.includes('File mode')", 5)
               and not file_page.locator("#finalizeAllBtn").is_visible(), "文件模式说明需启动本地服务，不改变状态")
            file_page.close()
            page.route("**/api/notes", lambda route: route.abort())
            ok(wait_dom(page, "() => document.getElementById('finalizeAllInfo').textContent.includes('requires the local service')", 8)
               and not button.is_visible(), "断线隐藏定稿入口，说明启动服务与重连方式")
            ok(page.request.get(base + "/api/notes").json()["notes"] == snapshot["notes"],
               "旧服务、文件模式与断线均未改变批注")
        finally:
            ctx.close()
            stop(server)


def current_task_controls_checks(browser, tmp: Path):
    """当前任务详情通过界面展开、收起；语言与轮询不会重置用户选择。"""
    d = tmp / "current_task_controls"
    d.mkdir()
    build(d, locale="en", name="current-task-controls")
    port = free_port()
    server = start_server(d, port)
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        ok(wait_mode(page, "server"), "当前任务：连接本地服务")
        page.keyboard.press("j")
        ok(wait_dom(page, JS_BATCHES_ON, 5), "当前任务：无提交时仍可打开处理记录")
        ok(not page.is_visible("#currentTaskToggle"), "当前任务：无批次时没有无效详情开关")
        page.locator("#batches [data-close]").click()
        add_note(page, "synthetic current-task annotation")
        page.click("#handBtn")
        page.fill("#handMsg", "synthetic current-task message")
        page.click("#doHand")
        ok(wait_until(lambda: "batch 1" in page.inner_text("#handState"), page, 8),
           "当前任务：第一批提交成功")
        page.keyboard.press("j")
        ok(wait_batches(page, "Batch 1"), "当前任务：处理记录显示最新批次")
        toggle = page.get_by_role("switch", name="Current task details", exact=True)
        exists = toggle.count() == 1
        ok(exists, "当前任务：提供可访问的详情开关")
        if not exists:
            return
        ok(not toggle.is_checked() and "synthetic current-task message" not in page.inner_text("#batchList"),
           "当前任务：详情默认收起")
        toggle.check()
        ok("Note: synthetic current-task message" in page.inner_text("#batchList")
           and "Asks for:" in page.inner_text("#batchList"), "当前任务：展开显示附言与要求")
        before = json.loads((d / "handoff.json").read_text())
        page.select_option("#localePick", "zh-CN")
        ok(page.get_by_role("switch", name="当前任务详情", exact=True).is_checked()
           and "附言：synthetic current-task message" in page.inner_text("#batchList"),
           "当前任务：切到中文保留展开与原始附言")
        ok(json.loads((d / "handoff.json").read_text()) == before, "当前任务：详情浏览与切换语言不改提交数据")
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(d)],
                       check=True, capture_output=True)
        ok(wait_batches(page, "处理中") and page.is_checked("#currentTaskToggle"),
           "当前任务：认领状态轮询保留展开")
        page.uncheck("#currentTaskToggle")
        page.select_option("#localePick", "en")
        ok(not page.is_checked("#currentTaskToggle")
           and "synthetic current-task message" not in page.inner_text("#batchList"),
           "当前任务：收起与语言切换保留选择")
        page.check("#currentTaskToggle")
        page.locator("#batches [data-close]").click()
        page.keyboard.press("j")
        ok(page.is_checked("#currentTaskToggle"), "当前任务：关闭再打开处理记录保留展开")
        page.focus("#currentTaskToggle")
        page.keyboard.press("Escape")
        ok(not page.is_visible("#batches"), "当前任务：开关有焦点时 Esc 仍可关闭面板")
        page.keyboard.press("j")
        wait_batches(page, "Batch 1")
        page.locator("#batches [data-close]").click()
        add_note(page, "synthetic second-batch annotation")
        page.click("#handBtn")
        page.fill("#handMsg", "synthetic second-batch message")
        page.click("#doHand")
        ok(wait_until(lambda: "batch 2" in page.inner_text("#handState"), page, 8), "当前任务：创建新批次")
        page.keyboard.press("j")
        ok(wait_batches(page, "Batch 2") and not page.is_checked("#currentTaskToggle")
           and "synthetic second-batch message" not in page.inner_text("#batchList")
           and "synthetic current-task message" in page.inner_text("#batchList"),
           "当前任务：新的一批默认收起，旧批次详情仍可查看")
        page.check("#currentTaskToggle")
        artifacts = os.environ.get("AE_BATCH_CONTROLS_ARTIFACTS")
        if artifacts:
            output = Path(artifacts)
            output.mkdir(parents=True, exist_ok=True)
            page.locator("#toast").wait_for(state="hidden")
            page.screenshot(path=str(output / "batch-controls.en.png"))
            page.select_option("#localePick", "zh-CN")
            page.screenshot(path=str(output / "batch-controls.zh.png"))
            page.select_option("#localePick", "en")

        requests = []
        page.on("request", lambda req: requests.append((req.method, req.url)))
        stop_requests = lambda: [r for r in requests if r[0] == "POST" and r[1].endswith("/api/server/stop")]
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name="Stop local service", exact=True).click()
        ok(server.poll() is None and not stop_requests(), "停止服务：取消确认不发送停止请求")

        def legacy_service(route):
            response = route.fetch()
            body = response.json()
            body["features"] = [f for f in body.get("features", []) if f != "stop-service"]
            route.fulfill(response=response, json=body)

        page.route("**/api/notes", legacy_service)
        ok(wait_until(lambda: "cannot be stopped from the page" in page.inner_text("#stopServiceInfo"),
                      page, 8), "停止服务：旧服务显示升级指引")
        page.get_by_role("button", name="Stop local service", exact=True).click()
        ok(wait_toast(page, "run the new serve.py") and not stop_requests() and server.poll() is None,
           "停止服务：旧服务不发送不支持的停止请求")
        page.unroute("**/api/notes", legacy_service)
        ok(wait_until(lambda: "cannot be stopped from the page" not in page.inner_text("#stopServiceInfo"),
                      page, 8), "停止服务：恢复新服务能力")

        def wrong_review(route):
            response = route.fetch()
            body = response.json()
            body["reviewId"] = "0123456789abcdef"
            route.fulfill(response=response, json=body)

        page.route("**/api/notes", wrong_review)
        ok(wait_mode(page, "mismatch"), "停止服务：检测服务审阅不匹配")
        page.get_by_role("button", name="Stop local service", exact=True).click()
        ok(wait_toast(page, "not connected to its review") and not stop_requests() and server.poll() is None,
           "停止服务：不会停止不匹配的服务")
        page.unroute("**/api/notes", wrong_review)
        ok(wait_mode(page, "server"), "停止服务：恢复本审阅连接")
        page.locator("#batches [data-close]").click()
        pending_saves = []

        def reject_save(route):
            if route.request.method == "POST":
                pending_saves.append(route)
            else:
                route.continue_()

        page.route("**/api/notes", reject_save)
        add_note(page, "synthetic saved before stop")
        ok(wait_until(lambda: "held for sending" in page.inner_text("#mode"), page, 5),
           "停止服务：等待响应的改动仍在待补传队列")
        page.keyboard.press("j")
        wait_batches(page, "Batch 2")

        def stop_after_failed_save(status):
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Stop local service", exact=True).click()
            if not wait_until(lambda: bool(pending_saves), page, 5,
                              condition=f"停止操作等待补传响应（{status}）"):
                return False
            waiting = page.is_disabled("#stopServiceBtn")
            pending_saves.pop(0).fulfill(status=status, json={"app": "annotation-everything", "code": "bad-note",
                                                            "error": "synthetic save failure"})
            settled = wait_until(lambda: page.is_enabled("#stopServiceBtn")
                                 and "Some changes have not been saved" in page.inner_text("#toast"),
                                 page, 5, condition=f"本次停止操作因保存失败结束（{status}）")
            return waiting and settled and not stop_requests() and server.poll() is None

        ok(stop_after_failed_save(503), "停止服务：补传 5xx 失败时保持服务运行")
        ok(stop_after_failed_save(400), "停止服务：明确拒绝保存时也不停止")
        page.unroute("**/api/notes", reject_save)
        page.locator("#batches [data-close]").click()
        add_note(page, "synthetic saved before stop")
        page.keyboard.press("j")
        wait_batches(page, "Batch 2")
        other_tab = ctx.new_page()
        other_tab.goto(f"http://127.0.0.1:{port}/")
        ok(wait_mode(other_tab, "server"), "停止服务：同一审阅的第二个标签页已连接")
        held_saves = []

        def hold_other_save(route):
            if route.request.method == "POST":
                held_saves.append(route)
            else:
                route.continue_()

        other_tab.route("**/api/notes", hold_other_save)
        add_note(other_tab, "synthetic rejected in another tab")
        ok(wait_until(lambda: bool(held_saves), other_tab, 5), "停止服务：另一标签页补传正在等待响应")
        page.bring_to_front()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Stop local service", exact=True).click()
        ok(page.get_by_role("button", name="Saving and stopping…", exact=True).is_disabled()
           and not stop_requests(), "停止服务：等待另一标签页的补传完成")
        held_saves[0].fulfill(status=400, json={"app": "annotation-everything", "code": "bad-note",
                                              "error": "synthetic rejection in another tab"})
        cross_tab_blocked = (wait_toast(page, "Some changes have not been saved", seconds=5)
                             and server.poll() is None and not stop_requests())
        ok(cross_tab_blocked, "停止服务：另一标签页保存被拒时也保持服务运行")
        if not cross_tab_blocked:
            return
        other_tab.unroute("**/api/notes", hold_other_save)
        held_reads = []
        read_count = 0

        def hold_final_read(route):
            nonlocal read_count
            if route.request.method == "GET":
                read_count += 1
                if read_count == 2:
                    held_reads.append((route, route.fetch()))
                    return
            route.continue_()

        # 补传结束后的身份预检也可能让出执行；在响应回来前让另一标签页保存被拒。
        page.route("**/api/notes", hold_final_read)
        held_saves.clear()
        other_tab.route("**/api/notes", hold_other_save)
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Stop local service", exact=True).click()
        ok(wait_until(lambda: bool(held_reads), page, 5), "停止服务：最终身份预检正在等待响应")
        other_tab.bring_to_front()
        add_note(other_tab, "synthetic rejection during final service check")
        ok(wait_until(lambda: bool(held_saves), other_tab, 5), "停止服务：最终预检期间另一标签页正在补传")
        held_saves[0].fulfill(status=400, json={"app": "annotation-everything", "code": "bad-note",
                                              "error": "synthetic rejection during final service check"})
        ok(wait_toast(other_tab, "synthetic rejection during final service check", seconds=5),
           "停止服务：最终预检期间另一标签页保存被拒")
        page.bring_to_front()
        held_reads[0][0].fulfill(response=held_reads[0][1])
        final_read_blocked = (wait_toast(page, "Some changes have not been saved", seconds=5)
                              and server.poll() is None and not stop_requests())
        ok(final_read_blocked, "停止服务：最终预检期间保存被拒仍保持服务运行")
        page.unroute("**/api/notes", hold_final_read)
        other_tab.unroute("**/api/notes", hold_other_save)
        if not final_read_blocked:
            return
        page.bring_to_front()
        saved_before_stop = []
        page.on("request", lambda req: saved_before_stop.append(
            any(n["text"] == "synthetic saved before stop" for n in disk_notes(d)))
            if req.method == "POST" and req.url.endswith("/api/server/stop") else None)
        confirmations = []
        page.once("dialog", lambda dialog: (confirmations.append(dialog.message), dialog.accept()))
        page.select_option("#localePick", "zh-CN")
        status_before = json.loads(subprocess.run([sys.executable, str(SCRIPTS / "serve.py"), "status", str(d)],
                                                 check=True, capture_output=True, text=True).stdout)
        history_before = json.loads((d / "handoff.json").read_text())
        page.get_by_role("button", name="停止本地服务", exact=True).click()
        ok(wait_until(lambda: server.poll() is not None, page, 8), "停止服务：真实服务进程正常退出")
        ok(server.returncode == 0 and not (d / ".annotation-server.json").exists(),
           "停止服务：退出码为零，运行状态文件清理")
        ok(saved_before_stop == [True] and len(stop_requests()) == 1,
           "停止服务：确认改动已落盘后只发送一次停止请求")
        ok(len(confirmations) == 1 and "所有标签页" in confirmations[0] and "Agent 继续运行" in confirmations[0],
           "停止服务：中文确认说明停止范围、保留数据与 Agent 行为")
        other_tab.bring_to_front()
        ok(wait_mode(other_tab, "offline"), "停止服务：同一审阅其他标签页转离线")
        page.bring_to_front()
        ok(wait_mode(page, "offline") and wait_until(lambda: "serve.py" in page.inner_text("#srvBar"), page, 8),
           "停止服务：页面转离线并给出重启命令")
        status_after = subprocess.run([sys.executable, str(SCRIPTS / "serve.py"), "status", str(d)],
                                      capture_output=True, text=True)
        ok(status_after.returncode == 3 and isinstance(status_before["owned_port"], int)
           and json.loads(status_after.stdout)["owned_port"] == status_before["owned_port"],
           "停止服务：专属端口仍保留")
        stop(server)
        server = start_server(d, port)
        page.reload()
        ok(wait_mode(page, "server")
           and "synthetic saved before stop" in page.inner_text("#nlist")
           and json.loads((d / "handoff.json").read_text()) == history_before,
           "停止服务：重启后批注与提交记录恢复")
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
            page.wait_for_function("() => document.getElementById('mode').classList.contains('server')")
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
                check(wait_until(lambda: "maximum 20000 characters" in page.inner_text("#toast"), page, 5)
                      and "Changes remain in the browser" not in page.inner_text("#toast")
                      and "text is too long" in page.inner_text("#toast"),
                      "字段拒绝提示具体字段与上限，不虚称改动已保留")
                error_detail = '<img src=x onerror="window.__errorExecuted=true"> synthetic refusal'
                route_path = "**/api/notes/" + note_id

                def reject_with_detail(route):
                    route.fulfill(status=400, content_type="application/json",
                                  body=json.dumps({"app": "annotation-everything", "code": "bad-note",
                                                   "error": error_detail}))

                page.route(route_path, reject_with_detail)
                try:
                    page.locator('#nlist [data-act="edit"]').first.click()
                    page.fill("#etext", "synthetic rejected edit")
                    page.locator('#nlist [data-act="save"]').first.click()
                    check(wait_until(lambda: error_detail in page.inner_text("#toast"), page, 5)
                          and page.locator("#toast img").count() == 0
                          and not page.evaluate("Boolean(window.__errorExecuted)"),
                          "未知业务拒绝保留具体原因，HTML 仅作文本展示")
                finally:
                    page.unroute(route_path, reject_with_detail)
            if output:
                page.locator("#toast").wait_for(state="hidden")
                page.set_viewport_size({"width": 1600, "height": 1100})
                page.locator("#nlist").scroll_into_view_if_needed()
                page.screenshot(path=str(output / "viewer.png"))
                page.keyboard.press("n")
                page.screenshot(path=str(output / "notes-export.png"))
                page.keyboard.press("Escape")
                page.set_viewport_size({"width": 1440, "height": 1000})

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


def server_import_export_fixture(tmp: Path) -> str:
    """server-mode 场景声明的准备步骤：用真实 CLI 从独立合成审阅导出导入用 Markdown。

    导入检查需要一份带已处理状态的中文导出；经 build_viewer 构建、notes_store 落盘、
    notes.py resolve 标已处理、notes.py export --all 导出，全程产品真实路径，
    不用测试捷径造格式。
    """
    source_notes = [
        {"id": "e2eimp000000001", "page": 3, "title": "结论与收入", "target": "报告",
         "text": "file 模式批注", "quote": "", "quoteFrom": "", "pin": {"x": 50, "y": 50},
         "status": "open", "at": "2026-10-27T10:00:00Z"},
        {"id": "e2eimp000000002", "page": 4, "title": "收入结构", "target": "报告",
         "text": "旧版批注甲", "quote": "", "quoteFrom": "", "pin": {"x": 20, "y": 30},
         "status": "open", "at": "2026-10-27T10:01:00Z"},
    ]
    source = tmp / "server_import_source"
    build(source)
    write_disk_notes(source, source_notes)
    resolved = notes_cli("resolve", str(source), source_notes[0]["id"],
                         "--note", "已处理", "--by", "claude")
    if resolved.returncode != 0:
        raise RuntimeError("导入用 Markdown 的准备失败：" + (resolved.stderr or resolved.stdout))
    exported = notes_cli("export", str(source), "--all")
    if exported.returncode != 0:
        raise RuntimeError("导入用 Markdown 的准备失败：" + (exported.stderr or exported.stdout))
    return exported.stdout


def file_mode_checks(browser, tmp: Path):
    """场景 file-mode：file:// 退回模式（详见 e2e_scenarios 注册表）。"""
    fdir = tmp / "file_mode"
    build(fdir)
    ctx = browser.new_context()
    page = ctx.new_page()
    url = (fdir / "index.html").as_uri()
    page.goto(url)
    ok("本地文件模式" in page.inner_text("#mode"), "file:// 打开显示「本地文件模式」")
    go_page(page, 3)
    add_note(page, "file 模式批注", pin=(0.5, 0.5))
    page.reload()   # 文件模式初始化同步渲染，返回时列表与提示条已就绪
    ok("file 模式批注" in page.inner_text("#nlist"), "file:// 刷新后批注仍在（localStorage）")
    ok("上次批注到第 3 页" in page.inner_text("#resume"), "file:// 打开时提示上次批注到哪一页")
    page2 = ctx.new_page()
    page2.goto(url)
    go_page(page2, 4)
    add_note(page2, "第二个标签页的批注")
    go_page(page, 5)
    add_note(page, "第一个标签页又加一条")
    texts = [n["text"] for n in page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")]
    ok({"file 模式批注", "第二个标签页的批注", "第一个标签页又加一条"} <= set(texts), "两个 file:// 标签页各自添加，批注都在")
    page2.close()
    page.evaluate(f"() => {STAGE}.insertAdjacentHTML('beforeend', '<img src=\"x:y\" onerror=\"window.__p_file=1\">')")
    # 保留的固定观察窗口（0.5s）：给无效 src 的 img 错误事件留出发火机会，
    # 证明 CSP 拦截的是「内联处理器不执行」这一负面行为（原因与预算见 docs/verify/browser-waits.md）
    page.wait_for_timeout(500)
    ok(page.evaluate("window.__p_file") is None, "file:// 模式下 CSP 拦住注入的内联事件")
    legacy = [{"id": "old1", "page": 4, "title": "收入结构", "target": "报告", "text": "旧版批注甲",
               "quote": "", "quoteFrom": "", "pin": {"x": 20, "y": 30}, "at": "2026-10-27T01:00:00Z"},
              {"id": "old2", "page": 5, "title": "成本", "target": "讲稿", "text": "旧版批注乙",
               "quote": "", "quoteFrom": "", "pin": None, "at": "2026-10-27T01:01:00Z"}]
    page.evaluate("v => localStorage.setItem('v7viewer.notes.viewer_r2', JSON.stringify(v))", legacy)
    page.reload()
    ok(page.is_visible("#legacyBtn") and "2" in page.inner_text("#legacyBtn"), "发现旧版对照页的 2 条批注，显示导入按钮")
    page.click("#legacyBtn")
    page.click("[data-found='0']")
    imported = wait_until(lambda: page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')).length") == 5,
                          page, 5, condition="旧批注导入本地存储")
    n = page.evaluate("JSON.parse(localStorage.getItem('ae.' + RID + '.notes')).length")
    ok(imported and n == 5, f"旧批注导入后共 5 条（实际 {n}）")
    ok(not page.is_visible("#legacyBtn"), "导入后不再提示旧批注")
    page.keyboard.press("Escape")
    go_page(page, 3)
    page.evaluate("document.querySelector('[data-note] [data-act=\"resolve\"]')?.click()")   # 同步改写列表
    page.click("#handBtn")
    ok(wait_toast(page, "本地文件模式连不到 Agent", condition="文件模式提交的退回提示出现"),
       "file:// 模式点「交给 Agent」退回为复制全部并提示")
    md = page.evaluate("exportText()")
    ok("【已处理】file 模式批注" in md and "## 第 4 页" in md, "导出 Markdown 含页码与已处理标记")
    ctx.close()




def css_fidelity_checks(browser, tmp: Path):
    """场景 css-fidelity：报告样式保真（详见 e2e_scenarios 注册表）。"""
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




def docx_review_checks(browser, tmp: Path):
    """场景 docx-review：DOCX 直接审阅（详见 e2e_scenarios 注册表）。

    只用现场合成的样例文档（仓库不放真实材料）：4 页 A4 纵向 + 1 页横向，
    覆盖 #135 的浏览器验收——原分页显示、verified 横幅与限制清单、正文可拖选、
    点标注与快捷删除在 DOCX 页上可用、选中原生修订时提示不能自动回写。
    """
    sys.path.insert(0, str(ROOT / "fixtures" / "docx"))
    import make_sample as ms
    d = tmp / "docx_review"
    d.mkdir()
    build(d, ms.build_docx(d / "sample.docx"), None)
    meta = json.loads((d / "review.json").read_text(encoding="utf-8"))
    ok(meta["format"] == {"report": "docx", "version": 1}, "review.json 标记 DOCX 格式版本 1")
    ok(meta["docx"]["pagination"] == "verified" and len(meta["pages"]) == 5,
       "分页可信度 verified，共 5 页")
    ok(meta["pages"][0]["w"] < meta["pages"][0]["h"] and meta["pages"][4]["w"] > meta["pages"][4]["h"],
       "第 1 页纵向、第 5 页横向的页面尺寸进了 review.json")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto((d / "index.html").as_uri())
    banner = page.inner_text("#docxBanner")
    ok("按 Word 保存的分页显示" in banner and "与文档记录的 5 页一致" in banner,
       "横幅标明按 Word 分页显示且与文档记录一致（verified）")
    ok("回写 DOCX" in banner and "只读结构" in banner, "横幅说明支持的纯文字可回写，且保留只读结构限制")
    ok(page.locator("#jump option").count() == 5, "页码选择器列出 5 页")
    first = page.locator("#host section.page").first.bounding_box()
    ok(first and first["width"] < first["height"],
       "A4 纵向页按原比例显示，无横向拉伸（%.0f×%.0f）" % (first["width"], first["height"]))
    stage_text = page.evaluate(f"() => {STAGE}.textContent")
    ok("第 1 页 / 共 5 页" in stage_text and "合成示例 · 仅供测试" in stage_text,
       "页脚 PAGE / NUMPAGES 按实际页码，页眉按页显示")
    ok("关键条款已用黄色高亮" in stage_text and "删除线文字" in stage_text, "高亮与删除线的文字可辨认")
    page.click("#docxDetails summary")
    limits = page.inner_text("#docxList")
    ok(all(word in limits for word in ("原生修订", "Word 批注", "文本框", "域")),
       "限制清单列出原生修订、Word 批注、文本框与域")
    first_para = page.evaluate(f"""() => {{
        const d = {STAGE}.querySelector('section.page [data-docx-p]');
        return {{n: +d.dataset.docxP, pid: d.dataset.docxPid || null, digest: d.dataset.docxD,
                 text: docxTextNodes(d).map(t => t.data).join('')}};
    }}""")
    drag_select(page, page.locator("#host section.page [data-docx-p]").first)
    ok(page.is_visible("#selbar"), "DOCX 正文文字可选择，拖选弹出操作条")
    page.click("#selbar [data-sel='delete']")
    ok(wait_toast(page, "已记下：删除这段", condition="普通段快捷删除的提示出现")
       and "校验定位并回写" in page.inner_text("#toast"),
       "普通段快捷删除照常记录，提示交给 Agent 后校验定位并回写")
    go_page(page, 3)
    page3 = page.evaluate(f"() => {STAGE}.textContent")
    ok("货到付款" in page3 and "验收合格后三十日内付款" in page3, "原生修订的删除与插入文字都保留")
    revision = page.locator("#host [data-docx-ro*='revision']").first
    revision.scroll_into_view_if_needed()   # 修订段在续表之下，先滚进视口再拖选
    drag_select(page, revision)
    ok(page.is_visible("#selbar"), "修订段同样可以选择")
    page.click("#selbar [data-sel='delete']")
    ok(wait_toast(page, "不能自动回写", condition="只读结构的提示出现")
       and "原生修订" in page.inner_text("#toast"),
       "选中原生修订时快捷删除提示涉及原生修订、不能自动回写")
    notes = page.evaluate("() => JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")
    dels = [n for n in notes if (n.get("edit") or {}).get("op") == "delete"]
    ok(len(dels) == 2 and all(n.get("quote") for n in dels),
       "两条快捷删除都记成批注并带选中文本（file:// 存本地存储）")
    # #136：引用选区写入段落定位（paraId、序号、摘要、段落内字符偏移），指纹与 review.json 一致
    quoted = (dels[0].get("docx") or {})
    ok(quoted.get("v") == 1 and quoted.get("kind") == "quote" and quoted.get("via") == "selection"
       and quoted.get("fp") == meta["docx"]["fingerprint"],
       "引用批注带 docx 定位 v1：来源为浏览器选区，指纹等于 review.json 记录的源文件指纹")
    ok(quoted.get("para") == {"id": first_para["pid"], "n": first_para["n"], "digest": first_para["digest"]},
       "引用批注定位到所选段落（paraId、段落序号、文本摘要与页面一致）")
    picked = "".join(first_para["text"][quoted.get("start", 0):quoted.get("end", 0)].split())
    ok(picked and "".join(dels[0]["quote"].split()).startswith(picked[:280]) and isinstance(quoted.get("run"), int),
       "字符偏移切出的段落文字就是引用的文字，并带文字运行序号作提示")
    fallback = page.evaluate(f"""() => {{
        const d = [...{STAGE}.querySelectorAll('[data-docx-p][data-docx-d]')].find(x => x.textContent.trim().length > 12);
        const text = docxTextNodes(d).map(t => t.data).join('').trim();
        return {{hit: docxFromText(text), n: +d.dataset.docxP, none: docxFromText('这段文字不在本页 zqxj')}};
    }}""")
    ok(fallback["hit"] and fallback["hit"]["via"] == "text" and fallback["hit"]["para"]["n"] == fallback["n"]
       and fallback["none"] is None,
       "拿不到 Shadow DOM 选区时按文本唯一匹配反查段落并记下来源；本页找不到的文字不写定位")
    readonly_fallback = page.evaluate(f"""() => {{
        const d = {STAGE}.querySelector('[data-docx-p][data-docx-ro*="revision"]');
        const original = sr.getSelection;
        try {{ sr.getSelection = () => null; return docxReadonly(docxTextNodes(d).map(t => t.data).join('')); }}
        finally {{ if (original) sr.getSelection = original; else delete sr.getSelection; }}
    }}""")
    ok("revision" in readonly_fallback, "拿不到 Shadow DOM 选区时，文本反查仍提示原生修订不能回写")
    add_note(page, "DOCX 页点标注", pin=(0.5, 0.1))
    page.click('[data-act="pin"]')   # 框选区域：进入标注模式后按住拖出一块（拖出 6px 以上才算框）
    box = page.locator("#slide").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.18)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.28, steps=4)
    page.mouse.up()
    page.fill("#ntext", "DOCX 页框选区域")
    page.click("[data-act='add']")
    ok(wait_until(lambda: "DOCX 页框选区域" in page.inner_text("#nlist"), page, 5,
                  condition="区域标注卡片出现在列表里")
       and page.locator("#host .ae-region").count() >= 1,
       "框选区域标注落在 DOCX 页上并记成批注")
    marked = {n["text"]: n.get("docx") or {} for n in
              page.evaluate("() => JSON.parse(localStorage.getItem('ae.' + RID + '.notes'))")}
    page_paras = set(page.evaluate(f"() => [...{STAGE}.querySelectorAll('[data-docx-p]')].map(d => +d.dataset.docxP)"))
    pin_a, region_a = marked.get("DOCX 页点标注", {}), marked.get("DOCX 页框选区域", {})
    ok(pin_a.get("kind") == "pin" and pin_a.get("via") == "point" and pin_a.get("para", {}).get("n") in page_paras,
       "点标注定位到落点所在的段落")
    ok(region_a.get("kind") == "region" and region_a.get("para", {}).get("n") in page_paras
       and region_a.get("endPara", region_a.get("para"))["n"] >= region_a["para"]["n"],
       "框选定位到框内的段落（首段到末段）")
    page.reload()
    texts = page.inner_text("#nlist")
    ok("DOCX 页点标注" in texts and "DOCX 页框选区域" in texts,
       "file:// 刷新后 DOCX 页上的点标注与框选区域仍在")
    go_page(page, 5)
    fifth = page.locator("#host section.page").first.bounding_box()
    ok(fifth and fifth["width"] > fifth["height"],
       "横向附件页按原比例显示（%.0f×%.0f）" % (fifth["width"], fifth["height"]))
    imported = [{"id": f"docx-import-{n}", "page": 1, "target": "报告", "text": "同文异段批注", "quote": "重复段",
                 "docx": {"v": 1, "kind": "quote", "via": "selection",
                          "para": {"id": None, "n": n, "digest": "0123456789ab"}, "start": 0, "end": 3}}
                for n in (0, 1)]
    page.click("#notesBtn")
    page.click("#openImp")
    page.fill("#impText", json.dumps({"reviewId": meta["reviewId"], "notes": imported}, ensure_ascii=False))
    page.click("#doImp")
    ok(wait_until(lambda: page.evaluate("() => JSON.parse(localStorage.getItem('ae.' + RID + '.notes')).filter(n => n.text === '同文异段批注').length") == 2,
                  page, 5, condition="两个不同段落的同文批注均导入本地存储"),
       "DOCX 同文异段批注在 file:// 导入时不会互相去重")
    ctx.close()
    # #136 服务模式：定位落盘；旧服务（不声明 docx 特性）时阻断写入
    sd = tmp / "docx_server"
    sd.mkdir()
    build(sd, ms.build_docx(sd / "sample.docx"), None)
    port = free_port()
    server = start_server(sd, port)
    sctx = browser.new_context()
    try:
        base = f"http://127.0.0.1:{port}/"
        spage = sctx.new_page()
        spage.goto(base)
        ok("docx" in spage.request.get(base + "api/notes").json()["features"], "服务声明 docx 特性")
        drag_select(spage, spage.locator("#host section.page [data-docx-p]").first)
        spage.click("#selbar [data-sel='delete']")
        ok(wait_until(lambda: any((n.get("docx") or {}).get("kind") == "quote" for n in disk_notes(sd)), spage, 8,
                      condition="带 docx 定位的批注写进 notes.json"),
           "服务模式下引用批注的 docx 定位落盘")
        ok(spage.locator("#docxBlock").is_hidden(), "新版服务下不显示阻断提示")
        drag_select(spage, spage.locator("#host section.page [data-docx-p]").first)
        spage.click("#selbar [data-sel='comment']")
        spage.fill("#ntext", "X" * 20001)
        spage.click("[data-act='add']")
        ok(wait_until(lambda: len(spage.input_value("#ntext")) == 20001 and "20000" in spage.inner_text("#toast"),
                      spage, 8, condition="超长引用批注被服务拒绝并恢复输入"),
           "服务拒绝超长引用批注后保留原输入")
        spage.fill("#ntext", "缩短后重试引用")
        spage.click("[data-act='add']")
        ok(wait_until(lambda: any(n["text"] == "缩短后重试引用" and (n.get("docx") or {}).get("kind") == "quote"
                                 for n in disk_notes(sd)), spage, 8, condition="引用批注重试后仍带 DOCX 定位"),
           "保存被拒后修改正文重试仍保留原段落定位")
        # 合成待定稿批次，验证顶栏处理记录中的直接写入口同样受旧服务阻断。
        seed = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import notes_store as store
d = Path(sys.argv[2])
seq = store.handoff_submit(d, '', target='review')[1]['seq']
store.handoff_claim(d, seq=seq)
for note in store.load(d)['notes']:
    store.set_status(d, note['id'], 'resolved', by='Synthetic', resolution='Synthetic change')
store.handoff_done(d, seq=seq)
"""
        subprocess.run([sys.executable, "-c", seed, str(SCRIPTS), str(sd)], check=True, capture_output=True)
        old = sctx.new_page()

        def legacy(route):
            response = route.fetch()
            doc = response.json()
            doc["features"] = [feature for feature in doc.get("features", []) if feature != "docx"]
            route.fulfill(response=response, json=doc)

        old.route("**/api/notes", legacy)
        old.goto(base)
        ok(wait_dom(old, "() => !document.getElementById('docxBlock').hidden", 8)
           and "旧版本" in old.inner_text("#docxBlock"), "旧版服务不认 docx 字段时显示阻断提示")
        before = len(disk_notes(sd))
        drag_select(old, old.locator("#host section.page [data-docx-p]").nth(1))
        ok(wait_toast(old, "不能写入", condition="阻断提示出现") and not old.is_visible("#selbar"),
           "阻断期间选中文字不弹操作条，提示不能写入")
        ok(old.evaluate("() => document.getElementById('bottomPanel').inert") and len(disk_notes(sd)) == before,
           "阻断期间批注面板不可操作，notes.json 没有新增")
        posts = []
        old.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
        old.click("#historyBtn")
        old.locator("#currentTaskToggle").check()
        old.locator('[data-bfinal="1"]').wait_for(state="visible")
        old.once("dialog", lambda dialog: dialog.accept())
        old.click('[data-bfinal="1"]')
        ok(wait_toast(old, "不能写入", condition="处理记录定稿操作也显示旧服务阻断")
           and not any(url.endswith("/api/handoff/finalize") for url in posts)
           and all(n["status"] == "drafted" for n in disk_notes(sd)),
           "旧服务阻断顶栏处理记录的直接写接口")
        old.once("dialog", lambda dialog: dialog.accept())
        old.click("#stopServiceBtn")
        ok(wait_until(lambda: server.poll() is not None, old, 8, condition="旧服务仍能通过恢复入口停止")
           and any(url.endswith("/api/server/stop") for url in posts),
           "旧服务阻断内容写入时仍允许停止服务以便升级")
        old.close()
    finally:
        sctx.close()
        stop(server)




def generated_draft_checks(browser, tmp: Path):
    """场景 generated-draft-loop：默认范式初稿闭环（详见 e2e_scenarios 注册表）。

    确定性的产品流程验证：安装副本的公开核验入口放行初稿、对照页接受带基础
    样式的产物、批注在页序调整后按稳定页面键找回。不冒充实际模型的生成质量
    评测（那由 evals/ 的技能评测覆盖）。
    """
    installed = tmp / "installed-skills" / "annotation-everything"
    env = {**os.environ, "CLAUDE_SKILLS_DIR": str(installed.parent)}
    subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    scripts = installed / "scripts"
    case = tmp / "draft-case"
    case.mkdir(parents=True)

    base_css = (scripts.parent / "references" / "report-base.css").read_text(encoding="utf-8")
    head_html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                 "<title>季度经营回顾初稿</title><style>" + base_css + "</style></head><body>")
    sec_sum = ('<section class="page" data-page="1" data-key="sum" id="page-sum">'
               "<h2>结论：收入同比增长 12%</h2><p class=\"ae-kpi\">+12%</p>"
               '<p>新开 3 家门店贡献主要增量。</p>'
               '<p class="ae-source">materials/q3-notes.md 第 2 节</p></section>')
    sec_rev = ('<section class="page" data-page="2" data-key="rev" id="page-rev">'
               "<h2>收入结构</h2>"
               "<table><tr><th>渠道</th><th>上季</th><th>本季</th></tr>"
               "<tr><td>堂食</td><td>66%</td><td>62%</td></tr>"
               "<tr><td>外卖</td><td>34%</td><td>38%</td></tr></table></section>")
    sec_next = ('<section class="page" data-page="3" data-key="next" id="page-next">'
                "<h2>下一步</h2><p>两件事。</p>"
                '<p class="ae-gap">十月周报缺失，待补充后复核结论。</p></section>')
    tail_html = "</body></html>"
    script_md = ("# 讲稿：季度经营回顾初稿\n\n"
                 "### 第 1 页｜结论\n**讲法**：\n先说结论：收入同比增长 12%。\n"
                 "### 第 2 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n"
                 "### 第 3 页｜下一步\n**讲法**：\n两件事。\n")
    report, script = case / "report.html", case / "script.md"
    report.write_text(head_html + sec_sum + sec_rev + sec_next + tail_html, encoding="utf-8")
    script.write_text(script_md, encoding="utf-8")

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    record = {
        "schema": "ae-report-record/1",
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["收入为什么增长"], "length": "3 页", "script": "yes"},
        "sources": [{"path": "materials/q3-notes.md", "status": "read",
                     "coverage": "全文", "note": ""}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha(report), "script": sha(script)},
                  "sourceNote": "materials/q3-notes.md"},
        "findings": [{"claim": "收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12},
                      "note": "口径：对上年同期"}],
        "gaps": ["十月周报缺失"],
        "openItems": [{"item": "提价幅度待定", "blocking": False}],
        "checks": [{"id": "content.requirements", "kind": "content", "status": "not_run",
                    "scope": "全部需求", "note": ""},
                   {"id": "visual.pages", "kind": "visual", "status": "not_run",
                    "scope": "全部 3 页", "note": ""}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record_path = case / "report-record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def cli(tool, *args, expect=0):
        done = subprocess.run([sys.executable, str(scripts / tool), *map(str, args)],
                              env=env, capture_output=True, text=True, timeout=60)
        if expect is not None:
            assert done.returncode == expect, \
                f"{tool} 退出码 {done.returncode}（期望 {expect}）：{done.stderr or done.stdout}"
        return done

    # 公开核验入口：初稿通过，可交付未被支持（内容与视觉核验未执行）
    checked = json.loads(cli("report_check.py", str(report), "--script", str(script),
                             "--record", str(record_path), "--locale", "zh-CN",
                             "--json").stdout)
    ok(checked["schema"] == "ae-report-check/1", "核验入口输出 ae-report-check/1 结构化结果")
    ok(checked["verdict"]["draftComplete"] is True, "初稿生成判定为完成")
    ok(checked["verdict"]["deliverable"] is False, "未执行内容与视觉核验时不支持可交付")
    ok(checked["coverage"]["content"] == "not_run" and checked["coverage"]["visual"] == "not_run",
       "覆盖情况如实显示未执行")
    ok(checked["record"]["fresh"] is True, "记录与当前产物对应（新鲜度通过）")

    review = case / "q3-review"
    cli("build_viewer.py", "--report", str(report), "--script", str(script),
        "--out", str(review), "--name", "季度回顾初稿", "--no-scan-skills")
    rid_before = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="对照页进入服务模式")
        page_count = page.locator("#jump option").count()
        ok(page_count == 3, f"带基础样式的三页报告进入对照页（{page_count} 页）")
        colors = page.evaluate(
            "() => { const r = document.getElementById('host').shadowRoot;"
            "  const h = r.querySelector('.page h1, .page h2');"
            "  const k = r.querySelector('.ae-kpi');"
            "  return [getComputedStyle(h).color, k ? getComputedStyle(k).color : null]; }")
        ok(colors[0] == "rgb(31, 41, 51)", f"基础样式正文色生效（{colors[0]}）")
        ok(colors[1] == "rgb(154, 52, 18)", f"基础样式 KPI 强调色生效（{colors[1]}）")
        go_page(page, 2)
        go_page(page, 3)
        ok(page.evaluate(
            "() => { const r = document.getElementById('host').shadowRoot;"
            "  return Boolean(r.querySelector('.ae-gap')); }"), "缺口标记随报告进入页面")
        go_page(page, 2)
        annotation = "外卖占比的数字加同比说明"
        add_note(page, annotation, pin=(0.5, 0.4))
        ok(wait_until(lambda: len(disk_notes(review)) == 1, page, 5), "批注自动落盘")
        ok(disk_notes(review)[0]["key"] == "rev" and disk_notes(review)[0]["page"] == 2,
           "批注关联稳定页面键 rev")
    finally:
        stop(server)
        ctx.close()

    # 页序调整（next 提到第 2 页，rev 退到第 3 页）后重建：真实改稿按新页序重写报告文档
    # （data-page 跟着新位置更新），讲稿同步；批注按 key 找回，审阅标识不变
    sec_next_2 = sec_next.replace('data-page="3"', 'data-page="2"')
    sec_rev_3 = sec_rev.replace('data-page="2"', 'data-page="3"')
    report.write_text(head_html + sec_sum + sec_next_2 + sec_rev_3 + tail_html, encoding="utf-8")
    reordered_script = script_md.replace(
        "### 第 2 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n"
        "### 第 3 页｜下一步\n**讲法**：\n两件事。\n",
        "### 第 2 页｜下一步\n**讲法**：\n两件事。\n"
        "### 第 3 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n")
    script.write_text(reordered_script, encoding="utf-8")
    cli("build_viewer.py", "--rebuild", str(review))
    rid_after = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
    ok(rid_after == rid_before, "重建后审阅标识不变")
    note = disk_notes(review)[0]
    ok(note["key"] == "rev" and note["page"] == 3,
       f"批注按页面键校正到新页码（key={note['key']} page={note['page']}）")

    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="重建后的对照页再次进入服务模式")
        go_page(page, 3)   # 批注列表按当前页过滤：翻到批注所在的新页码
        ok(wait_until(lambda: annotation in page.locator("#nlist").inner_text(), page, 5),
           "重建后批注卡片可见且内容完整")
    finally:
        stop(server)
        ctx.close()


def report_verdict_checks(browser, tmp: Path):
    """场景 report-verdict-loop：结构与视觉核验的可交付判定（详见 e2e_scenarios 注册表）。

    视觉逐页检查由本场景里的真实渲染执行：逐页测量画布溢出，把测量结果作为
    逐页证据写进核验记录，再由安装副本的核验入口做确定规则判定。正确产物
    （程序+内容+结构+视觉全部 covered）支持「报告可交付」；故障产物——如实
    登记的溢出、或声明逐页通过却缺页覆盖——不能通过整体交付验收。判定离线
    完成：不落批注、不触发复核或定稿，报告仍在对照页可审阅。不冒充实际模型
    的生成质量评测（那由 evals/ 覆盖）。
    """
    installed = tmp / "installed-skills" / "annotation-everything"
    env = {**os.environ, "CLAUDE_SKILLS_DIR": str(installed.parent)}
    subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    scripts = installed / "scripts"
    base_css = (scripts.parent / "references" / "report-base.css").read_text(encoding="utf-8")
    head = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            "<title>交付判定场景（合成）</title><style>" + base_css + "</style></head><body>")

    def section(n, key, title, body):
        return (f'<section class="page" data-page="{n}" data-key="{key}" id="page-{key}">'
                f"<h2>{title}</h2>{body}</section>")

    script_md = ("# 讲稿：交付判定场景（合成）\n\n"
                 "### 第 1 页｜结论\n**讲法**：\n先说结论：收入同比增长 12%。\n"
                 "### 第 2 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n"
                 "### 第 3 页｜下一步\n**讲法**：\n两件事。\n")
    material_text = "# 三季度笔记\n\n三季度收入 112 万元，上年同期 100 万元。\n"

    def sha_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def sha_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_case(name: str, report: str) -> dict:
        d = tmp / name
        (d / "materials").mkdir(parents=True)
        paths = {"dir": d,
                 "report": d / "report.html",
                 "script": d / "script.md",
                 "material": d / "materials" / "q3-notes.md"}
        paths["report"].write_text(head + report + "</body></html>", encoding="utf-8")
        paths["script"].write_text(script_md, encoding="utf-8")
        paths["material"].write_text(material_text, encoding="utf-8")
        return paths

    def record_for(paths: dict, *, visual: dict, declared=None) -> dict:
        """除视觉项外全部齐备的可交付形态记录；视觉项与交付声明由调用方给定。"""
        return {
            "schema": "ae-report-record/1",
            "generatedAt": "2026-11-02T09:00:00+08:00",
            "request": {"purpose": "季度经营汇报", "audience": "管理层",
                        "coreQuestions": ["三季度收入为什么增长"], "length": "3 页",
                        "script": "yes"},
            "sources": [{"path": "materials/q3-notes.md", "status": "read",
                         "coverage": "全文",
                         "sha256": sha_text(material_text), "note": ""}],
            "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                       "version": "0.9.0", "unavailable": []},
            "links": {"report": "report.html", "script": "script.md",
                      "sha256": {"report": sha_file(paths["report"]),
                                 "script": sha_file(paths["script"])},
                      "sourceNote": "materials/q3-notes.md 及同目录讲稿"},
            "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                          "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                          "refs": [{"path": "materials/q3-notes.md", "locator": "§1 收入",
                                    "quote": "上年同期 100 万元", "found": True}],
                          "support": "supported",
                          "basis": "对比材料两期收入原文，并按声明输入复算 percent-change",
                          "calculation": {"op": "percent-change",
                                          "inputs": {"a": 100, "b": 112}, "expected": 12,
                                          "unit": "万元", "period": "2025Q3 对 2024Q3",
                                          "definition": "对上年同期收入的增长百分比",
                                          "inputLocations": {
                                              "a": "materials/q3-notes.md §1：上年同期 100 万元",
                                              "b": "materials/q3-notes.md §1：三季度收入 112 万元"}},
                          "note": "收入 112 对上年 100"}],
            "gaps": ["十月周报缺失"],
            "checks": (
                [{"id": cid, "kind": "content", "status": "pass",
                  "scope": "全部", "note": "真实渲染核验场景的合成声明"}
                 for cid in ("content.requirements", "content.claims",
                             "content.fact-vs-inference", "content.units-dates",
                             "content.numbers-charts", "content.cross-page",
                             "content.script-consistency", "content.open-questions")]
                + [{"id": cid, "kind": "structure", "status": "pass",
                    "scope": "全部页", "note": "真实渲染核验场景的合成声明"}
                   for cid in ("structure.flow", "structure.headings",
                               "structure.claim-support", "structure.dup-gaps",
                               "structure.length")]
                + [visual]),
            "openItems": [{"item": "提价幅度待定", "blocking": False}],
            "verdict": {"draftComplete": True, "deliverable": declared},
        }

    def measure_page_overflow(page) -> list[dict]:
        """真实渲染逐页测量：当前页画布是否溢出（scrollWidth/Height 超出 client）。"""
        evidence = []
        for n in (1, 2, 3):
            go_page(page, n)
            m = page.evaluate(
                "() => { const r = document.getElementById('host').shadowRoot;"
                "  const s = r.querySelector('section.page');"
                "  return {sw: s.scrollWidth, cw: s.clientWidth,"
                "          sh: s.scrollHeight, ch: s.clientHeight}; }")
            issues = []
            if m["sw"] > m["cw"]:
                issues.append(f"内容超出画布右缘约 {m['sw'] - m['cw']}px")
            if m["sh"] > m["ch"]:
                issues.append(f"内容超出画布底部约 {m['sh'] - m['ch']}px")
            evidence.append({"page": n, "checked": True, "issues": issues,
                             "viewport": "1280x720"})
        return evidence

    def cli(tool, *args, expect=0):
        done = subprocess.run([sys.executable, str(scripts / tool), *map(str, args)],
                              env=env, capture_output=True, text=True, timeout=60)
        if expect is not None:
            assert done.returncode == expect, \
                f"{tool} 退出码 {done.returncode}（期望 {expect}）：{done.stderr or done.stdout}"
        return done

    # ── 正确产物：渲染测量无溢出 → 证据写进记录 → 可交付判定成立 ──
    good_body = "".join([
        section(1, "sum", "结论：收入同比增长 12%",
                '<p class="ae-kpi">+12%</p><p>新开 3 家门店贡献主要增量。</p>'
                '<p class="ae-source">materials/q3-notes.md 第 2 节</p>'),
        section(2, "rev", "收入结构",
                "<table><tr><th>渠道</th><th>上季</th><th>本季</th></tr>"
                "<tr><td>堂食</td><td>66%</td><td>62%</td></tr>"
                "<tr><td>外卖</td><td>34%</td><td>38%</td></tr></table>"),
        section(3, "next", "下一步", "<p>两件事。</p>"
                '<p class="ae-gap">十月周报缺失，待补充后复核结论。</p>'),
    ])
    good = write_case("verdict-good", good_body)
    review = good["dir"] / "q3-review"
    cli("build_viewer.py", "--report", str(good["report"]), "--script", str(good["script"]),
        "--out", str(review), "--name", "交付判定正确产物", "--no-scan-skills")
    rid_before = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="正确产物对照页进入服务模式")
        ok(page.locator("#jump option").count() == 3, "正确产物三页进入对照页")
        evidence = measure_page_overflow(page)
        ok(all(not e["issues"] for e in evidence),
           f"真实渲染逐页测量：正确产物无溢出（{evidence}）")
    finally:
        stop(server)
        ctx.close()
    visual_pass = {"id": "visual.pages", "kind": "visual", "status": "pass",
                   "scope": "全部 3 页（1280×720 实际渲染，逐页测量）",
                   "note": "逐页渲染检查", "pages": evidence}
    record_path = good["dir"] / "report-record.json"
    record_path.write_text(json.dumps(record_for(good, visual=visual_pass, declared=True),
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checked = json.loads(cli("report_check.py", str(good["report"]), "--script",
                             str(good["script"]), "--record", str(record_path),
                             "--locale", "zh-CN", "--json").stdout)
    ok(checked["verdict"]["deliverable"] is True,
       "程序、内容、结构、视觉全部 covered 时报告可交付判定成立")
    ok(checked["coverage"] == {"content": "covered", "structure": "covered",
                               "visual": "covered"},
       f"三类覆盖全部 covered（{checked['coverage']}）")
    ok(checked["verdict"]["draftComplete"] is True, "初稿与可交付分别输出（初稿完成）")
    ok(disk_notes(review) == [], "核验判定不落批注")
    rid_after = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
    ok(rid_after == rid_before, "核验判定不改变审阅标识，不触发复核或定稿")
    # 判定之后审阅照常：批注仍能落盘
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="判定后对照页仍进入服务模式")
        add_note(page, "外卖占比的数字加同比说明", pin=(0.5, 0.4))
        ok(wait_until(lambda: len(disk_notes(review)) == 1, page, 5),
           "判定之后批注照常落盘，审阅不受影响")
    finally:
        stop(server)
        ctx.close()

    # ── 故障产物：第 3 页 1600px 宽块真实溢出 ──
    wide = ('<div style="width:1600px"><table>'
            "<tr><th>项目</th><th>数值</th></tr><tr><td>咖啡豆成本</td><td>+9%</td></tr>"
            "</table></div>")
    faulty_body = "".join([
        section(1, "sum", "结论：收入同比增长 12%",
                '<p class="ae-kpi">+12%</p><p>新开 3 家门店贡献主要增量。</p>'),
        section(2, "rev", "收入结构", "<p>外卖占比 38%。</p>"),
        section(3, "next", "下一步", "<p>两件事。</p>" + wide),
    ])
    faulty = write_case("verdict-faulty", faulty_body)
    faulty_review = faulty["dir"] / "q3-review"
    cli("build_viewer.py", "--report", str(faulty["report"]), "--script", str(faulty["script"]),
        "--out", str(faulty_review), "--name", "交付判定故障产物", "--no-scan-skills")
    port = free_port()
    server = start_server(faulty_review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="故障产物对照页进入服务模式")
        evidence = measure_page_overflow(page)
        overflow_pages = [e["page"] for e in evidence if e["issues"]]
        ok(overflow_pages == [3],
           f"真实渲染逐页测量：只有第 3 页溢出（{overflow_pages}）")
    finally:
        stop(server)
        ctx.close()
    # 如实登记溢出：声明与证据一致（程序检查通过），但视觉覆盖 uncovered，
    # 「程序检查通过」不代替视觉检查通过，可交付被挡下
    visual_fail = {"id": "visual.pages", "kind": "visual", "status": "fail",
                   "scope": "全部 3 页（1280×720 实际渲染，逐页测量）",
                   "note": "逐页渲染检查，第 3 页溢出", "pages": evidence}
    record_path = faulty["dir"] / "report-record.json"
    record_path.write_text(json.dumps(record_for(faulty, visual=visual_fail, declared=False),
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checked = json.loads(cli("report_check.py", str(faulty["report"]), "--script",
                             str(faulty["script"]), "--record", str(record_path),
                             "--locale", "zh-CN", "--json").stdout)
    ok(checked["verdict"]["deliverable"] is False,
       "如实登记的溢出阻止可交付判定（程序检查通过不代替视觉检查通过）")
    ok(checked["coverage"]["visual"] == "uncovered",
       f"视觉覆盖如实显示未通过（{checked['coverage']['visual']}）")
    ok(checked["verdict"]["draftComplete"] is True, "故障产物的初稿仍可进入审阅")
    # 缺页覆盖反例：声明逐页通过但证据缺第 3 页，判定入口直接判否
    missing = record_for(faulty, visual=visual_fail)
    missing["verdict"] = {"draftComplete": True, "deliverable": True}
    for item in missing["checks"]:
        if item["id"] == "visual.pages":
            item.update({"status": "pass", "note": "声称全部检查通过",
                         "pages": [e for e in evidence if e["page"] in (1, 2)]})
    record_path.write_text(json.dumps(missing, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    done = cli("report_check.py", str(faulty["report"]), "--script", str(faulty["script"]),
               "--record", str(record_path), "--locale", "zh-CN", "--json", expect=1)
    payload = json.loads(done.stdout)
    visual_item = [c for c in payload["checks"] if c["id"] == "record.visual"][0]
    ok(visual_item["code"] == "record.visual-coverage"
       and "3" in visual_item["detail"],
       f"声明逐页通过但缺页覆盖被程序判否（{visual_item['code']}：{visual_item['detail']}）")
    ok(payload["verdict"]["deliverable"] is False,
       "缺页覆盖的反例不能通过整体交付验收")
    # 故障产物仍在对照页可审阅
    port = free_port()
    server = start_server(faulty_review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="故障产物对照页仍进入服务模式")
        go_page(page, 3)
        ok(True, "故障产物第 3 页在对照页照常渲染审阅")
    finally:
        stop(server)
        ctx.close()


def revision_update_checks(browser, tmp: Path):
    """场景 revision-update-loop：改稿更新核验（详见 e2e_scenarios 注册表）。

    真实公开操作串起一条改稿链：可交付初态 → 改数字（源文件+讲稿同步）→ 旧可
    交付判定失效 → 维护记录（更新哈希、revision 重新核验声明）→ 恢复；来源变动
    → 材料哈希不符 → unverified；只改样式 → 真实渲染重测视觉证据后按范围恢复；
    无法确认影响范围 → 摘要披露待核验范围与原因、可交付被拦；页序调整 → 批注按
    稳定页面键找回、记录引用保持关联。判定用安装副本的核验入口，不落批注。
    """
    installed = tmp / "installed-skills" / "annotation-everything"
    env = {**os.environ, "CLAUDE_SKILLS_DIR": str(installed.parent)}
    subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    scripts = installed / "scripts"
    base_css = (scripts.parent / "references" / "report-base.css").read_text(encoding="utf-8")

    def section(n, key, title, body):
        return (f'<section class="page" data-page="{n}" data-key="{key}" id="page-{key}">'
                f"<h2>{title}</h2>{body}</section>")

    material_text = "# 三季度笔记\n\n三季度收入 112 万元，上年同期 100 万元。\n"
    script_md = ("# 讲稿：改稿核验场景（合成）\n\n"
                 "### 第 1 页｜结论\n**讲法**：\n先说结论：收入同比增长 12%。\n"
                 "### 第 2 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n"
                 "### 第 3 页｜下一步\n**讲法**：\n两件事。\n")
    case = tmp / "revision-case"
    case.mkdir(parents=True)
    (case / "materials").mkdir()
    report, script, material = case / "report.html", case / "script.md", case / "materials" / "q3-notes.md"
    report_path = report

    def sha_file(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def write_report(body: str, style_extra: str = "") -> None:
        head = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                "<title>改稿核验场景（合成）</title><style>" + base_css + style_extra
                + "</style></head><body>")
        report_path.write_text(head + body + "</body></html>", encoding="utf-8")

    def write_body(v12: str = "12%") -> str:
        return "".join([
            section(1, "sum", f"结论：收入同比增长 {v12}",
                    f'<p class="ae-kpi">+{v12}</p><p>新开 3 家门店贡献主要增量。</p>'
                    '<p class="ae-source">materials/q3-notes.md 第 1 节</p>'),
            section(2, "rev", "收入结构",
                    "<table><tr><th>渠道</th><th>本季</th></tr>"
                    "<tr><td>外卖</td><td>38%</td></tr></table>"),
            section(3, "next", "下一步", "<p>两件事。</p>"),
        ])

    material.write_text(material_text, encoding="utf-8")
    script.write_text(script_md, encoding="utf-8")
    write_report(write_body())

    def cli(tool, *args, expect=0):
        done = subprocess.run([sys.executable, str(scripts / tool), *map(str, args)],
                              env=env, capture_output=True, text=True, timeout=60)
        if expect is not None:
            assert done.returncode == expect, \
                f"{tool} 退出码 {done.returncode}（期望 {expect}）：{done.stderr or done.stdout}"
        return done

    def base_record(record_report_sha: str) -> dict:
        return {
            "schema": "ae-report-record/1",
            "generatedAt": "2026-11-02T09:00:00+08:00",
            "request": {"purpose": "季度经营汇报", "audience": "管理层",
                        "coreQuestions": ["三季度收入为什么增长"], "length": "3 页",
                        "script": "yes"},
            "sources": [{"path": "materials/q3-notes.md", "status": "read",
                         "coverage": "全文", "sha256": sha_file(material), "note": ""}],
            "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                       "version": "0.9.0", "unavailable": []},
            "links": {"report": "report.html", "script": "script.md",
                      "sha256": {"report": record_report_sha, "script": sha_file(script)},
                      "sourceNote": "materials/q3-notes.md 及同目录讲稿"},
            "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                          "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                          "refs": [{"path": "materials/q3-notes.md", "locator": "§1 收入",
                                    "quote": "上年同期 100 万元", "found": True}],
                          "support": "supported",
                          "basis": "对比材料两期收入原文，并按声明输入复算 percent-change",
                          "calculation": {"op": "percent-change",
                                          "inputs": {"a": 100, "b": 112}, "expected": 12,
                                          "unit": "万元", "period": "2025Q3 对 2024Q3",
                                          "definition": "对上年同期收入的增长百分比",
                                          "inputLocations": {
                                              "a": "materials/q3-notes.md §1：上年同期 100 万元",
                                              "b": "materials/q3-notes.md §1：三季度收入 112 万元"}},
                          "note": "收入 112 对上年 100"}],
            "gaps": [],
            "checks": (
                [{"id": cid, "kind": "content", "status": "pass",
                  "scope": "全部", "note": "真实渲染核验场景的合成声明"}
                 for cid in ("content.requirements", "content.claims",
                             "content.fact-vs-inference", "content.units-dates",
                             "content.numbers-charts", "content.cross-page",
                             "content.script-consistency", "content.open-questions")]
                + [{"id": cid, "kind": "structure", "status": "pass",
                    "scope": "全部页", "note": "真实渲染核验场景的合成声明"}
                   for cid in ("structure.flow", "structure.headings",
                               "structure.claim-support", "structure.dup-gaps",
                               "structure.length")]
                + [{"id": "visual.pages", "kind": "visual", "status": "pass",
                    "scope": "全部 3 页（1280×720 实际渲染，逐页测量）",
                    "note": "逐页渲染检查", "pages": []}]),
            "openItems": [],
            "verdict": {"draftComplete": True, "deliverable": True},
        }

    def run_check(expect=0):
        return json.loads(cli("report_check.py", str(report_path), "--script", str(script),
                              "--record", str(record_path), "--locale", "zh-CN",
                              "--json", expect=expect).stdout)

    def measure_page_overflow(page, pages=(1, 2, 3)) -> list[dict]:
        """真实渲染逐页测量：当前页画布是否溢出（scrollWidth/Height 超出 client）。"""
        evidence = []
        for n in pages:
            go_page(page, n)
            m = page.evaluate(
                "() => { const r = document.getElementById('host').shadowRoot;"
                "  const s = r.querySelector('section.page');"
                "  return {sw: s.scrollWidth, cw: s.clientWidth,"
                "          sh: s.scrollHeight, ch: s.clientHeight}; }")
            issues = []
            if m["sw"] > m["cw"]:
                issues.append(f"内容超出画布右缘约 {m['sw'] - m['cw']}px")
            if m["sh"] > m["ch"]:
                issues.append(f"内容超出画布底部约 {m['sh'] - m['ch']}px")
            evidence.append({"page": n, "checked": True, "issues": issues,
                             "viewport": "1280x720"})
        return evidence

    def render_evidence() -> list[dict]:
        """对照页真实渲染逐页测量，返回视觉证据。"""
        port = free_port()
        review = case / "q3-review"
        server = start_server(review, port, scripts=scripts)
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
        try:
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/")
            wait_mode(page, "server", condition="改稿核验对照页进入服务模式")
            return measure_page_overflow(page)
        finally:
            stop(server)
            ctx.close()

    record_path = case / "report-record.json"
    review = case / "q3-review"

    # ── 初态：真实渲染证据 + 完整记录 → 可交付 ──
    cli("build_viewer.py", "--report", str(report_path), "--script", str(script),
        "--out", str(review), "--name", "改稿核验场景", "--no-scan-skills")
    evidence = render_evidence()
    ok(all(not e["issues"] for e in evidence),
       f"初态真实渲染逐页测量无溢出（{[e['issues'] for e in evidence]}）")
    record = base_record(sha_file(report_path))
    for item in record["checks"]:
        if item["id"] == "visual.pages":
            item["pages"] = evidence
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    checked = run_check()
    ok(checked["verdict"]["deliverable"] is True, "初态完整记录支持可交付判定")
    ok(checked["record"]["revision"] is None, "初次生成形态没有 revision 声明")

    # ── 改数字（源文件+讲稿同步）：旧可交付判定失效 ──
    write_report(write_body(v12="15%"))
    script.write_text(script_md.replace("同比增长 12%", "同比增长 15%"), encoding="utf-8")
    checked = run_check(expect=1)
    ok(checked["verdict"]["deliverable"] is False, "改数字后旧的可交付判定不再有效")
    ok(checked["record"]["fresh"] is False and checked["coverage"]["content"] == "unverified",
       f"记录对不上当前产物（fresh={checked['record']['fresh']}，"
       f"coverage={checked['coverage']['content']}）")
    ok(checked["verdict"]["draftComplete"] is True, "记录过期挡可交付，不挡初稿审阅")

    # ── 维护记录：更新哈希 + revision 重新核验声明 → 恢复 ──
    record = base_record(sha_file(report_path))
    for item in record["checks"]:
        if item["id"] == "visual.pages":
            item["pages"] = evidence
    record["revision"] = {
        "basis": "改动落在第 1 页收入数字，讲稿同步；跨页口径与引用未受影响",
        "rechecked": ["content.numbers-charts", "content.cross-page",
                      "content.script-consistency", "content.claims"],
        "pending": []}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    checked = run_check()
    ok(checked["verdict"]["deliverable"] is True,
       "受影响项重新核验并维护记录后可交付恢复（未受影响证据保留）")
    ok(checked["record"]["revision"]["rechecked"][0] == "content.numbers-charts",
       "结构化结果披露本次重新核验范围")

    # ── 来源变动：材料哈希不符 → unverified ──
    material.write_text(material_text.replace("112", "119"), encoding="utf-8")
    checked = run_check(expect=1)
    ok(checked["verdict"]["deliverable"] is False, "来源变动后旧结论不能为新材料背书")
    ok(checked["coverage"]["content"] == "unverified",
       f"来源变动的覆盖降级（{checked['coverage']['content']}）")
    material.write_text(material_text, encoding="utf-8")   # 恢复材料

    # ── 只改样式：真实渲染重测视觉证据后按范围恢复，内容类覆盖保留 ──
    write_report(write_body(v12="15%"), style_extra=" .page{padding:80px}")
    checked = run_check(expect=1)
    ok(checked["verdict"]["deliverable"] is False, "改样式后同样需要维护核验记录")
    style_evidence = render_evidence()
    ok(all(not e["issues"] for e in style_evidence),
       "样式改动的视觉影响经真实渲染重测（无溢出）")
    record = base_record(sha_file(report_path))
    for item in record["checks"]:
        if item["id"] == "visual.pages":
            item["pages"] = style_evidence
    record["revision"] = {
        "basis": "改动是全局页边距样式，内容与来源未变；视觉逐页重测",
        "rechecked": ["visual.pages"],
        "pending": []}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    checked = run_check()
    ok(checked["verdict"]["deliverable"] is True, "只改样式经视觉重测与记录维护后恢复")
    ok(checked["coverage"]["content"] == "covered", "内容类覆盖保留（未受影响证据不重查）")

    # ── 无法确认影响范围：待核验披露拦下可交付 ──
    record = base_record(sha_file(report_path))
    for item in record["checks"]:
        if item["id"] == "visual.pages":
            item["pages"] = style_evidence
    record["revision"] = {
        "basis": "样式改动疑似影响图表对齐，但无法确认涉及哪些页",
        "rechecked": ["visual.pages"],
        "pending": [{"scope": "第 2 页图表与讲稿对应说法",
                     "reason": "边距变化是否影响表格可读无法确认"}]}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    checked = run_check()
    ok(checked["verdict"]["deliverable"] is False, "存在待核验项时不能判可交付")
    reasons = "；".join(checked["verdict"]["reasons"])
    ok("第 2 页图表与讲稿对应说法" in reasons and "边距变化是否影响表格可读无法确认" in reasons,
       f"待核验范围与原因写入判定原因（{reasons}）")
    summary_run = cli("report_check.py", str(report_path), "--script", str(script),
                      "--record", str(record_path), "--locale", "zh-CN")
    ok("待核验" in summary_run.stdout and "第 2 页图表" in summary_run.stdout,
       "摘要如实披露待核验范围")

    # ── 页序调整：稳定页面键保持批注与记录引用的关联 ──
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="页序调整前对照页进入服务模式")
        add_note(page, "收入数字引用请标注期间", pin=(0.5, 0.4))
        ok(wait_until(lambda: len(disk_notes(review)) == 1, page, 5), "批注落盘")
    finally:
        stop(server)
        ctx.close()
    # 真实改稿按新页序重写文档顺序并更新 data-page，讲稿同步
    body_reordered = "".join([
        section(1, "sum", "结论：收入同比增长 15%",
                '<p class="ae-kpi">+15%</p><p>新开 3 家门店贡献主要增量。</p>'
                '<p class="ae-source">materials/q3-notes.md 第 1 节</p>'),
        section(2, "next", "下一步", "<p>两件事。</p>"),
        section(3, "rev", "收入结构",
                "<table><tr><th>渠道</th><th>本季</th></tr>"
                "<tr><td>外卖</td><td>38%</td></tr></table>"),
    ])
    write_report(body_reordered)
    script.write_text(script_md.replace("同比增长 12%", "同比增长 15%").replace(
        "### 第 2 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n"
        "### 第 3 页｜下一步\n**讲法**：\n两件事。\n",
        "### 第 2 页｜下一步\n**讲法**：\n两件事。\n"
        "### 第 3 页｜收入结构\n**讲法**：\n外卖占比继续上升。\n"), encoding="utf-8")
    cli("build_viewer.py", "--rebuild", str(review))
    note = disk_notes(review)[0]
    ok(note["key"] == "sum" and note["page"] == 1, "批注按页面键保持在结论页")
    reordered_evidence = render_evidence()
    record = base_record(sha_file(report_path))
    for item in record["checks"]:
        if item["id"] == "visual.pages":
            item["pages"] = reordered_evidence
    record["revision"] = {
        "basis": "页序调整，内容与来源未变；批注与记录引用按稳定页面键关联",
        "rechecked": ["structure.flow", "content.cross-page"],
        "pending": []}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    checked = run_check()
    page_refs = [c for c in checked["checks"] if c["id"] == "record.pageRefs"][0]
    ok(page_refs["status"] == "pass",
       "页序重排后记录引用经稳定页面键保持关联")
    ok(checked["verdict"]["deliverable"] is True, "页序调整维护后判定恢复")
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="重建后的对照页进入服务模式")
        ok(page.locator("#jump option").count() == 3, "重建后仍为三页")
        go_page(page, 1)
        ok(wait_until(lambda: "收入数字引用请标注期间" in page.locator("#nlist").inner_text(),
                      page, 5), "重建后批注在结论页可见")
    finally:
        stop(server)
        ctx.close()


def legacy_report_checks(browser, tmp: Path):
    """场景 legacy-report-compat：旧报告无核验记录仍直接审阅（详见 e2e_scenarios 注册表）。

    无记录旧报告走完整审阅回路：对照页、批注落盘、页面提交（已初改·待复核）、
    claim 接收、初改、重建、done --draft 停在待复核；报告质量状态与批次状态分离
    （核验入口对旧报告显示无记录，批次状态不受影响）。整体核验请求下按完整清单
    建立记录恢复可交付；原始依据不可得的缺口形态如实显示，不伪称核验完成。
    """
    installed = tmp / "installed-skills" / "annotation-everything"
    env = {**os.environ, "CLAUDE_SKILLS_DIR": str(installed.parent)}
    subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    scripts = installed / "scripts"
    base_css = (scripts.parent / "references" / "report-base.css").read_text(encoding="utf-8")

    case = tmp / "legacy-case"
    case.mkdir(parents=True)
    report = case / "report.html"
    script = case / "script.md"
    body = "".join([
        '<section class="page" data-page="1" data-key="sum" id="page-sum">'
        "<h2>结论</h2><p>收入同比上升。</p></section>",
        '<section class="page" data-page="2" data-key="rev" id="page-rev">'
        "<h2>结构</h2><p>外卖占比上升。</p></section>",
        '<section class="page" data-page="3" data-key="next" id="page-next">'
        "<h2>下一步</h2><p>两件事。</p></section>",
    ])
    report.write_text('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                      "<title>旧版季度回顾（合成，无核验记录）</title><style>" + base_css
                      + "</style></head><body>" + body + "</body></html>", encoding="utf-8")
    script.write_text("# 讲稿：旧版季度回顾（合成）\n\n"
                      "### 第 1 页｜结论\n**讲法**：\n先说结论。\n"
                      "### 第 2 页｜结构\n**讲法**：\n外卖占比上升。\n"
                      "### 第 3 页｜下一步\n**讲法**：\n两件事。\n", encoding="utf-8")

    def cli(tool, *args, expect=0, in_env=None):
        done = subprocess.run([sys.executable, str(scripts / tool), *map(str, args)],
                              env=in_env or env, capture_output=True, text=True, timeout=60)
        if expect is not None:
            assert done.returncode == expect, \
                f"{tool} 退出码 {done.returncode}（期望 {expect}）：{done.stderr or done.stdout}"
        return done

    def run_check(record=None, expect=0):
        args = ["report_check.py", str(report), "--script", str(script)]
        if record is not None:
            args += ["--record", str(record)]
        args += ["--locale", "zh-CN", "--json"]
        return json.loads(cli(*args, expect=expect).stdout)

    # ── 旧报告没有核验记录：核验入口如实显示，不阻塞审阅 ──
    checked = run_check()
    ok(checked["verdict"]["draftComplete"] is True and not checked["record"]["fresh"],
       "无记录旧报告：初稿判定不受阻，不被迫补建记录")
    ok(any("没有可用的核验记录" in r for r in checked["verdict"]["reasons"]),
       "缺记录是如实披露（记录缺失说明原因，不是程序失败）")

    # ── 真实审阅回路：对照页 → 批注 → 提交（已初改·待复核）→ 接收 → 初改 → 停 ──
    review = case / "old-review"
    cli("build_viewer.py", "--report", str(report), "--script", str(script),
        "--out", str(review), "--name", "旧版季度回顾", "--no-scan-skills")
    rid_before = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="旧报告对照页进入服务模式")
        ok(page.locator("#jump option").count() == 3, "旧报告三页进入对照页")
        add_note(page, "收入上升的幅度请补数字", pin=(0.5, 0.4))
        ok(wait_until(lambda: len(disk_notes(review)) == 1, page, 5), "旧报告批注落盘")
        page.click("#handBtn")
        page.check('input[name="htarget"][value="review"]')
        page.fill("#handMsg", "先改，改完等我复核")
        page.click("#doHand")
        ok(wait_until(lambda: (review / "handoff.json").exists(), page, 5),
           "旧报告的提交照常落盘（目标：已初改·待复核）")
    finally:
        stop(server)
        ctx.close()
    item = json.loads((review / "handoff.json").read_text(encoding="utf-8"))["items"][-1]
    ok(item.get("target") == "review", "提交目标记录为已初改·待复核")
    claimed = cli("notes.py", "claim", str(review))
    ok(claimed.returncode == 0, "Agent 照常接收旧报告的批次")
    batch = json.loads(claimed.stdout)
    seq = batch.get("seq")
    ok(seq is not None and batch.get("target") == "review",
       f"接收输出带批号与目标（seq={seq}）")
    # 初改：改源文件 + 重建 + 初改标记（target=review 的批次里 resolve 自动记为已初改·待复核）
    report.write_text(report.read_text(encoding="utf-8").replace("收入同比上升。",
                                                                "收入同比上升 12%。"),
                      encoding="utf-8")
    cli("build_viewer.py", "--rebuild", str(review))
    note_id = disk_notes(review)[0]["id"]
    cli("notes.py", "resolve", str(review), note_id, "--seq", str(seq),
        "--by", "初改", "--note", "结论页补上 12%")
    done = cli("notes.py", "done", str(review), "--seq", str(seq), "--draft",
               "--summary", "初改完成，等用户复核")
    ok(done.returncode == 0, "初改收尾")
    batch_after = json.loads((review / "handoff.json").read_text(encoding="utf-8"))["items"][-1]
    ok(batch_after.get("status") == "drafted",
       f"批次停在已初改·待复核，等用户（status={batch_after.get('status')}）")
    note_after = [n for n in disk_notes(review) if n["id"] == note_id][0]
    ok(note_after.get("status") == "drafted", "批注记为已初改·待复核（drafted）")
    rid_after = json.loads((review / "review.json").read_text(encoding="utf-8"))["reviewId"]
    ok(rid_after == rid_before, "改稿与重建保持审阅标识")

    # ── 状态分离：报告质量状态独立于批次状态 ──
    checked = run_check()
    ok(checked["verdict"]["draftComplete"] is True and not checked["record"]["fresh"],
       "初改后报告仍无核验记录：报告质量状态如实独立显示，不受批次影响")
    ok(checked["verdict"]["deliverable"] is False, "无记录不等于可交付")

    # ── 整体核验请求：按完整清单建立记录（真实渲染视觉证据）→ 可交付 ──
    (case / "materials").mkdir(exist_ok=True)
    material = case / "materials" / "old-notes.md"
    material.write_text("# 旧季度笔记\n\n收入 112，上年 100；外卖占比 38%。\n", encoding="utf-8")

    def sha_file(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="整体核验前对照页进入服务模式")
        evidence = []
        for n in (1, 2, 3):
            go_page(page, n)
            m = page.evaluate(
                "() => { const r = document.getElementById('host').shadowRoot;"
                "  const s = r.querySelector('section.page');"
                "  return {sw: s.scrollWidth, cw: s.clientWidth,"
                "          sh: s.scrollHeight, ch: s.clientHeight}; }")
            evidence.append({"page": n, "checked": True,
                             "issues": ([] if m["sw"] <= m["cw"] and m["sh"] <= m["ch"]
                                        else ["画布溢出"]), "viewport": "1280x720"})
        ok(all(not e["issues"] for e in evidence), "整体核验逐页渲染测量完成")
    finally:
        stop(server)
        ctx.close()
    record = {
        "schema": "ae-report-record/1",
        "generatedAt": "2026-11-02T10:00:00+08:00",
        "request": {"purpose": "季度经营回顾", "audience": "管理层",
                    "coreQuestions": ["收入与结构表现"], "length": "3 页", "script": "yes"},
        "sources": [{"path": "materials/old-notes.md", "status": "read",
                     "coverage": "全文", "sha256": sha_file(material), "note": "整体核验时重新读取"}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha_file(report), "script": sha_file(script)},
                  "sourceNote": "旧报告整体核验（无原始生成记录）"},
        "findings": [{"claim": "收入同比上升 12%", "type": "fact",
                      "sources": ["materials/old-notes.md"], "pages": ["sum"],
                      "refs": [{"path": "materials/old-notes.md", "locator": "全文",
                                "quote": "收入 112，上年 100", "found": True}],
                      "support": "supported", "basis": "按材料两期数字复算核对手工口径",
                      "note": "旧报告的结论重新定位到材料"}],
        "gaps": [],
        "checks": (
            [{"id": cid, "kind": "content", "status": "pass", "scope": "全部",
              "note": "整体核验补建"} for cid in (
                "content.requirements", "content.claims", "content.fact-vs-inference",
                "content.units-dates", "content.numbers-charts", "content.cross-page",
                "content.script-consistency", "content.open-questions")]
            + [{"id": cid, "kind": "structure", "status": "pass", "scope": "全部页",
                "note": "整体核验补建"} for cid in (
                "structure.flow", "structure.headings", "structure.claim-support",
                "structure.dup-gaps", "structure.length")]
            + [{"id": "visual.pages", "kind": "visual", "status": "pass",
                "scope": "全部 3 页（1280×720 实际渲染，逐页测量）",
                "note": "逐页渲染检查", "pages": evidence}]),
        "openItems": [],
        "verdict": {"draftComplete": True, "deliverable": True},
    }
    record_path = case / "report-record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    checked = run_check(record=record_path)
    ok(checked["verdict"]["deliverable"] is True,
       "整体核验请求下为旧报告建立完整记录并恢复可交付判定")
    ok(checked["record"]["fresh"] is True, "补建的记录对应当前产物")

    # ── 原始依据不可得：缺口登记，不伪称核验完成 ──
    gap_case = tmp / "legacy-gap"
    gap_case.mkdir(parents=True)
    gap_report = gap_case / "report.html"
    gap_report.write_text(report.read_text(encoding="utf-8"), encoding="utf-8")
    gap_script = gap_case / "script.md"
    gap_script.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    gap_record = json.loads(json.dumps(record))
    gap_record["sources"] = []
    gap_record["links"] = {"report": "report.html", "script": "script.md",
                           "sha256": {"report": sha_file(gap_report),
                                      "script": sha_file(gap_script)},
                           "sourceNote": "原始材料不可获得，仅报告自身可用"}
    for it in gap_record["findings"]:
        it["sources"] = []
        it["refs"] = []
        it["support"] = "unverified"
        it["basis"] = "原始材料不可获得，无法对照"
    for it in gap_record["checks"]:
        if it["id"] in ("content.claims", "content.numbers-charts"):
            it["status"] = "not_run"
            it["note"] = "原始材料不可获得，无法核对"
    gap_record["gaps"] = ["原始材料不可获得：核验依据以报告自身为准，待材料补充后复核"]
    gap_record["verdict"] = {"draftComplete": True, "deliverable": None}
    gap_path = gap_case / "report-record.json"
    gap_path.write_text(json.dumps(gap_record, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    gap_checked = json.loads(cli("report_check.py", str(gap_report), "--script", str(gap_script),
                                 "--record", str(gap_path), "--locale", "zh-CN",
                                 "--json").stdout)
    ok(gap_checked["verdict"]["deliverable"] is False,
       "原始依据不可得的记录不能伪称可交付")
    gap_reasons = "；".join(gap_checked["verdict"]["reasons"])
    ok("内容核验" in gap_reasons, f"未执行的内容核验在原因中如实列出（{gap_reasons}）")
    gap_summary_run = cli("report_check.py", str(gap_report), "--script", str(gap_script),
                          "--record", str(gap_path), "--locale", "zh-CN")
    ok("原始材料不可获得" in gap_summary_run.stdout, "摘要披露缺口登记")


def update_notice_checks(browser, tmp: Path):
    """#128：从真实重建到手动刷新，提示醒目且不打断未发送的批注。"""
    review = tmp / "update_notice"
    build(review)
    port = free_port()
    server = start_server(review, port)
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    artifacts = os.environ.get("AE_UPDATE_NOTICE_ARTIFACTS")

    def rebuild():
        index = review / "index.html"
        before = index.stat().st_mtime_ns
        subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), "--rebuild", str(review)],
                       check=True, capture_output=True)
        # 显式推进文件系统时间，避免为跨过 mtime 粒度固定睡眠。
        modified = max(index.stat().st_mtime_ns, before + 2_000_000_000)
        os.utime(index, ns=(modified, modified))

    def notice_fits():
        boxes = [page.locator(selector).bounding_box()
                 for selector in ("#reloadBanner", "#reloadBtn", "#reloadClose")]
        viewport = page.viewport_size
        return all(box and box["x"] >= 0 and box["y"] >= 0
                   and box["x"] + box["width"] <= viewport["width"]
                   and box["y"] + box["height"] <= viewport["height"] for box in boxes)

    try:
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server")
        navigations = []
        page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
        ok(not page.is_visible("#reloadBanner"), "首次打开对照页不显示更新提示")
        go_page(page, 2)
        page.fill("#ntext", "合成草稿：第 2 页尚未发送")
        go_page(page, 3)
        page.fill("#ntext", "合成草稿：第 3 页尚未发送")
        page.fill("#nskills", "humanizer-zh")
        page.select_option("#ntarget", "讲稿")
        rebuild()
        shown = wait_until(lambda: page.is_visible("#reloadBanner"), page, 10,
                           condition="真实重建后轮询显示顶部更新横幅")
        ok(shown and "对照页已更新" in page.inner_text("#reloadBanner"),
           "真实重建后，主要工作区顶部明确显示「对照页已更新」")
        box = page.locator("#reloadBanner").bounding_box()
        ok(notice_fits() and box and box["y"] < 130 and box["height"] >= 60
           and page.locator("#reloadBtn").bounding_box()["height"] >= 36,
           "更新横幅位于页面顶部，说明与刷新按钮醒目可见")
        ok(page.evaluate("""() => {
            const slide = document.getElementById('slide').getBoundingClientRect();
            const frame = document.getElementById('host').shadowRoot.getElementById('ae-frame').getBoundingClientRect();
            return frame.top >= slide.top && frame.bottom <= slide.bottom
                && frame.left >= slide.left && frame.right <= slide.right;
        }"""), "矮窗口里显示更新横幅后，报告仍完整缩放在可见区域内")
        ok(wait_poll(page) and page.is_visible("#reloadBanner") and not navigations
           and page.input_value("#ntext") == "合成草稿：第 3 页尚未发送",
           "后续轮询不会自动关闭提示、刷新页面或改动正在写的批注")

        height_with_notice = page.locator("#host").bounding_box()["height"]
        page.click("#reloadClose")
        ok(page.locator("#host").bounding_box()["height"] > height_with_notice,
           "关闭更新横幅后立即恢复报告尺寸，无需等待下次轮询")
        ok(not page.is_visible("#reloadBanner") and wait_poll(page)
           and not page.is_visible("#reloadBanner") and not navigations,
           "关闭提示不刷新页面，同一次重建的后续轮询不会重新弹出")
        rebuild()
        ok(wait_until(lambda: page.is_visible("#reloadBanner"), page, 10,
                      condition="再次重建后重新显示更新横幅"), "关闭后再次重建会重新显示提示")
        page.click("#focusBtn")
        ok(wait_dom(page, "() => document.body.classList.contains('focus-mode')", 5)
           and page.is_visible("#reloadBanner") and notice_fits(), "专注模式仍能看到更新横幅")
        page.keyboard.press("Escape")
        wait_dom(page, "() => !document.body.classList.contains('focus-mode')", 5)
        page.set_viewport_size({"width": 1366, "height": 900})

        for locale, title, action, close in (
            ("zh-CN", "对照页已更新", "刷新", "关闭更新提示"),
            ("en", "Review page updated", "Reload", "Dismiss update notice"),
        ):
            page.select_option("#localePick", locale)
            ok(title in page.inner_text("#reloadBanner") and page.inner_text("#reloadBtn") == action
               and page.get_attribute("#reloadClose", "aria-label") == close,
               f"{locale} 的更新说明、刷新与关闭操作均使用对应文案")
            if artifacts:
                output = Path(artifacts)
                output.mkdir(parents=True, exist_ok=True)
                capture_screenshot(page, output / ("update-notice.%s.png" % ("zh" if locale == "zh-CN" else "en")))
            page.set_viewport_size({"width": 360, "height": 740})
            ok(page.is_visible("#reloadBanner") and notice_fits(),
               f"{locale} 在 360px 窄窗口下仍完整显示更新提示与两个操作")
            page.set_viewport_size({"width": 1366, "height": 900})

        # 不等 400ms 草稿防抖，直接点击刷新，覆盖最后一次输入。
        page.fill("#ntext", "合成草稿：点击刷新前的最后一次输入")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click("#reloadBtn")
        wait_mode(page, "server")
        ok(len(navigations) == 1 and not page.is_visible("#reloadBanner"),
           "只有手动点击才刷新页面，载入新版后更新横幅消失")
        ok(page.input_value("#ntext") == "合成草稿：点击刷新前的最后一次输入"
           and page.input_value("#nskills") == "humanizer-zh" and page.input_value("#ntarget") == "讲稿",
           "手动刷新保留当前页未发送批注的最新文字、技能和对象")
        go_page(page, 2)
        ok(page.input_value("#ntext") == "合成草稿：第 2 页尚未发送" and not disk_notes(review),
           "刷新也保留其他页的未发送批注，不会将草稿自动提交")
    finally:
        context.close()
        stop(server)


def server_mode_checks(browser, tmp: Path):
    """场景 server-mode：服务模式全流程与恢复、离线、安全边界（详见 e2e_scenarios 注册表）。"""
    import_md = server_import_export_fixture(tmp)
    # ---------- 3. 服务模式 ----------
    sdir = tmp / "server_mode"
    build(sdir)
    port = free_port()
    srv = start_server(sdir, port)
    ctx = browser.new_context()
    page = ctx.new_page()
    base = f"http://127.0.0.1:{port}/"
    page.goto(base)
    wait_mode(page, "server", condition="首次轮询确认服务后徽标进入服务模式")
    ok("已连接本地服务" in page.inner_text("#mode"), "经服务打开显示「已连接本地服务」")
    ok(page.locator("header kbd").count() == 0 and "P" in page.get_attribute("#pinBtn", "title")
       and "Ctrl" in page.get_attribute("#addBtn", "title"), "快捷键显示在工具提示与说明中")
    go_page(page, 3)
    page.evaluate("""() => { const p = document.querySelector('.talk p'); const r = document.createRange();
            r.selectNodeContents(p); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup')); }""")
    page.keyboard.press("Escape")
    add_note(page, "服务模式批注：讲法第一句太长", pin=(0.3, 0.4))
    ok(wait_until(lambda: (lambda ns: len(ns) == 1 and ns[0]["page"] == 3 and ns[0].get("key") == "sum"
                           and ns[0]["pin"] and ns[0]["quoteFrom"] == "讲稿")(disk_notes(sdir)),
                  page, 5, condition="批注经服务写入 notes.json"),
       "批注即时写入 notes.json（页码、页面键、标点、讲稿引用）")
    ok(page.evaluate("localStorage.getItem('ae.' + RID + '.notes')") is None, "服务模式不在浏览器里留整份批注副本")
    page.click('[data-act="edit"]')
    page.fill("#etext", "服务模式批注（已编辑）")
    page.click('[data-act="save"]')
    ok(wait_until(lambda: disk_notes(sdir)[0]["text"] == "服务模式批注（已编辑）", page, 5,
                  condition="编辑后的正文落盘"), "页面编辑批注后落盘")
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
    moved = wait_until(lambda: (lambda pn: bool(pn) and pn["x"] > old_pin["x"] and pn["y"] > old_pin["y"])(
                           [n for n in disk_notes(sdir) if n["id"] == nid0][0]["pin"]),
                       page, 5, condition="拖动后的标点位置落盘")
    new_pin = [n for n in disk_notes(sdir) if n["id"] == nid0][0]["pin"]
    ok(moved, f"拖动标点：新位置落盘（{old_pin} → {new_pin}）")
    page.hover(f'#nlist [data-note="{nid0}"] .body')
    ok("hl" in (pin.get_attribute("class") or ""), "鼠标移到右侧批注上，对应标点高亮")
    bb = pin.bounding_box()
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    ok(wait_dom(page, "() => document.getElementById('pinpop').classList.contains('on')", 5,
                condition="标点弹窗打开") and "服务模式批注" in page.inner_text("#pinpop"),
       "点击标点弹出这条批注及操作")
    page.keyboard.press("Escape")
    add_note(page, "临时标点批注", pin=(0.6, 0.6))
    wait_until(lambda: any(n["text"] == "临时标点批注" for n in disk_notes(sdir)), page, 5,
               condition="临时标点批注落盘")
    tid = [n for n in disk_notes(sdir) if n["text"] == "临时标点批注"][0]["id"]
    tpin = page.locator(f'#host .ae-pin[data-id="{tid}"]')
    # 落盘先于标点重绘；点击动作等待新标点可见后再进行鼠标交互。
    tpin.click()
    page.click('#pinpop [data-pp="unpin"]')
    ok(wait_until(lambda: [n for n in disk_notes(sdir) if n["id"] == tid][0]["pin"] is None, page, 5,
                  condition="「去掉标注」落盘"), "在标注弹窗里「去掉标注」")
    page.click(f'#nlist [data-note="{tid}"] [data-act="del"]')
    ok(wait_until(lambda: not any(n["id"] == tid for n in disk_notes(sdir)), page, 5,
                  condition="删除的批注从 notes.json 消失"), "删除批注（确认后）")
    # 技能：代理写入技能目录 → 轮询把它应用到页面 → 输入栏有候选 → 批注带上技能
    subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(sdir), "--set", "humanizer-zh=中文润色", "dataviz=图表"],
                   check=True, capture_output=True)
    # 候选目录只经轮询（applyServer → renderSkillList）进入页面：等目录确实应用，而不是睡过一个周期
    wait_dom(page, "() => skillCatalog.some(s => s.name === 'humanizer-zh')", 10,
             condition="轮询把新的技能目录应用到页面（humanizer-zh 进入候选目录）")
    page.click("#nskills")
    ok(page.locator('#skillList [role="option"]').filter(has_text="humanizer-zh").count() == 1, "技能目录出现在「技能」输入栏的候选里")
    go_page(page, 2)   # 放在第 2 页，不影响第 3 页「全部已处理」的检查
    page.fill("#ntext", "这段口气太硬，润色一下")
    page.fill("#nskills", "humanizer-zh, dataviz")
    page.click('[data-act="add"]')
    sk = wait_until(lambda: [n for n in disk_notes(sdir) if n["text"] == "这段口气太硬，润色一下"], page, 5,
                    condition="带技能的批注落盘")
    ok(bool(sk) and sk[0].get("skills") == ["humanizer-zh", "dataviz"] and "⚙ humanizer-zh" in page.inner_text("#nlist"),
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
    ok(wait_until(lambda: json.loads((sdir / "skills.json").read_text()).get("always")
                  == [{"name": "checker-x", "scope": "all"}, {"name": "humanizer-zh", "scope": "讲稿"}], page, 5,
                  condition="常驻技能写入 skills.json"), "常驻技能存进审阅目录的 skills.json，技能目录不受影响")
    ok(wait_until(lambda: "常驻 checker-x" in page.inner_text("#nlist") and page.inner_text("#alwaysN") == "2", page, 8,
                  condition="保存响应后卡片与按钮刷新（常驻技能 2）"),
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
    ok(wait_dom(page, "() => document.getElementById('selbar').textContent.includes('确认删除')", 5,
                condition="第一次 D 后选区条进入「待确认」")
       and not any((n.get("edit") or {}).get("op") == "delete" for n in disk_notes(sdir)),
       "按一次 D 只进入「待确认」，不会误记删除（防止打拼音时误触）")
    page.keyboard.press("d")
    dels = wait_until(lambda: [n for n in disk_notes(sdir) if (n.get("edit") or {}).get("op") == "delete"], page, 5,
                      condition="「删除这段」落盘")
    ok(bool(dels) and dels[0]["target"] == "报告和讲稿" and "毛利率" in dels[0]["edit"]["from"]
       and (dels[0].get("pair") or {}).get("side") == "讲稿" and "平台抽成" in dels[0]["pair"]["text"],
       "再按 D：记下「删除这段」，对象自动为报告和讲稿，并带上讲稿的对应句")
    ok("↔ 讲稿" in page.inner_text("#nlist"), "批注卡片上显示另一侧也要改")
    drag_select(page, page.locator("#pageScript .talk p").nth(1))
    ok(page.is_checked("#pairOn"), "选中讲稿文字：报告里有相近的句子，同样提示同时改")
    page.keyboard.press("b")
    ok(not page.is_checked("#pairOn") and page.input_value("#ntarget") == "讲稿", "按 B 取消同时改，对象回到讲稿")
    page.keyboard.press("r")
    ok(wait_dom(page, "() => document.getElementById('repbox').classList.contains('on')"
                " && document.getElementById('repText').value.includes('平台抽成')", 5,
                condition="改写框弹出并预填选中的原文"), "按 R：弹出改写框，预填选中的原文")
    page.fill("#repText", "平台抽成上调，外卖毛利率降了 2 个百分点。")
    page.keyboard.press("Enter")
    reps = wait_until(lambda: [n for n in disk_notes(sdir) if (n.get("edit") or {}).get("op") == "replace"], page, 5,
                      condition="「改为」落盘")
    ok(bool(reps) and reps[0]["target"] == "讲稿" and reps[0]["edit"]["to"].startswith("平台抽成上调，") and not reps[0].get("pair"),
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
    ok("风格优化 · 框选处" in page.inner_text("#styleChip"), "S → 2 后拖动框选，输入框标明「风格优化 · 框选处」")
    page.fill("#ntext", "表格太挤，加大行距并对齐")
    page.click('[data-act="add"]')
    rg = wait_until(lambda: [n for n in disk_notes(sdir) if n["text"] == "表格太挤，加大行距并对齐"], page, 5,
                    condition="框选区域的风格优化落盘")
    ok(bool(rg) and rg[0]["style"] == {"scope": "region"} and rg[0]["region"]["w"] > 5 and "table" in (rg[0].get("anchor") or ""),
       f"框选区域的风格优化落盘，附位置线索（{(rg[0].get('anchor') or '')[:40] if rg else ''}）")
    reg = page.locator(f'#host .ae-region[data-id="{rg[0]["id"]}"]')
    bb = reg.bounding_box()
    page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    page.mouse.down()
    page.mouse.move(bb["x"] + bb["width"] / 2 + 40, bb["y"] + bb["height"] / 2 + 30, steps=6)
    page.mouse.up()
    ok(wait_until(lambda: (lambda mn: mn["region"]["x"] > rg[0]["region"]["x"] and mn["region"]["w"] == rg[0]["region"]["w"]
                           and mn["style"] == {"scope": "region"})([n for n in disk_notes(sdir) if n["id"] == rg[0]["id"]][0]),
                  page, 5, condition="拖动后的区域位置落盘"), "拖动区域：新位置落盘，大小不变")
    bb = reg.bounding_box()
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    ok(page.is_visible("#pinpop") and "表格太挤" in page.inner_text("#pinpop"), "点击区域弹出这条批注")
    page.keyboard.press("Escape")
    page.keyboard.press("s")
    page.keyboard.press("1")
    page.keyboard.press("Escape")   # 离开输入框
    page.click('[data-act="add"]')
    whole = wait_until(lambda: [n for n in disk_notes(sdir) if (n.get("style") or {}).get("scope") == "page"], page, 5,
                       condition="整页风格优化落盘")
    ok(bool(whole) and whole[0]["text"] == "风格优化：整页" and whole[0]["target"] == "报告" and "整页" in whole[0].get("anchor", ""),
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
    ok(page.locator("#host .ae-region.pending").count() == 1 and page.locator("#host .ae-pin.pending").count() == 0
       and "on" not in (page.get_attribute("#pinBtn", "class") or ""), "标注模式下拖动 = 框选一块区域（不是放点），放好后退出标注模式")
    ok(not page.is_visible("#styleChip"), "普通框选不会变成风格优化")
    page.fill("#ntext", "这块口径要和讲稿一致")
    page.click('[data-act="add"]')
    bx = wait_until(lambda: [n for n in disk_notes(sdir) if n["text"] == "这块口径要和讲稿一致"], page, 5,
                    condition="普通批注的框选落盘")
    ok(bool(bx) and bx[0].get("region") and abs(bx[0]["region"]["w"] - 25) < 2 and not bx[0].get("style") and not bx[0].get("pin")
       and bx[0]["target"] == "报告" and bx[0].get("anchor"), f"普通批注框选：区域与位置线索落盘（{bx[0].get('region') if bx else ''}）")
    bid, r1 = bx[0]["id"], bx[0]["region"]
    ok("框选 " in page.inner_text(f'#nlist [data-note="{bid}"] .meta'), "批注卡片标明「框选 n」")
    rz = page.locator(f'#host .ae-region[data-id="{bid}"] .rz').bounding_box()
    page.mouse.move(rz["x"] + rz["width"] / 2, rz["y"] + rz["height"] / 2)
    page.mouse.down()
    page.mouse.move(rz["x"] + 70, rz["y"] + 45, steps=6)
    page.mouse.up()
    resized = wait_until(lambda: (lambda r2: r2["w"] > r1["w"] and r2["h"] > r1["h"] and r2["x"] == r1["x"] and r2["y"] == r1["y"])(
                             [n for n in disk_notes(sdir) if n["id"] == bid][0]["region"]),
                         page, 5, condition="调整大小后的区域落盘")
    r2 = [n for n in disk_notes(sdir) if n["id"] == bid][0]["region"]
    ok(resized, f"拖框的右下角调大小：落盘，位置不变（{r1} → {r2}）")
    page.click(f'#nlist [data-note="{bid}"] [data-act="edit"]')
    page.click(f'#nlist [data-note="{bid}"] [data-act="remark"]')
    ok("on" in (page.get_attribute("#pinBtn", "class") or ""), "编辑里点「重新标注」进入标注模式")
    page.mouse.click(sec["x"] + sec["width"] * .8, sec["y"] + sec["height"] * .8)
    ok(wait_until(lambda: (lambda rn: bool(rn.get("pin")) and not rn.get("region"))([n for n in disk_notes(sdir) if n["id"] == bid][0])
                  and page.locator(f'#host .ae-pin[data-id="{bid}"]').count() == 1,
                  page, 5, condition="放点替换原来的框（落盘）"), "重新标注时单击 = 放点，原来的框被替换")
    page.click(f'#nlist [data-note="{bid}"] [data-act="unpin"]')
    ok(wait_until(lambda: (lambda rn: not rn.get("pin") and not rn.get("region"))([n for n in disk_notes(sdir) if n["id"] == bid][0])
                  and page.locator(f'#host [data-id="{bid}"]').count() == 0,
                  page, 5, condition="「去掉标注」落盘"), "编辑里「去掉标注」")
    page.click(f'#nlist [data-note="{bid}"] [data-act="remark"]')
    page.click(f'#nlist [data-note="{bid}"] [data-act="cancel"]')
    page.mouse.click(sec["x"] + sec["width"] * .5, sec["y"] + sec["height"] * .9)
    ok(wait_until(lambda: "on" not in (page.get_attribute("#pinBtn", "class") or "")
                  and not [n for n in disk_notes(sdir) if n["id"] == bid][0].get("pin"),
                  page, 5, condition="取消重新标注后退出标注模式"), "点了「重新标注」又取消：退出标注模式，之后随手点报告不会改这条批注")
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
    # 服务端修订只经轮询进入页面：以提示条出现为「轮询已应用」的证据
    ok(wait_dom(page, "() => document.getElementById('resume').textContent.includes('条批注的原页面在新版报告里找不到')", 10,
                condition="轮询应用修订后提示条给出孤立批注数量"),
       "原页面找不到的批注：提示条给出数量")
    page.keyboard.press("Escape")
    page.keyboard.press("n")
    page.select_option(f'#orphanBox select[data-mv="{lost_id}"]', "2")
    kept_destination = True
    for locale in ("en", "zh-CN"):
        page.locator("#localePick").evaluate("(el, value) => {el.value=value;el.dispatchEvent(new Event('change'))}", locale)
        kept_destination = kept_destination and page.input_value(f'#orphanBox select[data-mv="{lost_id}"]') == "2"
    ok(kept_destination, "孤立批注选择目标页后切换语言，保留尚未提交的目标页")
    page.click(f'#orphanBox [data-mvgo="{lost_id}"]')
    ok(wait_until(lambda: (lambda mn: mn["page"] == 2 and not mn.get("orphan"))([n for n in disk_notes(sdir) if n["id"] == lost_id][0]),
                  page, 5, condition="挪动结果落盘"), "在「批注」里把它挪到第 2 页")
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
    box = page.evaluate("""() => { const f = document.getElementById('nform').getBoundingClientRect(), s = document.getElementById('panelBody').getBoundingClientRect();
            return [f.top >= s.top, f.bottom <= s.bottom + 1]; }""")
    ok(all(box), "讲稿很长时，输入框停靠在面板底部，不用滚动就能写")
    go_page(page, 3)
    # 正在写草稿时，Claude 标已处理触发轮询刷新：草稿与焦点都要保住
    page.click("#ntext")
    page.keyboard.type("正在写的草稿")
    nid = [n for n in disk_notes(sdir) if n["text"].startswith("服务模式批注")][0]["id"]
    baseline_rev = page.evaluate("() => rev")
    subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(sdir), nid, "--note", "已拆成两句",
                    "--by", "claude"], check=True, capture_output=True)
    # 不变量前置证据：先证明一次轮询把服务端新修订应用到页面（rev 离开基线），再检查草稿与焦点保住
    refreshed = wait_rev(page, baseline_rev, 10)
    txt = page.inner_text("#nlist")
    ok(refreshed and "已处理 · Claude" in txt and "处理说明：已拆成两句" in txt, "Claude 用 CLI 标已处理后页面自动刷新显示")
    ok(refreshed and page.input_value("#ntext") == "正在写的草稿" and page.evaluate("document.activeElement.id") == "ntext",
       "轮询刷新不打断正在写的草稿（内容与焦点都在）")
    page.keyboard.type("，继续")
    ok(page.input_value("#ntext") == "正在写的草稿，继续", "刷新后继续输入不丢字")
    page.click("#next")
    ok(wait_dom(page, "() => document.getElementById('ntext').value === ''", 5,
                condition="翻到下一页后输入框重置"), "翻到下一页，输入框是该页自己的（空）")
    page.click("#prev")
    ok(wait_dom(page, "() => document.getElementById('ntext').value === '正在写的草稿，继续'", 5,
                condition="翻回来后本页草稿恢复"), "翻回来，草稿还在")
    ok("全部已处理" in page.inner_text("#nlist") or "✓" in page.inner_text("#jump"), "已处理完的页有 ✓ 标记")
    page.fill("#ntext", "")
    # 交给 Agent：代理在后台等待，用户点按钮后等待命令退出（Claude Code 里即唤醒会话）
    waiter = subprocess.Popen([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(sdir), "--timeout", "30",
                               "--interval", "0.3", "--by", "claude"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    CLEANUP.add(waiter)   # wait 就算在提交后才启动，也会先读到已存在的待处理批次并立即退出，无需固定垫等待
    page.keyboard.press("Escape")   # 先离开输入框（在输入框里按 A 是打字）
    page.keyboard.press("a")
    page.fill("#handMsg", "先改第 3 页")
    page.click("#doHand")
    ok(wait_until(lambda: (sdir / "handoff.json").exists()
                  and json.loads((sdir / "handoff.json").read_text())["items"][-1]["message"] == "先改第 3 页",
                  page, 5, condition="提交与附言落盘（handoff.json）"),
       "按 A 打开「交给 Agent」，提交与附言记进审阅目录")
    try:
        wout, _ = waiter.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        waiter.kill()
        wout = ""
    ok(waiter.returncode == 0 and "第 1 批" in wout and "先改第 3 页" in wout and "指定了技能：dataviz、humanizer-zh" in wout,
       "后台等待的 notes.py wait 随即退出，输出提交、附言与要用的技能")
    ok(wait_until(lambda: "Agent 已认领" in page.inner_text("#handState"), page, 10,
                  condition="轮询应用批次状态（Agent 已认领）"), "代理接收后页面显示「Agent 已认领」")
    ok("接手未确认" in page.inner_text("#agentDelivery"), "等待进程退出后，页面如实显示主会话接手未确认")
    fixed = [n for n in disk_notes(sdir) if n["status"] == "open"][0]
    subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(sdir), fixed["id"], "--note", "标题缩短了", "--by", "claude"],
                   check=True, capture_output=True)
    subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(sdir), "--summary", "第 3 页已改"],
                   check=True, capture_output=True)
    ok(wait_until(lambda: "Agent 已处理 · 第 1 批（解决 1 条）" in page.inner_text("#handState")
                  and "第 3 页已改" in (page.get_attribute("#handState", "title") or ""), page, 10,
                  condition="轮询应用收尾状态（Agent 已处理）"),
       "代理收尾后页面显示「Agent 已处理 · 第 1 批（解决 1 条）」与处理摘要")
    page.click("#handState")
    batches_ready = wait_batches(page, "第 1 批", condition="点 Agent 状态后处理记录打开并载入批次")
    page.check("#currentTaskToggle")
    bl = page.inner_text("#batchList") if batches_ready else ""
    ok("第 1 批" in bl and "解决 1 条" in bl and "处理摘要：第 3 页已改" in bl and "处理说明：标题缩短了" in bl and "附言：先改第 3 页" in bl,
       "点 Agent 状态打开处理记录：批次、解决几条、处理摘要、每条的处理说明")
    page.click(f'#batchList [data-goto="{fixed["id"]}"]')
    ok(wait_dom(page, "() => !document.getElementById('batches').classList.contains('on')", 5,
                condition="处理记录关闭")
       and page.input_value("#jump") == str(fixed["page"] - 1), "在处理记录里点一条批注：跳到它所在的页")
    page.keyboard.press("j")
    ok(wait_dom(page, JS_BATCHES_ON, 5, condition="处理记录打开"), "按 J 打开处理记录")
    page.keyboard.press("Escape")
    go_page(page, 1)
    page.reload()
    ok(wait_until(lambda: "上次批注到第" in page.inner_text("#resume")
                  and "Agent 已处理完第 1 批（解决 1 条）" in page.inner_text("#resume"), page, 10,
                  condition="重新打开后的提示条恢复"),
       "重新打开时提示上次批注到哪页、Agent 是否处理完")
    page.keyboard.press("l")
    from datetime import datetime
    def user_time(n):
        return max(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() for v in (n["at"], n.get("updatedAt") or n["at"]))
    last_page = max(disk_notes(sdir), key=user_time)["page"]
    ok(wait_until(lambda: page.inner_text("#stat").startswith(f"第 {last_page} /"), page, 5,
                  condition=f"统计行跳到第 {last_page} 页"), f"按 L 跳到上次批注的页（第 {last_page} 页）")
    time.sleep(1.1)   # 保留的固定等待：跨过文件系统 mtime 粒度，保证重建后 indexMtime 可被轮询观察到变化
    subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), "--rebuild", str(sdir)], check=True, capture_output=True)
    ok(wait_until(lambda: page.is_visible("#reloadBtn"), page, 10,
                  condition="轮询发现对照页更新（顶部横幅显示刷新按钮）"), "Claude 重建对照页后顶部横幅显示刷新按钮")

    # ---------- 4. 断线暂存与补传 ----------
    stop(srv)
    # 断线是事件：下一次轮询连接失败后徽标才变化，以「服务未连接」出现为准，不垫固定等待
    ok(wait_until(lambda: "服务未连接" in page.inner_text("#mode"), page, 10,
                  condition="下一次轮询连接失败（服务未连接）"), "服务停止后页面提示未连接")
    add_note(page, "删", pin=(0.2, 0.2))
    add_note(page, "删", pin=(0.7, 0.7))
    ok(wait_until(lambda: "待补传 2" in page.inner_text("#mode"), page, 10,
                  condition="两条批注的补传尝试都失败并计入待补传"), "断线时两条批注暂存浏览器并显示待补传")
    page.click("#handBtn")
    page.click("#doHand")
    ok(wait_until(lambda: "待补传" in page.inner_text("#handState"), page, 8,
                  condition="断线提交进入页面暂存"), "断线时点「交给 Agent」也先暂存")
    srv = start_server(sdir, port)
    flushed = wait_until(lambda: len([n for n in disk_notes(sdir) if n["text"] == "删"]) == 2, page, 10,
                         condition="断线暂存的两条批注补传落盘")
    dels = [n for n in disk_notes(sdir) if n["text"] == "删"]
    ok(flushed and "已连接" in page.inner_text("#mode"), f"服务恢复后补传，同文不同标点两条都在（{len(dels)} 条）")
    def queued_handoff_saved():
        last = json.loads((sdir / "handoff.json").read_text())["items"][-1]
        return (last["seq"] == 2 and last["status"] == "pending"
                and {n["id"] for n in dels} <= set(last["openIds"]))

    ok(wait_until(queued_handoff_saved, page, 8, condition="批注补传后的提交快照包含这两条批注"),
       "暂存的「交给 Agent」在批注补传之后才提交（提交快照里包含这两条）")
    # 处理记录：用户展开的批次在状态变化重绘后仍展开
    page.keyboard.press("Escape")
    page.keyboard.press("j")
    wait_dom(page, JS_BATCHES_ON, 5, condition="处理记录弹窗打开")
    page.click('#batchList details[data-seq="1"] summary')
    subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(sdir)], check=True, capture_output=True)
    # 「处理中」只会随轮询应用的认领状态出现：它是重绘确实发生过的证据，然后再查展开状态保持
    ok(wait_dom(page, "() => document.getElementById('batchList').textContent.includes('处理中')", 10,
                condition="轮询应用认领状态（处理记录出现「处理中」）")
       and page.evaluate("document.querySelector('#batchList details[data-seq=\"1\"]').open"),
       f"处理记录随状态刷新，用户展开的批次保持展开（{page.inner_text('#batchList')[:120]!r}）")
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
    wait_mode(old, "old", condition="旧服务徽标出现（首次轮询按功能集判定）")
    ok("旧版服务" in old.inner_text("#mode"), "背后是旧版服务：状态处显示「旧版服务」")
    old.keyboard.press("p")
    ob = old.locator("#host section.page").bounding_box()
    old.mouse.move(ob["x"] + 40, ob["y"] + 40)
    old.mouse.down()
    old.mouse.move(ob["x"] + 240, ob["y"] + 160, steps=6)
    old.mouse.up()
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
    wait_dom(page, JS_BATCHES_ON, 5, condition="处理记录弹窗打开")
    page.click("#batches [data-close] kbd")
    ok(wait_dom(page, "() => !document.getElementById('batches').classList.contains('on')", 5,
                condition="处理记录关闭"), "点关闭按钮上的「Esc」标签也能关掉处理记录")
    page.keyboard.press("a")
    page.check('input[name="htarget"][value="review"]')
    page.uncheck("#hSub")
    page.fill("#handMsg", "先初改，我看过再复核")
    page.click("#doHand")
    ok(wait_until(lambda: last_batch()["target"] == "review" and last_batch()["allowSubagents"] is False
                  and last_batch()["status"] == "pending", page, 10,
                  condition="提交选项落盘（handoff.json 最新批次）"),
       "交给 Agent 时选「已初改·待复核」、不允许子代理，选项随提交落盘")
    lb = last_batch()
    ok(page.inner_text("#nflight").startswith("处理中") and page.inner_text("#ncount") == "",
       f"交给 Agent 后，「批注」按钮显示「处理中 N」而不是未处理红点（{page.inner_text('#notesBtn')!r}）")
    cli("claim", str(sdir), "--by", "sonnet-初改")
    target = [n for n in disk_notes(sdir) if n["id"] in lb["openIds"]][0]
    cli("resolve", str(sdir), target["id"], "--note", "标题改短", "--by", "sonnet-初改")
    cli("done", str(sdir), "--summary", "初改 1 条", "--by", "sonnet-初改")
    go_page(page, target["page"])
    state_ready = wait_until(lambda: "已初改 · 待复核" in page.inner_text("#handState") and page.inner_text("#ndraft") == "待复核 1", page, 10,
                             condition="轮询应用初改收尾状态（已初改 · 待复核 / 待复核 1）")
    ok(state_ready,
       f"初改收尾后：状态显示「已初改 · 待复核」，「批注」按钮显示「待复核 1」（{page.inner_text('#handState')!r} / {page.inner_text('#notesBtn')!r}）")
    card = page.inner_text(f'#nlist [data-note="{target["id"]}"]')
    ok("已初改·待复核" in card and "初改说明：标题改短" in card and "痕迹：初改（sonnet-初改 · 第" in card,
       "待复核的批注卡片显示状态、初改说明与处理痕迹")
    page.keyboard.press("j")
    wait_batches(page, f"第 {lb['seq']} 批")
    page.check("#currentTaskToggle")
    batches_ready = wait_batches(page, "要求：做到已初改·待复核",
                                 condition="处理记录打开并载入初改批次")
    bl = page.inner_text("#batchList")
    ok(batches_ready and "初改 · sonnet-初改" in bl and "已初改·待复核的 1 条" in bl,
       "处理记录显示要求、初改阶段与待复核的批注")
    page.click(f'#batchList [data-breview="{lb["seq"]}"]')
    ok(page.is_visible("#hand") and f"请 Agent 复核第 {lb['seq']} 批" in page.inner_text("#handTitle")
       and not page.is_visible("#hTargetRow"), "点「请 Agent 复核」打开复核请求（不再问做到哪一步）")
    page.fill("#handMsg", "重点看标题")
    page.click("#doHand")
    ok(wait_until(lambda: last_batch()["status"] == "review" and last_batch()["reviewMessage"] == "重点看标题", page, 8,
                  condition="复核请求落盘（handoff.json 最新批次）"), "请求复核落盘：等待复核接收")
    wout = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(sdir), "--timeout", "5", "--stage", "review",
                           "--by", "opus-复核"], capture_output=True, text=True).stdout
    ok("收到复核请求" in wout and "重点看标题" in wout and target["id"] in wout, "复核者 wait --stage review 收到复核请求与待复核批注")
    cli("review", str(sdir), target["id"], "--return", "--note", "口径要你定", "--by", "opus-复核")
    cli("done", str(sdir), "--summary", "退回 1 条", "--by", "opus-复核")
    returned = wait_until(lambda: "退回给你" in page.inner_text(f'#nlist [data-note="{target["id"]}"]'), page, 10,
                          condition="轮询应用退回状态（卡片显示「退回给你」）")
    card = page.inner_text(f'#nlist [data-note="{target["id"]}"]')
    ok(returned and "口径要你定" in card and "已初改" not in card.split("痕迹")[0],
       "复核者退回：批注回到未处理，痕迹里写明退回原因")
    page.keyboard.press("Escape")
    page.keyboard.press("a")
    page.check('input[name="htarget"][value="review"]')
    seq_before = last_batch()["seq"]
    page.click("#doHand")
    # 先等这次提交落盘（seq 前进），再让 CLI 认领，避免测试准备与页面写入竞争
    wait_until(lambda: last_batch()["seq"] > seq_before, page, 8, condition="新提交落盘（seq 前进）")
    cli("claim", str(sdir), "--by", "claude")
    cli("resolve", str(sdir), target["id"], "--note", "按你上次说的口径改", "--by", "claude")
    cli("done", str(sdir), "--by", "claude")
    page.keyboard.press("a")
    seq2 = last_batch()["seq"]
    wait_dom(page, 's => !!document.querySelector(`#handDrafts [data-hfinal="${s}"]`)', 10,
             arg=str(seq2), condition="待复核弹窗出现「直接定稿」按钮（轮询应用批次状态后）")
    page.click(f'#handDrafts [data-hfinal="{seq2}"]')
    finalized = wait_until(lambda: last_batch()["status"] == "done", page, 8, condition="直接定稿落盘（批次 done）")
    lb = last_batch()
    fin = [n for n in disk_notes(sdir) if n["id"] == target["id"]][0]
    ok(finalized and lb["status"] == "done" and fin["status"] == "resolved" and fin["trail"][-1]["action"] == "accepted",
       "「交给 Agent」弹窗顶部的「直接定稿」：跳过复核，批注定为已处理并记为你定稿")
    page.keyboard.press("Escape")

    # ---------- 5. 写入失败与坏字符 ----------
    os.chmod(sdir, 0o500)
    try:
        add_note(page, "写盘失败时的批注")
        ok(wait_until(lambda: "写盘失败时的批注" in page.inner_text("#nlist")
                      and "待补传 1" in page.inner_text("#mode"), page, 10,
                      condition="写入失败后批注留在页面并计入待补传"),
           "服务端写入失败（5xx）时批注仍在页面上并进入待补传")
    finally:
        os.chmod(sdir, 0o700)
    ok(wait_until(lambda: any(n["text"] == "写盘失败时的批注" for n in disk_notes(sdir))
                  and "待补传" not in page.inner_text("#mode"), page, 10,
                  condition="恢复可写后轮询补传落盘并清空待补传"), "目录恢复可写后自动补传")
    page.evaluate("() => setQuote('甲'.repeat(299) + '📈尾巴', '讲稿')")
    add_note(page, "引用里有被截断的 emoji")
    add_note(page, "后面一条普通批注")
    ok(wait_until(lambda: (lambda texts: "引用里有被截断的 emoji" in texts and "后面一条普通批注" in texts)(
                      [n["text"] for n in disk_notes(sdir)])
                  and "待补传" not in page.inner_text("#mode"), page, 8,
                  condition="两条批注都落盘且待补传清空"),
       "引用含 emoji 被截断时照常保存，不卡住后面的批注")

    # ---------- 6. 服务卡死 ----------
    os.kill(srv.pid, signal.SIGSTOP)
    try:
        add_note(page, "服务卡住时写的批注")
        # 时间边界：页面 8 秒请求超时真正走完，批注才转待补传——以模式变化为超时发生的证据
        ok(wait_until(lambda: "待补传" in page.inner_text("#mode"), page, 14,
                      condition="请求超时（页面 8 秒上限）后转入待补传"), "服务卡住时请求超时，批注转入待补传")
    finally:
        os.kill(srv.pid, signal.SIGKILL)
        srv.communicate()
    (sdir / ".annotation-server.json").unlink(missing_ok=True)
    srv = start_server(sdir, port)
    ok(wait_until(lambda: any(n["text"] == "服务卡住时写的批注" for n in disk_notes(sdir)), page, 10,
                  condition="重启后轮询补传落盘"), "重启服务后补传到 notes.json")

    # ---------- 7. 同一端口换成另一份审阅 ----------
    other = tmp / "other_review"
    build(other, name="B 稿票面")
    stop(srv)
    srv_b = start_server(other, port)
    add_note(page, "切换后立刻写的 A 稿意见")   # 页面还以为连着 A
    # A 停掉的空档里若刚好轮询过，页面先是「服务未连接」，下一次轮询（4 秒内）才认出端口上换成了 B
    # 若已发出写请求，还要等 flush 的拒绝处理结束；若没有发出，则由新轮询确认不匹配。
    mismatch_settled = wait_dom(page, "() => mode === 'mismatch' && flushing === null", 10,
                               condition="确认服务不匹配且本次补传处理结束")
    ok(mismatch_settled and not any("A 稿" in n["text"] for n in disk_notes(other)),
       "服务刚切换就写，也不会写进 B 稿的 notes.json")
    ok(mismatch_settled and "服务不匹配" in page.inner_text("#mode") and "待补传" in page.inner_text("#mode"),
       "页面显示「服务不匹配」，改动留在待补传")
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
    ok(wait_until(lambda: any(n["text"] == "切换后立刻写的 A 稿意见" for n in disk_notes(sdir)), page, 10,
                  condition="换回本审阅服务后轮询补传落盘"), "换回本审阅的服务后补传到正确的位置")

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
    log_path = sdir / "server.log"
    rejects_before = log_path.read_text(encoding="utf-8").count("[reject]") if log_path.exists() else 0
    epage = ctx.new_page()
    epage.goto(f"http://localhost:{eport}/")  # 另一个来源
    epage.wait_for_function("() => window.results && window.results.done", timeout=10000)
    # 先证明恶意请求确实到达服务并被拒绝（server.log 出现新的 [reject]），再核对什么都没写进去
    got_reject = wait_until(lambda: (log_path.read_text(encoding="utf-8").count("[reject]")
                                     if log_path.exists() else 0) > rejects_before,
                            epage, 10, condition="server.log 出现新的 [reject]（恶意请求被拒）")
    res = epage.evaluate("window.results")
    after = disk_notes(sdir)
    ok(got_reject and len(after) == before and not any("EVIL" in n["text"] for n in after)
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
    shown = wait_dom(page, JS_LIST_TEXT, 8, arg="注入测试", condition="导入的批注显示在列表（不执行脚本）")
    page.keyboard.press("Escape")
    pwned = page.evaluate("[window.__p_id, window.__p_txt, window.__p_q]")
    ok(shown and pwned == [None, None, None] and "注入测试" in page.inner_text("#nlist"), "导入带恶意 id / 内容的批注：照常显示为文字，不执行脚本")
    imported = wait_until(lambda: [n for n in disk_notes(sdir) if "注入测试" in n["text"]], page, 8,
                          condition="导入的批注落盘")
    ok(bool(imported) and imported[0]["id"].replace("-", "").replace("_", "").isalnum(), "恶意 id 落盘前被重新生成")
    page.keyboard.press("n")
    page.click("#openImp")
    page.fill("#impText", import_md)
    page.click("#doImp")
    ok(wait_until(lambda: (lambda texts: "旧版批注甲" in texts and "file 模式批注" in texts)([n["text"] for n in disk_notes(sdir)]),
                  page, 8, condition="粘贴导入的批注落盘"), "粘贴导出的 Markdown 导入到服务端 notes.json")
    resolved = [n for n in disk_notes(sdir) if n["text"] == "file 模式批注"]
    ok(bool(resolved) and resolved[0]["status"] == "resolved", "导入保留已处理状态")
    ctx.close()
    if srv.poll() is None:
        stop(srv)


def external_method_checks(browser, tmp: Path):
    """场景 external-method-loop：外部生成技能共用验收（详见 e2e_scenarios 注册表）。

    明确指定的合成外部技能（fixtures/external-report-skill）被实际调用：运行其
    脚本，可观察的调用效果（产物标记、调用说明）写进核验记录；产物走安装副本的
    同一核验入口与同一记录契约，打开现有对照页渲染、批注。缺依据的外部产物
    （lite 变体）与换成内置方法声明的孪生记录判定完全一致——外部方法不放宽验收。
    确定性产品流程验证，不冒充实际模型的评测（那由 evals/generate-method-* 定义）。
    """
    base = tmp / "external-loop"
    installed = base / "installed-skills" / "annotation-everything"
    env = {**os.environ, "HOME": str(base / "home"),
           "CLAUDE_SKILLS_DIR": str(base / "installed-skills"),
           "AE_HOME": str(base / "ae-home")}
    subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"],
                   env=env, check=True, capture_output=True, text=True, timeout=60)
    scripts = installed / "scripts"
    # 指定的外部技能放进同一合成技能目录：它在本场景里「可用」
    for fixture in ("external-report-skill", "external-report-skill-lite"):
        shutil.copytree(ROOT / "fixtures" / fixture,
                        base / "installed-skills" / fixture, dirs_exist_ok=True)
    case = base / "case"
    (case / "materials").mkdir(parents=True)
    material = case / "materials" / "notes.md"
    material.write_text("# 三季度经营笔记（合成）\n\n- 三季度收入 112 万元\n"
                        "- 上年同期收入 100 万元\n- 外卖渠道占比 38%\n"
                        "- 十月周报缺失，十月趋势暂无法核对\n", encoding="utf-8")

    def cli(tool, *args, expect=0):
        done = subprocess.run([sys.executable, str(scripts / tool), *map(str, args)],
                              env=env, capture_output=True, text=True, timeout=60)
        if expect is not None:
            assert done.returncode == expect, \
                f"{tool} 退出码 {done.returncode}（期望 {expect}）：{done.stderr or done.stdout}"
        return done

    # ── 选定方法：实际调用指定技能（不是只凭目录扫描或名称宣称） ──
    done = subprocess.run(
        [sys.executable, str(base / "installed-skills" / "external-report-skill" / "scripts" / "make_report.py"),
         "--materials", str(material), "--out", str(case)],
        env=env, capture_output=True, text=True, timeout=60)
    ok(done.returncode == 0 and "xs-banner" in done.stdout,
       f"指定的合成技能被实际调用（退出码 {done.returncode}，调用输出带产物标记）")
    report, script = case / "report.html", case / "script.md"
    record_path = case / "report-record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    ok(report.exists() and script.exists() and record_path.exists(),
       "外部技能产出报告、讲稿与核验记录")
    ok(record["method"]["kind"] == "external"
       and record["method"]["name"] == "synthetic-brief-kit"
       and record["method"].get("version")
       and record["method"].get("invocation", "").strip(),
       "记录保留实际生成方法、版本信息与调用说明")

    # ── 相同核验：同一入口、同一记录契约、同一判定语义 ──
    checked = json.loads(cli("report_check.py", str(report), "--script", str(script),
                             "--record", str(record_path), "--locale", "zh-CN",
                             "--json").stdout)
    ok(checked["schema"] == "ae-report-check/1", "外部产物走同一核验入口（ae-report-check/1）")
    ok(checked["verdict"]["draftComplete"] is True and checked["verdict"]["deliverable"] is False,
       "判定语义与内置产物一致：初稿完成、内容与视觉核验未执行不支持可交付")
    ok(checked["record"]["method"]["name"] == "synthetic-brief-kit"
       and checked["record"]["method"]["kind"] == "external",
       "核验结果披露实际生成方法")

    # ── 打开现有对照页：外部版式进同一审阅页，批注与页面键照常工作 ──
    review = case / "brief-review"
    cli("build_viewer.py", "--report", str(report), "--script", str(script),
        "--out", str(review), "--name", "外部技能简报", "--no-scan-skills")
    port = free_port()
    server = start_server(review, port, scripts=scripts)
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000})
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server", condition="对照页进入服务模式")
        page_count = page.locator("#jump option").count()
        ok(page_count == 2, f"外部技能的两页报告进入现有对照页（{page_count} 页）")
        banner = page.evaluate(
            "() => { const r = document.getElementById('host').shadowRoot;"
            "  const b = r.querySelector('.xs-banner');"
            "  return b ? [getComputedStyle(b).backgroundColor, b.textContent] : null; }")
        ok(banner and banner[0] == "rgb(11, 114, 133)" and "SYNTHETIC-BRIEF-KIT" in banner[1],
           f"外部技能自己的版式真实渲染（背景 {banner and banner[0]}，标记 {banner and banner[1]!r}）")
        go_page(page, 1)
        source_seen = page.evaluate(
            "() => Boolean(document.getElementById('host').shadowRoot.querySelector('.ae-source'))")
        ok(source_seen, "外部产物保留依据标注语义类")
        add_note(page, "外部来源标注补同比基数", pin=(0.5, 0.4))
        ok(wait_until(lambda: len(disk_notes(review)) == 1, page, 5), "批注照常落盘")
        note = disk_notes(review)[0]
        ok(note["key"] == "sum" and note["page"] == 1,
           f"批注关联稳定页面键 sum（key={note['key']} page={note['page']}）")
    finally:
        stop(server)
        ctx.close()

    # ── 缺依据的外部产物：缺口如实列出、不可交付；与内置孪生判定一致 ──
    lite = base / "case-lite"
    lite_done = subprocess.run(
        [sys.executable, str(base / "installed-skills" / "external-report-skill-lite" / "scripts" / "make_report.py"),
         "--materials", str(material), "--out", str(lite)],
        env=env, capture_output=True, text=True, timeout=60)
    ok(lite_done.returncode == 0, "lite 变体技能实际运行生成缺依据产物")
    lite_checked = json.loads(cli("report_check.py", str(lite / "report.html"),
                                  "--script", str(lite / "script.md"),
                                  "--record", str(lite / "report-record.json"),
                                  "--locale", "zh-CN", "--json").stdout)
    ok(lite_checked["verdict"]["deliverable"] is False
       and lite_checked["coverage"]["content"] != "covered"
       and any("另行核对" in g for g in lite_checked["gaps"]),
       "缺依据的外部产物：缺口如实列出、覆盖不显示为通过、不可交付")

    def outline(payload):
        return {"checks": [(c["id"], c["status"], c["code"]) for c in payload["checks"]],
                "coverage": payload["coverage"],
                "draftComplete": payload["verdict"]["draftComplete"],
                "deliverable": payload["verdict"]["deliverable"]}

    lite_record = json.loads((lite / "report-record.json").read_text(encoding="utf-8"))
    twin_dir = base / "case-lite-twin"
    twin_dir.mkdir()
    shutil.copy(lite / "report.html", twin_dir / "report.html")
    shutil.copy(lite / "script.md", twin_dir / "script.md")
    twin = dict(lite_record)
    twin["method"] = {"kind": "builtin", "name": "annotation-everything 内置范式",
                      "version": "0.9.0", "unavailable": []}
    twin_path = twin_dir / "report-record.json"
    twin_path.write_text(json.dumps(twin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    twin_checked = json.loads(cli("report_check.py", str(twin_dir / "report.html"),
                                  "--script", str(twin_dir / "script.md"),
                                  "--record", str(twin_path),
                                  "--locale", "zh-CN", "--json").stdout)
    ok(outline(twin_checked) == outline(lite_checked),
       "相同缺陷的内置与外部产物判定完全一致（外部方法不放宽也不加严）")


# 编号与计数的合成审阅：第 3 页两条未处理 + 一条已处理（都带标注），第 4 页两条未处理（一条带标注），
# 第 5 页一条待复核。标点与框在同一页统一按出现顺序编号，已处理的不被排除在编号之外。
POLL_NUMBERING_NOTES = [
    {"id": "numpin000000001", "page": 3, "title": "结论", "target": "报告", "text": "编号甲（点）",
     "quote": "", "quoteFrom": "", "pin": {"x": 10, "y": 10}, "status": "open", "at": "2026-10-01T00:00:01Z"},
    {"id": "numreg000000002", "page": 3, "title": "结论", "target": "报告", "text": "编号乙（框）",
     "quote": "", "quoteFrom": "", "region": {"x": 20, "y": 20, "w": 30, "h": 30}, "anchor": "table「渠道」",
     "status": "open", "at": "2026-10-01T00:00:02Z"},
    {"id": "numres000000003", "page": 3, "title": "结论", "target": "讲稿", "text": "编号丙（已处理）",
     "quote": "", "quoteFrom": "", "pin": {"x": 40, "y": 40}, "status": "resolved", "resolution": "已改好",
     "resolvedBy": "claude", "resolvedAt": "2026-10-02T00:00:00Z", "at": "2026-10-01T00:00:03Z"},
    {"id": "numopen00000004", "page": 4, "title": "成本", "target": "报告", "text": "编号丁",
     "quote": "", "quoteFrom": "", "status": "open", "at": "2026-10-01T00:00:04Z"},
    {"id": "numpin000000005", "page": 4, "title": "成本", "target": "报告", "text": "编号戊（点）",
     "quote": "", "quoteFrom": "", "pin": {"x": 50, "y": 50}, "status": "open", "at": "2026-10-01T00:00:05Z"},
    {"id": "numdrf000000006", "page": 5, "title": "下一步", "target": "报告", "text": "编号己（待复核）",
     "quote": "", "quoteFrom": "", "status": "drafted", "draftBatch": 1, "draftedBy": "claude",
     "draftedAt": "2026-10-02T00:00:00Z", "resolution": "初改说明", "at": "2026-10-01T00:00:06Z"},
]

# 提交状态与续看提示重画时都会重写这几个节点。用 MutationObserver 数 DOM 改动，
# 不碰页面内部函数，所以「没有重画」是外部可观察的行为。
REPAINT_WATCH = """() => {
  window.__repaint = {handState: 0, agentDelivery: 0, resumeText: 0};
  const bump = key => () => { window.__repaint[key] += 1; };
  [['handState', 'handState'], ['agentDelivery', 'agentDelivery'], ['resumeText', 'resumeText']]
    .forEach(pair => {
      const el = document.getElementById(pair[0]);
      if (el) new MutationObserver(bump(pair[1])).observe(el, {childList: true, subtree: true, characterData: true});
    });
}"""


def read_repaint_counter(page):
    """读一次重画计数；多让出一个任务边界，排队的微任务（Observer 回调）随之一并跑完。"""
    return page.evaluate("() => new Promise(r => setTimeout(() => r(window.__repaint), 0))")


def write_json_atomic(path: Path, data) -> None:
    """原子替换 JSON 文件：读者不会看到写了一半的内容。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def mark_notes(d: Path, resolved_id: str, drafted_id: str) -> None:
    """按真实存储操作把两条合成批注标成「已处理」和「已初改·待复核」（notes_store 的同一入口）。"""
    code = ("import sys;sys.path.insert(0,sys.argv[1]);import notes_store as s;from pathlib import Path;"
            "d=Path(sys.argv[2]);"
            "s.set_status(d,sys.argv[3],'resolved',resolution='已改好',by='claude');"
            "s.set_status(d,sys.argv[4],'drafted',by='claude')")
    subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(d), resolved_id, drafted_id],
                   check=True, capture_output=True)


def poll_repaint_numbering(page, d: Path):
    """跳页选项计数、卡片标注序号与页面标点编号，与改动前在同一合成审阅上逐项一致。"""
    ok(wait_until(lambda: len(disk_notes(d)) == len(POLL_NUMBERING_NOTES), page, 10,
                  condition="合成批注已在磁盘上"), "编号：合成批注写入审阅目录")
    labels = wait_until(lambda: (lambda ls: ls if "）" in ls[2] else None)(
        page.evaluate("() => [...document.getElementById('jump').options].map(o => o.textContent)")),
        page, 10, condition="跳页选项带上每页计数")
    ok(labels == ["第 1 页", "第 2 页", "第 3 页（批注 2）", "第 4 页（批注 2）",
                  "第 5 页（待复核 1）", "第 6 页"],
       "跳页选项的计数与改动前一致（%s）" % (labels,))
    go_page(page, 3)
    pins = page.evaluate("() => [...document.querySelector('#host').shadowRoot"
                         ".querySelectorAll('.ae-pin')].map(e => e.textContent)")
    regions = page.evaluate("() => [...document.querySelector('#host').shadowRoot"
                            ".querySelectorAll('.ae-region .lbl')].map(e => e.textContent)")
    ok(pins == ["1", "3"] and regions == ["2"],
       "第 3 页的标点与框按出现顺序编号，已处理的不跳过（标点 %s、框 %s）" % (pins, regions))
    metas = page.evaluate("() => Object.fromEntries([...document.querySelectorAll('#nlist [data-note]')]"
                          ".map(c => [c.dataset.note, c.querySelector('.meta').textContent]))")
    ok("标点 1" in metas.get("numpin000000001", "") and "框选 2" in metas.get("numreg000000002", "")
       and "标点 3" in metas.get("numres000000003", ""),
       "卡片上的标注序号与页面标点编号一致（%s）" % (metas,))


def poll_repaint_checks(browser, tmp: Path):
    """场景 poll-repaint：轮询数据不变不重画，其他触发点照旧立即更新（详见 e2e_scenarios 注册表）。"""
    # ---------- 编号与计数 ----------
    ndir = tmp / "poll_repaint_numbering"
    build(ndir, name="poll-repaint-numbering")
    write_disk_notes(ndir, POLL_NUMBERING_NOTES)
    mark_notes(ndir, "numres000000003", "numdrf000000006")
    port = free_port()
    srv = start_server(ndir, port)
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        ok(wait_mode(page, "server"), "编号：经服务打开")
        poll_repaint_numbering(page, ndir)
    finally:
        ctx.close()
        stop(srv)

    # ---------- 轮询带来的提交状态与续看提示 ----------
    d = tmp / "poll_repaint"
    build(d, name="poll-repaint")
    port = free_port()
    srv = start_server(d, port)
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        ok(wait_mode(page, "server"), "轮询：经服务打开")
        go_page(page, 3)
        add_note(page, "轮询场景的合成批注")
        ok(wait_until(lambda: any(n["text"] == "轮询场景的合成批注" for n in disk_notes(d)), page, 5,
                      condition="批注落盘"), "轮询：先写一条合成批注")
        page.click("#handBtn")
        page.fill("#handMsg", "合成附言：先改第 3 页")
        page.click("#doHand")
        ok(wait_until(lambda: (d / "handoff.json").exists()
                      and json.loads((d / "handoff.json").read_text())["items"][-1]["message"] == "合成附言：先改第 3 页",
                      page, 8, condition="提交与附言落盘"), "轮询：交给 Agent 一批")
        nid = [n for n in disk_notes(d) if n["text"] == "轮询场景的合成批注"][0]["id"]
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(d)], check=True, capture_output=True)
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "resolve", str(d), nid,
                        "--note", "标题缩短了", "--by", "claude"], check=True, capture_output=True)
        subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(d), "--summary", "第 3 页已改"],
                       check=True, capture_output=True)
        ok(wait_until(lambda: "Agent 已处理 · 第 1 批（解决 1 条）" in page.inner_text("#handState"), page, 10,
                      condition="轮询应用收尾状态"), "轮询：代理收尾后提交状态经轮询更新")
        go_page(page, 2)
        page.evaluate(REPAINT_WATCH)
        before = read_repaint_counter(page)
        ok(wait_poll(page, 12, condition="第一次轮询往返") and wait_poll(page, 12, condition="第二次轮询往返"),
           "轮询：期间发生两次轮询往返")
        after = read_repaint_counter(page)
        ok(before == after,
           "轮询数据不变时不重画提交状态与续看提示（%s → %s）" % (before, after))
        # 最新一批只改摘要字段：提交状态照旧重画（handSig 那几个字段没变也要画）
        hand = json.loads((d / "handoff.json").read_text())
        hand["items"][-1]["summary"] = "改后的合成摘要"
        write_json_atomic(d / "handoff.json", hand)
        ok(wait_until(lambda: "改后的合成摘要" in (page.get_attribute("#handState", "title") or ""), page, 12,
                      condition="轮询应用改后的摘要"), "最新一批只改摘要字段时，提交状态重画")
        # 本地写批注、翻页、切换界面语言：续看提示仍立即更新（不等下一轮轮询）
        go_page(page, 4)
        add_note(page, "轮询后的本地批注")
        ok("上次批注到第 4 页" in page.inner_text("#resume"),
           "轮询数据不变时，本地写一条批注后续看提示立即更新")
        go_page(page, 2)
        ok("上次批注到第 4 页" in page.inner_text("#resume") and page.is_visible("#resumeGo"),
           "轮询数据不变时，翻页后续看提示立即更新")
        page.select_option("#localePick", "en")
        ok("Last annotation on page 4" in page.inner_text("#resume"),
           "轮询数据不变时，切换界面语言后续看提示立即更新")
        page.select_option("#localePick", "zh-CN")
    finally:
        ctx.close()
        stop(srv)

    # ---------- 别的标签页写批注：storage 事件后本页提示更新 ----------
    fdir = tmp / "poll_repaint_file"
    build(fdir, name="poll-repaint-file")
    url = (fdir / "index.html").as_uri()
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(url)
        ok("本地文件模式" in page.inner_text("#mode"), "storage：file:// 打开")
        ok(not page.is_visible("#resume"), "storage：没有批注时不显示续看提示")
        page2 = ctx.new_page()
        page2.goto(url)
        go_page(page2, 4)
        add_note(page2, "另一个标签页的批注")
        ok(wait_until(lambda: "上次批注到第 4 页" in page.inner_text("#resume"), page, 10,
                      condition="storage 事件后续看提示更新"),
           "storage：别的标签页写批注后，本页的续看提示立即更新")
        page2.close()
        legacy = [{"id": "stgold000000001", "page": 4, "title": "成本", "target": "报告",
                   "text": "旧版批注甲", "quote": "", "quoteFrom": "", "pin": None,
                   "at": "2026-10-27T01:00:00Z"},
                  {"id": "stgold000000002", "page": 5, "title": "下一步", "target": "讲稿",
                   "text": "旧版批注乙", "quote": "", "quoteFrom": "", "pin": None,
                   "at": "2026-10-27T01:01:00Z"}]
        page.evaluate("v => localStorage.setItem('v7viewer.notes.viewer_r2', JSON.stringify(v))", legacy)
        page.reload()
        ok(wait_until(lambda: page.is_visible("#legacyBtn") and "（2）" in page.inner_text("#legacyBtn"),
                      page, 10, condition="发现 2 条旧版批注"),
           "storage：发现旧版对照页的 2 条批注，显示导入按钮")
        page3 = ctx.new_page()
        page3.goto(url)
        go_page(page3, 4)
        add_note(page3, "旧版批注甲")   # 同页、同对象、同正文：与旧版那条去重键相同
        ok(wait_until(lambda: not page.is_visible("#legacyBtn") or "（1）" in page.inner_text("#legacyBtn"),
                      page, 12, condition="storage 事件后旧批注提示更新"),
           "storage：别的标签页写下同内容批注后，本页的旧批注提示更新")
        page3.close()
    finally:
        ctx.close()


def analysis_smoke_checks(browser, tmp: Path):
    """从源批注发起分析、确认和作答、交出、转出草稿并确认的真实页面闭环。"""
    review = tmp / "analysis-smoke"
    build(review, name="合成分析审阅")
    server = start_server(review, free_port())
    port = json.loads((review / ".annotation-server.json").read_text())["port"]
    context = browser.new_context(viewport={"width": 1440, "height": 1000})

    def cli(*args, stdin=None):
        result = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), *args],
                                input=stdin, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr + result.stdout)
        return json.loads(result.stdout)

    try:
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server")
        page.wait_for_function("() => analysisSupported")
        add_note(page, "增长主要来自新客，这个结论站得住吗？")
        page.click("#handBtn")
        page.click("#doHand")
        page.wait_for_function("() => hand?.latest?.status==='pending'")
        page.locator('#nlist [data-act="analysis-start"]').click()
        page.wait_for_selector("#analysisMain:not([hidden])")
        ok(page.url.endswith("#analysis"), "从批注发起后切到 #analysis")
        ok("增长主要来自新客" in page.inner_text("#analysisFocus"), "分析顶部保留焦点问题")
        ok(disk_notes(review)[0]["status"] == "held", "源批注搁住，不再属于待改稿")
        page.select_option("#localePick", "en")
        ok(page.inner_text("#handBtn") == "Hand to the agent for analysis", "分析模式主按钮按英文显示")
        selection = page.evaluate("({page:i,line:analysisState.line})")
        card = page.locator("#analysisBody [data-analysis-page]").first
        referenced_page = int(card.get_attribute("data-analysis-page"))
        expected_page = page.evaluate("""number=>{const copy=document.createElement('div');
            copy.innerHTML=SECT[P.findIndex(page=>page.n===number)];return copy.innerHTML}""", referenced_page)
        card.click()
        page.wait_for_selector("#analysisPageDialog.on")
        preview = page.locator("#analysisPageHost").evaluate("host=>host.shadowRoot.getElementById('ae-stage').innerHTML")
        ok(preview == expected_page and page.locator("#analysisPageDialog iframe").count() == 0,
           "引用页浮层复用正确的内嵌报告页，不创建 iframe")
        ok(page.evaluate("({page:i,line:analysisState.line})") == selection,
           "预览引用页不改变报告当前页或分析线")
        page.keyboard.press("Escape")
        page.wait_for_selector("#analysisPageDialog.on", state="hidden")
        ok(page.url.endswith("#analysis") and page.locator("#analysisMain").is_visible(),
           "Esc 只关闭引用页浮层并留在本轮分析")

        opening = cli("claim", str(review), "--events", "analysis", "--round", "1", "--by", "Synthetic")
        proposal = [{"key": "caliber", "text": "采用年度还是季度口径？", "short": "核验口径", "recommend": "annual",
                     "options": [{"id": "annual", "label": "年度"}, {"id": "quarter", "label": "季度"}]}]
        cli("analysis-writeback", str(review), "--round", "1", "--base-rev", str(opening["baseRev"]),
            "--proposals", "-", "--note", "先确认核验口径。", stdin=json.dumps(proposal, ensure_ascii=False))
        page.wait_for_selector('#analysisBody [data-key="S:caliber"][data-kind="accept"]')
        page.locator('#analysisBody [data-key="S:caliber"][data-kind="accept"]').click()
        page.wait_for_selector('#analysisBody [data-key^="Q:"][data-kind="accept"]')
        ok("核验口径" in page.inner_text("#analysisTree"), "确认提议后问题进入树")
        page.locator('#analysisBody [data-key^="Q:"][data-kind="accept"]').click()
        page.wait_for_selector("#analysisBody .analysis-answers")
        page.click("#handBtn")
        page.wait_for_function("() => analysisCurrentRound(analysisState)?.status==='submitted'")
        ok("waiting 1" in page.inner_text("#analysisQueue"), "分析队列独立显示等待轮次")
        claimed = cli("claim", str(review), "--events", "analysis", "--round", "2", "--by", "Synthetic")
        cli("analysis-writeback", str(review), "--round", "2", "--base-rev", str(claimed["baseRev"]),
            "--proposals", "-", "--note", "经核验采用年度口径。", stdin="[]")
        page.wait_for_function("() => analysisState?.rounds?.[1]?.status==='done'")
        page.locator('#analysisBody [data-analysis-act="convert"]').click()
        page.fill("#analysisDraftText", "将增长结论改为经核验的年度口径。")
        page.click("#analysisDraftCreate")
        page.wait_for_selector('#analysisBody [data-analysis-act="confirm"]')
        ok(len(disk_notes(review)) == 1, "草稿确认前不进入批注存储")
        page.locator('#analysisBody [data-analysis-act="confirm"]').click()
        page.wait_for_function("() => analysisState?.sourceState==='resolved'")
        notes_after = disk_notes(review)
        ok(len(notes_after) == 2 and notes_after[0]["status"] == "resolved"
           and notes_after[1]["status"] == "open", "确认草稿后新增未处理批注并处理源批注")
        handoff = json.loads((review / "handoff.json").read_text())
        ok(len(handoff["items"]) == 1 and handoff["items"][0]["status"] == "void",
           "源批注移出空批后批次作废，转出新批注不自动提交改稿")
        page.reload()
        page.wait_for_selector("#analysisMain:not([hidden])")
        page.wait_for_function("() => analysisState?.sourceState==='resolved'")
        ok(page.url.endswith("#analysis") and "Round 2" in page.inner_text("#analysisTimeline"),
           "刷新仍保持分析模式，问答时间线可回看")
        page.click("#annotationMode")
        page.wait_for_function("() => !document.body.classList.contains('analysis-mode')")
        ok(page.inner_text("#handBtn") == "Hand to the agent for revision", "回到批注后主按钮改为改稿")

        # #112：从页发起无源分析，原话草稿跨语言/轮询保留，查证/假设与搁置区接通。
        page.click("#pageAnalysisStart")
        page.fill("#analysisStartText", "本页的合成结论是否可靠？")
        page.click("#analysisStartCreate")
        page.wait_for_function("() => analysisState?.focus==='本页的合成结论是否可靠？'")
        ok(len(disk_notes(review)) == 2, "从页发起分析不伪造或搁住批注")
        opening_seq = page.evaluate("analysisCurrentRound(analysisState).seq")
        opening = cli("claim", str(review), "--events", "analysis", "--round", str(opening_seq), "--by", "Synthetic")
        proposals = [{"key": key, "text": text, "short": text, "recommend": "a",
                      "options": [{"id": "a", "label": "合成推荐"}, {"id": "b", "label": "合成备选"}]}
                     for key, text in (("text", "采用哪个口径？"), ("park", "是否另查旁支？"), ("verify", "原始材料记载多少？"))]
        cli("analysis-writeback", str(review), "--round", str(opening_seq), "--base-rev", str(opening["baseRev"]),
            "--proposals", "-", stdin=json.dumps(proposals, ensure_ascii=False))
        for key in ("text", "park", "verify"):
            page.wait_for_selector(f'#analysisBody [data-key="S:{key}"][data-kind="accept"]')
            page.locator(f'#analysisBody [data-key="S:{key}"][data-kind="accept"]').click()
        text_question = page.locator('[data-analysis-item="Q:n1"]')
        text_question.locator("summary").click()
        text_question.locator("textarea").fill("按月核验，保留异常月份。")
        page.select_option("#localePick", "zh-CN")
        page.evaluate("analysisRenderKey='';renderAnalysis()")
        ok(page.input_value('[data-analysis-item="Q:n1"] textarea') == "按月核验，保留异常月份。",
           "自由文本草稿跨语言切换与重绘保留")
        page.locator('[data-key="Q:n1"][data-kind="text"]').click()
        page.wait_for_function("() => !!analysisState?.nodes?.n1?.decision")
        page.locator('[data-key="Q:n2"][data-kind="park"]').click()
        page.wait_for_function("() => analysisState?.nodes?.n2?.status==='parked'")
        ok("是否另查旁支" in page.inner_text("#analysisParkedSide"), "旁支进入侧栏搁置区")
        page.locator('[data-key="Q:n3"][data-kind="verify"]').click()
        page.wait_for_function("() => analysisCurrentRound(analysisState)?.answers?.['Q:n3']?.kind==='verify'")
        ok(page.evaluate("analysisCurrentRound(analysisState).answers['Q:n3'].allowExternal===false"),
           "页面查证默认不允许外部资料")
        round_seq = page.evaluate("analysisCurrentRound(analysisState).seq")
        decision_id = page.evaluate("analysisState.nodes.n1.decision")
        page.click("#handBtn")
        page.wait_for_function("() => analysisCurrentRound(analysisState)?.status==='submitted'")
        claimed = cli("claim", str(review), "--events", "analysis", "--round", str(round_seq), "--by", "Synthetic")
        payload = {"proposals": [], "assumptions": [{"key": "scope", "text": "各月统计范围一致。", "decisions": [decision_id]}],
                   "facts": [{"key": "revenue", "text": "合成底稿记载收入为120。", "source": "合成底稿第2页", "node": "n3"}]}
        cli("analysis-writeback", str(review), "--round", str(round_seq), "--base-rev", str(claimed["baseRev"]),
            "--proposals", "-", stdin=json.dumps(payload, ensure_ascii=False))
        page.wait_for_selector('[data-key="A:a1"][data-kind="accept"]')
        page.locator('[data-key="A:a1"][data-kind="accept"]').click()
        page.wait_for_function("() => analysisState?.assumptions?.a1?.confirmed?.main==='confirmed'")
        page.click('[data-analysis-tab="facts"]')
        ok("收入为120" in page.inner_text("#analysisFacts"), "查证事实与出处在底部页签显示")
        page.click('[data-analysis-tab="assumptions"]')
        ok("各月统计范围一致" in page.inner_text("#analysisAssumptions"), "假设按线确认并可回看")
        page.click('[data-analysis-status="pause"]')
        page.wait_for_function("() => analysisState?.status==='paused'")
        page.click('[data-analysis-status="activate"]')
        page.wait_for_function("() => analysisState?.status==='active'")
    finally:
        context.close()
        stop(server)


def analysis_ui_checks(browser, tmp: Path):
    """两个模式的提交文案、分析标识隐藏与旧服务退化；只用合成内容。"""
    review = tmp / "analysis-ui"
    build(review, name="合成分析界面核对", locale="en")
    server = start_server(review, free_port())
    port = json.loads((review / ".annotation-server.json").read_text())["port"]
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")

    def cli(*args, stdin=None):
        result = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), *args],
                                input=stdin, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr + result.stdout)
        return json.loads(result.stdout)

    def without_analysis(route):
        response = route.fetch()
        data = response.json()
        data["features"] = [feature for feature in data.get("features", []) if feature != "analysis"]
        route.fulfill(response=response, json=data)

    try:
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{port}/")
        wait_mode(page, "server")
        page.wait_for_function("() => analysisSupported")
        add_note(page, "合成待改稿批注，保留在批注模式。")
        for locale, label in (("en", "Hand to the agent for revision"), ("zh-CN", "交给 Agent 改稿")):
            page.select_option("#localePick", locale)
            ok(page.inner_text("#handBtn") == label, f"{locale} 批注模式主按钮明确用于改稿")
            page.click("#handBtn")
            page.wait_for_selector("#hand.on")
            ok(page.inner_text("#doHandLabel") == label, f"{locale} 批注提交窗口保留改稿文案")
            page.keyboard.press("Escape")

        page.click("#pageAnalysisStart")
        page.fill("#analysisStartText", "合成增长判断采用哪个口径？")
        page.click("#analysisStartCreate")
        page.wait_for_function("() => analysisState?.focus==='合成增长判断采用哪个口径？'")
        opening_seq = page.evaluate("analysisCurrentRound(analysisState).seq")
        opening = cli("claim", str(review), "--events", "analysis", "--round", str(opening_seq), "--by", "Synthetic")
        proposal = [{"key": "private-ui-question", "text": "采用年度还是季度口径？", "short": "合成口径问题", "recommend": "annual",
                     "options": [{"id": "annual", "label": "年度"}, {"id": "quarter", "label": "季度"}]}]
        cli("analysis-writeback", str(review), "--round", str(opening_seq), "--base-rev", str(opening["baseRev"]),
            "--proposals", "-", stdin=json.dumps(proposal, ensure_ascii=False))
        page.wait_for_selector('[data-key="S:private-ui-question"][data-kind="accept"]')
        page.locator('[data-key="S:private-ui-question"][data-kind="accept"]').click()
        question = page.locator('#analysisBody [data-analysis-item^="Q:"]').first
        question.wait_for()
        question_key = question.get_attribute("data-analysis-item")
        node_id = question_key[2:]
        question.locator('[data-kind="accept"]').click()
        page.wait_for_function("id=>!!analysisState?.nodes?.[id]?.decision", arg=node_id)
        round_seq = page.evaluate("analysisCurrentRound(analysisState).seq")
        decision = page.evaluate("id=>analysisState.nodes[id].decision", node_id)
        page.click("#handBtn")
        page.wait_for_function("() => analysisCurrentRound(analysisState)?.status==='submitted'")
        claimed = cli("claim", str(review), "--events", "analysis", "--round", str(round_seq), "--by", "Synthetic")
        payload = {"proposals": [], "assumptions": [{"key": "private-ui-assumption", "text": "合成两期统计范围一致。", "decisions": [decision]}]}
        cli("analysis-writeback", str(review), "--round", str(round_seq), "--base-rev", str(claimed["baseRev"]),
            "--proposals", "-", stdin=json.dumps(payload, ensure_ascii=False))
        assumption_question = page.locator('#analysisBody [data-analysis-item^="A:"]').first
        assumption_question.wait_for()
        assumption_id = assumption_question.get_attribute("data-analysis-item")[2:]
        assumption_question.locator('[data-kind="accept"]').click()
        page.wait_for_function("id=>analysisState?.assumptions?.[id]?.confirmed?.main==='confirmed'", arg=assumption_id)
        round_seq = page.evaluate("analysisCurrentRound(analysisState).seq")
        page.click("#handBtn")
        page.wait_for_function("() => analysisCurrentRound(analysisState)?.status==='submitted'")
        claimed = cli("claim", str(review), "--events", "analysis", "--round", str(round_seq), "--by", "Synthetic")
        payload = {"proposals": [], "deadEnds": [{"key": "private-ui-dead-end", "kind": "option", "node": node_id, "option": "quarter",
                                                 "reason": "合成季度口径缺少可比范围。", "assumptions": [assumption_id]}]}
        cli("analysis-writeback", str(review), "--round", str(round_seq), "--base-rev", str(claimed["baseRev"]),
            "--proposals", "-", stdin=json.dumps(payload, ensure_ascii=False))
        page.wait_for_selector('#analysisBody [data-key^="D:"][data-kind="accept"]')
        page.click('[data-analysis-tab="assumptions"]')
        for locale, analysis_label, revision_label in (("en", "Hand to the agent for analysis", "Hand to the agent for revision"),
                                                        ("zh-CN", "交给 Agent 分析", "交给 Agent 改稿")):
            page.select_option("#localePick", locale)
            ok(page.inner_text("#handBtn") == analysis_label, f"{locale} 分析模式主按钮明确用于分析")
            visible = " ".join(page.inner_text(selector) for selector in ("#analysisSidebar", "#analysisMain", "#analysisPanel"))
            private_ids = page.evaluate("""() => [analysisState.id,...Object.keys(analysisState.nodes),
                ...Object.keys(analysisState.decisions),...Object.keys(analysisState.assumptions),...Object.keys(analysisState.deadEnds),
                ...analysisState.rounds.flatMap(round=>(round.items||[]).map(item=>item.key)),
                'private-ui-question','private-ui-assumption','private-ui-dead-end']""")
            ok(all(identifier not in visible for identifier in private_ids), f"{locale} 分析可见内容只显示原话和自然名称")
            page.click("#annotationMode")
            page.wait_for_function("() => !document.body.classList.contains('analysis-mode')")
            ok(page.inner_text("#handBtn") == revision_label, f"{locale} 回到批注恢复改稿文案")
            page.click("#analysisMode")
            page.wait_for_selector("#analysisMain:not([hidden])")

        page.route("**/api/notes", without_analysis)
        page.reload()
        wait_mode(page, "server")
        page.wait_for_function("() => analysisChecked&&!analysisSupported")
        ok(page.locator("#modeSwitch").is_hidden() and page.locator("#pageAnalysisStart").is_hidden()
           and page.locator("#analysisMain").is_hidden(), "旧服务隐藏分析切换与发起入口")
        page.wait_for_function("() => !busy")
        page.evaluate("() => poll()")   # 再完成一轮真实轮询，升级提示仍应可读，不能只捕获瞬时文字。
        upgrade_text, revision_label = page.inner_text("#toast"), page.inner_text("#handBtn")
        ok("本地服务不支持分析" in upgrade_text and revision_label == "交给 Agent 改稿",
           f"旧服务安全退回批注模式并给出可读的升级提示（实际提示 {upgrade_text!r}，按钮 {revision_label!r}）")
        add_note(page, "合成旧服务批注仍可保存。")
        ok(wait_dom(page, "() => notes.some(note=>note.text==='合成旧服务批注仍可保存。')&&!pend.get().length", 8)
           and len(disk_notes(review)) == 2, "不支持分析时原批注流程仍可使用")
    finally:
        try:
            # 先等拦截响应读完，避免关闭上下文的错误泄漏到下一场景。
            for routed_page in context.pages:
                routed_page.unroute_all(behavior="wait")
        finally:
            context.close()
            stop(server)


SCENARIO_RUNNERS = {
    "console-layout": console_layout_checks,
    "update-notice": update_notice_checks,
    "focus-mode": focus_mode_checks,
    "analysis-smoke": analysis_smoke_checks,
    "analysis-ui": analysis_ui_checks,
    "receiver-binding": receiver_binding_checks,
    "generated-draft-loop": generated_draft_checks,
    "report-verdict-loop": report_verdict_checks,
    "revision-update-loop": revision_update_checks,
    "legacy-report-compat": legacy_report_checks,
    "external-method-loop": external_method_checks,
    "installed-delivery": installed_delivery_checks,
    "locale-live-controls": locale_live_controls_checks,
    "skill-search": skill_search_checks,
    "skill-recommendations": skill_recommendation_checks,
    "skill-i18n": skill_i18n_checks,
    "full-check": full_check_checks,
    "locale-loop": locale_checks,
    "english-script": english_script_checks,
    "handoff-i18n": handoff_i18n_checks,
    "finalize-all": finalize_all_checks,
    "current-task-controls": current_task_controls_checks,
    "export-import": export_import_checks,
    "file-mode": file_mode_checks,
    "review-overview": review_overview_checks,
    "css-fidelity": css_fidelity_checks,
    "docx-review": docx_review_checks,
    "server-mode": server_mode_checks,
    "poll-repaint": poll_repaint_checks,
}

SCENARIO_STATUS_LABELS = {"passed": "通过", "failed": "失败", "error": "错误",
                          "interrupted": "中断", "not_run": "未运行"}


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run_parallel_browser(request):
    """整组持有原 host-state 锁；每个场景独享进程、浏览器和全部合成用户目录。"""
    import verify_parallel
    import verify

    # 首先启动实测最长的场景；最终清单仍保持注册顺序，局部入口仍按序执行。
    priority = {"server-mode": 0, "receiver-binding": 1, "current-task-controls": 2,
                "full-check": 3, "handoff-i18n": 4, "installed-delivery": 5}
    ordered = sorted(request.plan, key=lambda sid: priority.get(sid, 6))

    def worker(sid, _output):
        # fork 不得继承父进程登记的目录所有权，更不能清理其他场景的用户目录。
        CLEANUP._dirs = []
        CLEANUP._procs = []
        CLEANUP.add_dir(Path(os.environ["HOME"]))
        CLEANUP.add_dir(Path(os.environ["AE_HOME"]))
        try:
            return main(["--only", sid], allow_parallel=False)
        finally:
            CLEANUP.stop_all()

    records, environments = {}, []
    interrupted, failed, interrupted_signal = False, False, signal.SIGINT
    with tempfile.TemporaryDirectory(prefix="ae-browser-workers-") as temp:
        try:
            results = verify_parallel.run_tasks([(sid, sid) for sid in ordered], worker, Path(temp))
        except verify_parallel.InterruptedRun as exc:
            results, interrupted, interrupted_signal = exc.results, True, exc.signum
        for sid in request.plan:
            item = results.get(sid)
            if item is None:
                records[sid] = {"id": sid, "status": "not_run", "checks": {"total": 0, "passed": 0, "failed": 0},
                                "started_at": None, "ended_at": None, "seconds": None}
                failed = True
                continue
            log = item["log"].read_text(encoding="utf-8", errors="replace")
            print("=== 独立浏览器进程 %s（退出码 %s）===" % (sid, item["exit_status"]), flush=True)
            print(log, end="", flush=True)
            summary = verify.parse_scenario_summary(log)
            environment = verify.parse_browser_environment(log)
            environments.append(environment)
            if (summary and summary.get("plan") == [sid] and len(summary["scenarios"]) == 1
                    and summary["scenarios"][0].get("id") == sid):
                record = dict(summary["scenarios"][0])
                if item["exit_status"] != 0 and record["status"] == "passed":
                    record.update(status="error", message="场景进程未正常退出")
            else:
                record = {"id": sid, "status": "error", "checks": {"total": 0, "passed": 0, "failed": 0},
                          "started_at": None, "ended_at": None, "seconds": item["seconds"],
                          "message": "场景进程没有留下完整且匹配的执行清单"}
            records[sid] = record
            failed = failed or item["exit_status"] != 0 or record["status"] != "passed"
    if environments and environments[0] and all(env == environments[0] for env in environments):
        environment = environments[0]
    else:
        environment = {"schema": "ae-browser-environment/1", "unavailable": "worker-version-mismatch-or-missing"}
    print("AE-BROWSER-ENV: " + json.dumps(environment, ensure_ascii=False), flush=True)
    total = sum(record["checks"]["total"] for record in records.values())
    passed = sum(record["checks"]["passed"] for record in records.values())
    print("%d/%d 通过" % (passed, total), flush=True)
    print("AE-SCENARIOS: " + json.dumps({"mode": request.mode, "source": request.source,
        "requested": list(request.requested), "plan": list(request.plan),
        "scenarios": [records[sid] for sid in request.plan],
        "execution": {"mode": "isolated-processes/1", "jobs": 3}}, ensure_ascii=False), flush=True)
    return 128 + interrupted_signal if interrupted else 1 if failed else 0


def main(argv=None, *, allow_parallel=True) -> int:
    # 参数解析、选择与注册表一致性检查都发生在启动浏览器之前
    try:
        request = scenarios.resolve_request(sys.argv[1:] if argv is None else argv)
    except scenarios.UsageError as exc:
        print("浏览器场景参数错误：%s" % exc, file=sys.stderr)
        return 2
    problem = scenarios.ensure_runners(SCENARIO_RUNNERS)
    if problem is not None:
        # 一致性检查先于 --list 返回：注册表漏挂执行函数时清单也不许照常展示
        print("场景注册表与执行函数不一致：%s" % problem, file=sys.stderr)
        return 2
    if request.listing:
        print(scenarios.format_listing())
        return 0
    print(scenarios.format_plan(request) + "\n", flush=True)

    if (allow_parallel and request.mode == "full" and os.environ.get("AE_VERIFY_SERIAL") != "1"
            and all(scenarios.scenario_by_id(sid).parallel_safe for sid in request.plan)):
        return run_parallel_browser(request)

    tmp = Path(tempfile.mkdtemp(prefix="ae-e2e-"))
    CLEANUP.add_dir(tmp)
    records: list[dict] = []
    ran: set[str] = set()
    interrupted = False
    startup_error = None

    def record_sigint(signum, frame):
        nonlocal interrupted
        interrupted = True
        signal.default_int_handler(signum, frame)

    previous_sigint = signal.signal(signal.SIGINT, record_sigint)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                import importlib.metadata
                browser_environment = {"schema": "ae-browser-environment/1", "browser": "chromium", "channel": "chrome",
                                       "version": browser.version, "playwright": importlib.metadata.version("playwright")}
            except Exception as exc:
                # 采集不改变场景行为；无法确认版本时保留缺口，不用于性能比较。
                browser_environment = {"schema": "ae-browser-environment/1", "unavailable": type(exc).__name__}
            print("AE-BROWSER-ENV: " + json.dumps(browser_environment, ensure_ascii=False), flush=True)
            try:
                for sid in request.plan:
                    scenario = scenarios.scenario_by_id(sid)
                    base = len(RESULTS)
                    started = time.monotonic()
                    started_wall = iso_now()
                    print("=== 场景 %s：%s ===" % (sid, scenario.title), flush=True)
                    status, message = "passed", None
                    e2e_waits.set_scenario(sid)   # 等待超时的诊断要能报出场景 ID
                    try:
                        SCENARIO_RUNNERS[sid](browser, tmp)
                    except KeyboardInterrupt:
                        status, interrupted = "interrupted", True
                    except Exception as exc:   # 场景互相隔离：记下错误继续后面的场景
                        if interrupted:
                            # Ctrl-C 的 SIGINT 同时到达本进程与浏览器驱动，驱动先死时
                            # TargetClosedError 会先于 KeyboardInterrupt 抛出：按中断处理，
                            # 只有实际收到 SIGINT 才算取消；浏览器意外断线仍是错误
                            status, interrupted = "interrupted", True
                        else:
                            status = "error"
                            message = "%s: %s" % (type(exc).__name__, exc)
                            traceback.print_exc()
                    finally:
                        e2e_waits.set_scenario(None)
                    done = RESULTS[base:]
                    failed = sum(1 for good, _ in done if not good)
                    if status == "passed" and failed:
                        status = "failed"
                    if status == "passed" and not done:
                        status, message = "error", "场景没有执行任何检查"
                    records.append({"id": sid, "status": status,
                                    "checks": {"total": len(done), "passed": len(done) - failed,
                                               "failed": failed},
                                    "started_at": started_wall, "ended_at": iso_now(),
                                    "seconds": round(time.monotonic() - started, 6)})
                    if message:
                        records[-1]["message"] = message
                    if status == "error":
                        # 错误场景遗留的服务器立即停掉，不留到进程退出；目录仍由退出清理统一收
                        CLEANUP.stop_procs()
                    ran.add(sid)
                    print("--- 场景 %s 结束：%s（检查 %d/%d，%.1f 秒）---\n" % (
                        sid, SCENARIO_STATUS_LABELS[status], len(done) - failed, len(done),
                        records[-1]["seconds"]), flush=True)
                    if interrupted or not browser.is_connected():
                        break
            finally:
                try:
                    browser.close()
                except Exception:
                    pass   # 中断后驱动连接可能已断：尽力关闭，结果以场景记录与摘要为准
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        startup_error = "%s: %s" % (type(exc).__name__, exc)
        traceback.print_exc()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)

    for sid in request.plan:   # 提前停止时，没跑到的场景记为未运行，不冒充通过
        if sid not in ran:
            records.append({"id": sid, "status": "not_run",
                            "checks": {"total": 0, "passed": 0, "failed": 0},
                            "started_at": None, "ended_at": None, "seconds": None})

    failed = sum(not good for good, _ in RESULTS)
    print("场景执行清单（%s）：" % ("全部注册场景" if request.mode == "full" else "部分场景"))
    for record in records:
        print("  %-22s %-4s 检查 %d/%d" % (record["id"],
                                           SCENARIO_STATUS_LABELS[record["status"]],
                                           record["checks"]["passed"], record["checks"]["total"]))
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    print("AE-SCENARIOS: " + json.dumps({
        "mode": request.mode, "source": request.source, "requested": list(request.requested),
        "plan": list(request.plan), "scenarios": records}, ensure_ascii=False), flush=True)
    if interrupted:
        return 130
    bad = any(record["status"] != "passed" for record in records)
    return 1 if (failed or bad or startup_error) else 0


if __name__ == "__main__":
    sys.exit(main())

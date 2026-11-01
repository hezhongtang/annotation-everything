"""浏览器等待的可观察条件层（issue #54，无浏览器依赖）。

只依赖标准库、可被单元发现导入；playwright 仍只在 tests/e2e_browser.py 导入。
这里的等待函数是「有界 + 条件 + 诊断」的纯逻辑：探测函数、页面调用与时钟由
调用方注入（真实场景传 playwright 页面对象，单元测试传合成 stub），所以等待
函数本身可以在单元层验证；真实浏览器行为只在 e2e_browser.py 端到端层证明。

契约（issue #54 验收标准）：
- 超时仍有上限：seconds 必须是正数，超时后返回 False，不再轮询；
- 超时输出等待的条件、最后观察到的状态和当前场景 ID（print 进场景日志）；
- 错误不被自动重试掩盖：probe / 页面 API 抛出的非超时异常原样传播；
  超时通过 register_timeout_errors 注册的异常类型识别（playwright 的
  TimeoutError 由 e2e_browser.py 在导入时注册，stub 测试注册自己的类型）。
"""
from __future__ import annotations

import time

_SCENARIO = {"id": None}


def set_scenario(scenario_id) -> None:
    """登记当前场景 ID；e2e_browser.py 在每个场景开始时设置，结束时清空。"""
    _SCENARIO["id"] = scenario_id


def current_scenario():
    return _SCENARIO["id"]


TIMEOUT_ERRORS: tuple = ()


def register_timeout_errors(*classes) -> None:
    """登记「等待超时」异常类型；不传参数即清空（单元测试用）。"""
    global TIMEOUT_ERRORS
    TIMEOUT_ERRORS = classes


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, tuple(TIMEOUT_ERRORS)) if TIMEOUT_ERRORS else False


def _report_timeout(condition, seconds, last_state) -> None:
    scenario = current_scenario() or "（未登记场景）"
    print("[等待超时] 场景 %s：%s（上限 %g 秒）；最后状态：%s" % (
        scenario, condition, seconds, last_state if last_state is not None else "（未提供）"),
        flush=True)


def _safe_describe(describe):
    if describe is None:
        return None
    try:
        return describe()
    except Exception as exc:   # 页面已关闭等：快照失败不能掩盖等待超时本身
        return "（状态快照失败：%s: %s）" % (type(exc).__name__, exc)


def bounded_poll(probe, seconds, *, step=0.05, sleeper=time.sleep, clock=time.monotonic,
                 condition=None, describe=None):
    """有界轮询 Python 侧条件（磁盘文件、组合判断等）。

    probe() 为真时立即返回它的真值（可以是列表 / 字典等，便于调用方直接取用，
    与改造前 wait_until 返回 cond() 值的契约一致）；超时返回 False 并输出诊断
    （条件、最后状态、场景 ID）。probe 抛出的异常原样传播，不被轮询掩盖。
    clock / sleeper 可注入，单元测试用假时钟验证边界，不需要真的等待。
    """
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError("等待必须有正的上限（秒），收到 %r" % (seconds,))
    deadline = clock() + seconds
    while True:
        value = probe()
        if value:
            return value
        if clock() >= deadline:
            break
        sleeper(step)
    _report_timeout(condition or "Python 侧等待条件", seconds, _safe_describe(describe))
    return False


def wait_dom(page, expression: str, seconds, *, arg=None, condition=None, describe=None) -> bool:
    """等浏览器里的可观察状态：page.wait_for_function（浏览器侧自动重试）。

    超时返回 False 并输出诊断；表达式抛出的非超时异常原样传播。expression 默认
    就是诊断里的等待条件，调用方可用 condition 给出更可读的说法。
    """
    try:
        page.wait_for_function(expression, arg=arg, timeout=int(seconds * 1000))
        return True
    except Exception as exc:
        if not _is_timeout(exc):
            raise
        _report_timeout(condition or expression, seconds, _safe_describe(describe))
        return False


def wait_response(page, predicate, seconds, *, condition=None, describe=None,
                  action=None) -> bool:
    """等下一个匹配的网络事件完成：page.expect_response（事件驱动，不靠固定间隔）。

    用于把等待绑定到「本次动作产生的请求 / 应答」或「一次轮询往返确实发生」。
    action 在监听注册之后、等待之前执行（Playwright 推荐模式）：先注册再触发动作，
    毫秒级完成的请求不会落在注册前的漏事件窗口里。超时返回 False 并输出诊断；
    非超时异常原样传播。
    """
    try:
        with page.expect_response(predicate, timeout=int(seconds * 1000)):
            if action is not None:
                action()
        return True
    except Exception as exc:
        if not _is_timeout(exc):
            raise
        _report_timeout(condition or "匹配的网络事件", seconds, _safe_describe(describe))
        return False


def is_note_poll(response) -> bool:
    """GET /api/notes 的完整往返（页面轮询的请求特征；不含子路径与其他方法）。"""
    request = getattr(response, "request", None)
    return bool(request) and request.method == "GET" and request.url.endswith("/api/notes")


def wait_poll(page, seconds, *, condition="下一次对 /api/notes 的轮询往返", describe=None) -> bool:
    """等一次页面轮询确实发生（GET /api/notes 请求与应答都到达）。"""
    return wait_response(page, is_note_poll, seconds, condition=condition, describe=describe)


def wait_rev(page, before, seconds, *, describe=None) -> bool:
    """等页面应用一次新的服务端修订：页面全局 rev 离开基线值。

    「新轮询不能破坏草稿 / 选择」一类不变量的前置证据：先证明服务端修订确实
    被一次轮询应用到页面，再检查不变量，避免检查在动作前就恒过。
    """
    return wait_dom(
        page,
        "b => typeof rev !== 'undefined' && rev !== null && rev !== b",
        seconds, arg=before,
        condition="页面应用新的服务端修订（rev 离开基线 %r）" % (before,),
        describe=describe)

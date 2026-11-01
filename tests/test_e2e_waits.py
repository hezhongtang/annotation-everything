"""浏览器等待层的单元测试（issue #54）。

等待逻辑本身不依赖 playwright：probe / 页面调用 / 时钟全部由调用方注入，
这里用合成 stub 验证等待函数的契约——有界超时、诊断输出（场景 ID、条件、
最后观察到的状态）、错误不被重试掩盖、事件驱动等待把超时上限传给页面 API。
真实浏览器里的等待行为只在 tests/e2e_browser.py 端到端层验证。
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import e2e_waits


class StubTimeout(Exception):
    """stub 页面的超时异常；按 playwright TimeoutError 的同一方式注册。"""


class FakeClock:
    """可注入的单调时钟：sleep 直接推进时钟，不真的等待。"""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class StubPage:
    """合成页面：记录 wait_for_function / expect_response 的调用与参数。"""

    def __init__(self, fail_after=None, error=None, timeout_error=StubTimeout):
        self.calls = []
        self.state_checks = 0
        self.fail_after = fail_after          # 前 N 次超时，之后成功；None 表示一直超时
        self.error = error                    # 非 None 时每次调用抛这个异常
        self.timeout_error = timeout_error

    def wait_for_function(self, expression, arg=None, timeout=None):
        self.calls.append(("wait_for_function", expression, arg, timeout))
        if self.error is not None:
            raise self.error
        self.state_checks += 1
        if self.fail_after is None or self.state_checks <= self.fail_after:
            raise self.timeout_error("timed out")
        return "ok"

    @contextlib.contextmanager
    def expect_response(self, predicate, timeout=None):
        self.calls.append(("expect_response", predicate, timeout))
        if self.error is not None:
            raise self.error
        self.state_checks += 1
        if self.fail_after is None or self.state_checks <= self.fail_after:
            raise self.timeout_error("timed out")
        yield "response"


class Response:
    def __init__(self, method="GET", url="http://127.0.0.1:8800/api/notes"):
        self.request = type("Req", (), {"method": method, "url": url})()


@contextlib.contextmanager
def capture_output():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class BoundedPollTests(unittest.TestCase):
    def setUp(self):
        e2e_waits.set_scenario("unit-scenario")
        self.addCleanup(e2e_waits.set_scenario, None)

    def test_returns_true_without_sleeping_when_probe_is_immediately_true(self):
        clock = FakeClock()
        probes = []

        def probe():
            probes.append(1)
            return True

        self.assertTrue(e2e_waits.bounded_poll(probe, 5, clock=clock.monotonic, sleeper=clock.sleep))
        self.assertEqual(len(probes), 1)
        self.assertEqual(clock.now, 100.0)   # 没有任何固定等待

    def test_returns_the_probe_truthy_value_not_just_true(self):
        clock = FakeClock()
        marker = [{"id": "n1"}]

        def probe():
            return marker or False

        self.assertIs(e2e_waits.bounded_poll(probe, 5, clock=clock.monotonic, sleeper=clock.sleep), marker)

    def test_polls_until_deadline_then_returns_false_with_bounded_probes(self):
        clock = FakeClock()
        probes = []

        def probe():
            probes.append(clock.now)
            return False

        with capture_output() as out:
            result = e2e_waits.bounded_poll(probe, 0.2, step=0.05,
                                            clock=clock.monotonic, sleeper=clock.sleep)
        self.assertFalse(result)
        self.assertGreaterEqual(clock.now - 100.0, 0.2, "没等满上限就放弃")
        self.assertLess(clock.now - 100.0, 0.2 + 0.05 * 2, "超出上限后还在轮询")
        self.assertLessEqual(len(probes), 6, "probe 次数没有上界")

    def test_timeout_diagnostic_includes_scenario_condition_and_last_state(self):
        clock = FakeClock()

        def probe():
            return False

        with capture_output() as out:
            e2e_waits.bounded_poll(probe, 0.1, clock=clock.monotonic, sleeper=clock.sleep,
                                   condition="合成状态变为绿色",
                                   describe=lambda: "mode=offline; nlist=空")
        text = out.getvalue()
        self.assertIn("unit-scenario", text, "诊断没有包含场景 ID")
        self.assertIn("合成状态变为绿色", text, "诊断没有包含等待条件")
        self.assertIn("mode=offline; nlist=空", text, "诊断没有包含最后观察到的状态")

    def test_describe_errors_do_not_mask_the_timeout_report(self):
        clock = FakeClock()

        def probe():
            return False

        with capture_output() as out:
            result = e2e_waits.bounded_poll(probe, 0.1, clock=clock.monotonic, sleeper=clock.sleep,
                                            condition="条件", describe=lambda: 1 / 0)
        self.assertFalse(result)
        self.assertIn("条件", out.getvalue())

    def test_probe_exceptions_are_not_swallowed_by_the_loop(self):
        clock = FakeClock()

        def probe():
            raise RuntimeError("页面已关闭")

        with self.assertRaises(RuntimeError):
            e2e_waits.bounded_poll(probe, 1, clock=clock.monotonic, sleeper=clock.sleep)

    def test_rejects_missing_or_negative_bounds(self):
        for seconds in (None, 0, -1):
            with self.assertRaises(ValueError):
                e2e_waits.bounded_poll(lambda: True, seconds)


class WaitDomTests(unittest.TestCase):
    def setUp(self):
        e2e_waits.register_timeout_errors(StubTimeout)
        e2e_waits.set_scenario("unit-scenario")
        self.addCleanup(e2e_waits.set_scenario, None)
        self.addCleanup(e2e_waits.register_timeout_errors)

    def test_passes_milliseconds_and_argument_to_the_page_api(self):
        page = StubPage(fail_after=0)
        self.assertTrue(e2e_waits.wait_dom(page, "() => flag", 3.5, arg="x"))
        kind, expression, arg, timeout = page.calls[0]
        self.assertEqual(expression, "() => flag")
        self.assertEqual(arg, "x")
        self.assertEqual(timeout, 3500)

    def test_returns_false_with_diagnostic_when_the_page_times_out(self):
        page = StubPage()
        with capture_output() as out:
            result = e2e_waits.wait_dom(page, "() => rev !== b", 2,
                                        condition="页面应用新的服务端修订",
                                        describe=lambda: "mode=server; rev=7")
        self.assertFalse(result)
        text = out.getvalue()
        self.assertIn("unit-scenario", text)
        self.assertIn("页面应用新的服务端修订", text)
        self.assertIn("mode=server; rev=7", text)

    def test_non_timeout_page_errors_propagate(self):
        page = StubPage(error=RuntimeError("expression is invalid"))
        with self.assertRaises(RuntimeError):
            e2e_waits.wait_dom(page, "() => oops", 1)

    def test_unregistered_timeout_errors_propagate_instead_of_passing(self):
        e2e_waits.register_timeout_errors()   # 清空注册：未注册的超时不能被当成成功
        page = StubPage()
        with self.assertRaises(StubTimeout):
            e2e_waits.wait_dom(page, "() => x", 1)


class WaitResponseTests(unittest.TestCase):
    def setUp(self):
        e2e_waits.register_timeout_errors(StubTimeout)
        e2e_waits.set_scenario("unit-scenario")
        self.addCleanup(e2e_waits.set_scenario, None)
        self.addCleanup(e2e_waits.register_timeout_errors)

    def test_wait_poll_matches_only_full_note_round_trips(self):
        page = StubPage(fail_after=0)
        self.assertTrue(e2e_waits.wait_poll(page, 6))
        kind, predicate, timeout = page.calls[0]
        self.assertEqual(timeout, 6000)
        self.assertTrue(predicate(Response(method="GET", url="http://127.0.0.1:8800/api/notes")))
        self.assertFalse(predicate(Response(method="POST", url="http://127.0.0.1:8800/api/notes")))
        self.assertFalse(predicate(Response(method="GET", url="http://127.0.0.1:8800/api/notes/1")))
        self.assertFalse(predicate(Response(method="GET", url="http://127.0.0.1:8800/api/health")))

    def test_wait_poll_returns_false_with_diagnostic_on_timeout(self):
        page = StubPage()
        with capture_output() as out:
            result = e2e_waits.wait_poll(page, 4, describe=lambda: "mode=server")
        self.assertFalse(result)
        text = out.getvalue()
        self.assertIn("unit-scenario", text)
        self.assertIn("mode=server", text)

    def test_non_timeout_errors_propagate(self):
        page = StubPage(error=RuntimeError("browser closed"))
        with self.assertRaises(RuntimeError):
            e2e_waits.wait_poll(page, 1)


class WaitRevTests(unittest.TestCase):
    def setUp(self):
        e2e_waits.register_timeout_errors(StubTimeout)
        e2e_waits.set_scenario("unit-scenario")
        self.addCleanup(e2e_waits.set_scenario, None)
        self.addCleanup(e2e_waits.register_timeout_errors)

    def test_waits_for_the_page_global_rev_to_leave_the_baseline(self):
        page = StubPage(fail_after=0)
        self.assertTrue(e2e_waits.wait_rev(page, 7, 4))
        kind, expression, arg, timeout = page.calls[0]
        self.assertIn("rev", expression)
        self.assertEqual(arg, 7)
        self.assertEqual(timeout, 4000)

    def test_timeout_reports_the_baseline(self):
        page = StubPage()
        with capture_output() as out:
            result = e2e_waits.wait_rev(page, 7, 1, describe=lambda: "rev=7")
        self.assertFalse(result)
        self.assertIn("7", out.getvalue())


if __name__ == "__main__":
    unittest.main()

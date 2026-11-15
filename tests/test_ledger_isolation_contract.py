"""台账隔离契约：进程内导入台账相关模块的测试文件必须在导入期设置 AE_HOME。

背景见 issue #165：漏设隔离的测试文件经 build_viewer.register_review 等路径，
把系统临时目录下的测试审阅登记进真实台账 ~/.annotation-everything/，
累计留下大量「失效」条目。与 test_core.py 共用同一隔离协议：测试不碰用户真实的审阅台账。

契约口径（按 AST 判定，字符串字面量里的 import 与 subprocess 方式不计入）：
模块顶层真实 `import build_viewer / serve / ledger / review_overview / review_stop`
任一台账模块的 test_*.py，必须在首个此类 import 之前有模块级
`os.environ["AE_HOME"] = ...` 赋值。notes_store 只写审阅目录内的批注文件，
不碰台账，不在契约内；用 subprocess 跑脚本并在 env 里传 AE_HOME 的文件（如
test_skill_updates.py）不受本契约约束。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# 这些模块在导入或调用时会写审阅台账（build_viewer.register_review、ledger.register 等）
LEDGER_MODULES = {"build_viewer", "serve", "ledger", "review_overview", "review_stop"}


def _ledger_import_line(node: ast.stmt) -> int | None:
    """模块顶层语句是台账模块 import 时返回行号，否则 None。"""
    if isinstance(node, ast.Import) and any(a.name.split(".")[0] in LEDGER_MODULES for a in node.names):
        return node.lineno
    return None


def _ae_home_assign_line(node: ast.stmt) -> int | None:
    """模块顶层语句是 os.environ["AE_HOME"] = ... 赋值时返回行号，否则 None。"""
    if not isinstance(node, ast.Assign):
        return None
    for target in node.targets:
        if (isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "os"
                and target.value.attr == "environ"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "AE_HOME"):
            return node.lineno
    return None


class LedgerIsolationContractTests(unittest.TestCase):
    def test_modules_importing_ledger_scripts_set_ae_home_first(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            first = None
            assign_line = None
            for stmt in tree.body:
                if (line := _ledger_import_line(stmt)) is not None and first is None:
                    first = line
                if (line := _ae_home_assign_line(stmt)) is not None and assign_line is None:
                    assign_line = line
            if first is not None and (assign_line is None or assign_line > first):
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            "以下测试文件进程内导入台账相关模块，但没有在首个此类 import 之前设置"
            " os.environ[\"AE_HOME\"]，会把测试审阅登记进用户真实的审阅台账（issue #165）",
        )


if __name__ == "__main__":
    unittest.main()

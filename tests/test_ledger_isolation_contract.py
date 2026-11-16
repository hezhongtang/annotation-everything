"""台账隔离契约：进程内导入台账相关模块的测试文件必须在导入期设置 AE_HOME。

背景见 issue #165：漏设隔离的测试文件经 build_viewer.register_review 等路径，
把系统临时目录下的测试审阅登记进真实台账 ~/.annotation-everything/，
累计留下大量「失效」条目。与 test_core.py 共用同一隔离协议：测试不碰用户真实的审阅台账。

契约按 AST 中的源码顺序判定。test_*.py 中出现台账脚本的 import 或 from import
时（含函数内导入），首个此类导入所在的模块级语句之前必须有
`os.environ["AE_HOME"] = ...` 赋值。通过 subprocess 运行的脚本使用
子进程的 AE_HOME。notes_store 只写审阅目录内的批注文件。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# 这些模块在导入或调用时会写审阅台账（build_viewer.register_review、ledger.register 等）
LEDGER_MODULES = {"build_viewer", "serve", "ledger", "review_overview", "review_stop"}


def _imports_ledger(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] in LEDGER_MODULES for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        return node.module.split(".")[0] in LEDGER_MODULES
    return False


def _sets_ae_home(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    for target in node.targets:
        if (isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "os"
                and target.value.attr == "environ"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "AE_HOME"):
            return True
    return False


def _missing_ae_home(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if _sets_ae_home(stmt):
            return False
        if any(_imports_ledger(node) for node in ast.walk(stmt)):
            return True
    return False


class LedgerIsolationContractTests(unittest.TestCase):
    def test_function_local_import_requires_unconditional_module_assignment(self):
        for statement in ("import build_viewer", "from build_viewer import main"):
            with self.subTest(statement=statement):
                source = f"def build_page():\n    {statement}\n"
                setdefault = 'os.environ.setdefault("AE_HOME", "temporary")\n'
                assign = 'os.environ["AE_HOME"] = "temporary"\n'
                self.assertTrue(_missing_ae_home(ast.parse(setdefault + source)))
                self.assertTrue(_missing_ae_home(ast.parse(source + assign)))
                self.assertFalse(_missing_ae_home(ast.parse(assign + source)))

    def test_modules_importing_ledger_scripts_set_ae_home_first(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _missing_ae_home(tree):
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            "以下测试文件未在首个台账导入所在的模块级语句前赋值"
            " os.environ[\"AE_HOME\"]，可能写入真实的审阅台账（issue #165）",
        )


if __name__ == "__main__":
    unittest.main()

"""文档允许列表的依赖契约：新增程序引用时要求重新核实选测边界。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import verify_selection

FIXTURE = json.loads((ROOT / "fixtures" / "verify-doc-allowlist.json").read_text(encoding="utf-8"))
SOURCE_DIRS = ("tests", "tools", "skill/annotation-everything/scripts", ".github")
SOURCE_SUFFIXES = {".py", ".sh", ".js", ".html", ".yml", ".yaml"}


def executable_strings(node, allow_declaration=False):
    # 注释和说明不是程序依赖；唯一豁免的代码数据是内置允许列表声明自身。
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return
    if (allow_declaration and isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DOC_ALLOWLIST" for target in node.targets)):
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("glob", "rglob"):
        values = [part.value for part in ast.walk(node) if isinstance(part, ast.Constant) and isinstance(part.value, str)]
        if "docs" in values:
            yield "/".join(("docs", "**"))  # 分段写出的 docs 通配遍历同样需要复核。
    for child in ast.iter_child_nodes(node):
        yield from executable_strings(child, allow_declaration)


def document_references(root: Path, prefixes: list) -> list:
    patterns = [re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(Path(prefix.rstrip("/")).stem), re.IGNORECASE)
                for prefix in prefixes]
    files = {path for path in root.iterdir() if path.is_file() and path.suffix in SOURCE_SUFFIXES}
    for directory in SOURCE_DIRS:
        files.update(path for path in (root / directory).rglob("*")
                     if path.is_file() and (path.suffix in SOURCE_SUFFIXES or not path.suffix))
    references = []
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        strings = (list(executable_strings(ast.parse(text), rel == "verify_selection.py"))
                   if path.suffix == ".py" else
                   [line for line in text.splitlines() if not line.lstrip().startswith(("#", "//"))])
        if any(pattern.search(value) or "docs/" in value.lower() and "*" in value
               for value in strings for pattern in patterns):
            references.append(rel)
    return references


class DocumentationAllowlistTests(unittest.TestCase):
    def test_new_allowlist_entries_have_no_executable_consumers(self):
        additions = {entry.prefix for entry in verify_selection.DOC_ALLOWLIST} - set(FIXTURE["legacy_allowlist"])
        self.assertEqual(additions, set(FIXTURE["audited_paths"]), "新增允许项须补齐核实依据与依赖契约")
        self.assertEqual(document_references(ROOT, sorted(additions)), [],
                         "程序开始引用这些文档：先撤销豁免或重新核实 docs/verify/selection-strategy.md")

    def test_contract_detects_readers_in_tests_verification_tools_and_runtime(self):
        with tempfile.TemporaryDirectory(prefix="verify-document-consumers-") as temp:
            root = Path(temp)
            for reader in FIXTURE["readers"]:
                path = root / reader["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(reader["source"], encoding="utf-8")
            self.assertEqual(document_references(root, FIXTURE["audited_paths"]),
                             sorted(reader["path"] for reader in FIXTURE["readers"]))


if __name__ == "__main__":
    unittest.main()

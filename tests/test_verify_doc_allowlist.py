"""文档允许列表的依赖契约：新增程序引用时要求重新核实选测边界。"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
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


def function_digest(node):
    def normalize(value):
        if isinstance(value, ast.AST):
            # 新版 Python 增加的空字段不改变函数语义，也不应改变核实摘要。
            return {"node": type(value).__name__, "fields": {
                key: normalize(item) for key, item in ast.iter_fields(value)
                if item is not None and not (isinstance(item, list) and not item)}}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    canonical = json.dumps(normalize(node), sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"), default=repr)
    return hashlib.sha256(canonical.encode()).hexdigest()


def unreviewed_traversals(tree, rel: str, prefixes: list) -> bool:
    """宽范围扫描缺省需复核；已核实的受限扫描绑定到整个函数的语义摘要。"""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    probes = [prefix + "synthetic.md" if prefix.endswith("/") else prefix for prefix in prefixes]
    imported = {}
    for statement in ast.walk(tree):
        if isinstance(statement, ast.ImportFrom) and statement.module in ("glob", "os"):
            operations = ("glob", "iglob") if statement.module == "glob" else ("walk", "fwalk")
            for alias in statement.names:
                if alias.name == "*":
                    imported.update((name, name) for name in operations)
                elif alias.name in operations:
                    imported[alias.asname or alias.name] = alias.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            operation = imported.get(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "ast":
                continue  # ast.walk 遍历语法树，不遍历文件。
            operation = node.func.attr
        else:
            operation = None
        if operation not in ("glob", "rglob", "iglob", "walk", "fwalk"):
            continue
        try:
            pattern = ast.literal_eval(node.args[0])
        except (ValueError, IndexError):
            pattern = None
        if (operation not in ("walk", "fwalk") and isinstance(pattern, str)
                and not pattern.lower().endswith(".md")
                and not any(fnmatch.fnmatchcase(probe, pattern) or fnmatch.fnmatchcase(Path(probe).name, pattern)
                            for probe in probes)):
            continue
        scope = parents.get(node)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents.get(scope)
        if scope is not None and any(
                entry["path"] == rel and entry["function"] == scope.name
                and entry["source_sha256"] == function_digest(scope) and entry["basis"]
                for entry in FIXTURE.get("bounded_traversals", [])):
            continue
        return True
    return False


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
        traversal = False
        if path.suffix == ".py":
            tree = ast.parse(text)
            strings = list(executable_strings(tree, rel == "verify_selection.py"))
            traversal = unreviewed_traversals(tree, rel, prefixes)
        else:
            strings = [line for line in text.splitlines() if not line.lstrip().startswith(("#", "//"))]
        if traversal or any(pattern.search(value) or "docs/" in value.lower() and "*" in value
                            for value in strings for pattern in patterns):
            references.append(rel)
    return references


class DocumentationAllowlistTests(unittest.TestCase):
    def test_optional_empty_ast_fields_do_not_invalidate_an_audited_scope(self):
        scope = ast.parse("def synthetic(): pass").body[0]
        original = function_digest(scope)
        scope._fields = (*scope._fields, "future_optional_field")
        scope.future_optional_field = []
        self.assertEqual(function_digest(scope), original)

    def test_new_allowlist_entries_have_no_executable_consumers(self):
        additions = {entry.prefix for entry in verify_selection.DOC_ALLOWLIST} - set(FIXTURE["legacy_allowlist"])
        self.assertEqual(additions, set(FIXTURE["audited_paths"]), "新增允许项须补齐核实依据与依赖契约")
        # 无新增豁免项时没有需要保护的路径：docs 通配遍历的保守启发式不构成消费判定。
        self.assertEqual(document_references(ROOT, sorted(additions)) if additions else [], [],
                         "程序开始引用这些文档：先撤销豁免或重新核实 docs/verify/selection-strategy.md")

    def test_contract_detects_readers_in_tests_verification_tools_and_runtime(self):
        with tempfile.TemporaryDirectory(prefix="verify-document-consumers-") as temp:
            root = Path(temp)
            for reader in FIXTURE["readers"]:
                path = root / reader["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(reader["source"], encoding="utf-8")
            self.assertEqual(document_references(root, FIXTURE["probe_prefixes"]),
                             sorted(reader["path"] for reader in FIXTURE["readers"]))

    def test_bounded_traversal_requires_the_audited_function_to_stay_unchanged(self):
        for entry in FIXTURE["bounded_traversals"]:
            with self.subTest(path=entry["path"], function=entry["function"]):
                tree = ast.parse((ROOT / entry["path"]).read_text())
                scope = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                             and node.name == entry["function"])
                with tempfile.TemporaryDirectory(prefix="verify-bounded-consumer-") as temp:
                    root = Path(temp)
                    path = root / entry["path"]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(ast.unparse(scope), encoding="utf-8")
                    self.assertEqual(document_references(root, FIXTURE["probe_prefixes"]), [])
                    scope.body.append(ast.Pass())
                    path.write_text(ast.unparse(ast.fix_missing_locations(scope)), encoding="utf-8")
                    self.assertEqual(document_references(root, FIXTURE["probe_prefixes"]), [entry["path"]])


if __name__ == "__main__":
    unittest.main()

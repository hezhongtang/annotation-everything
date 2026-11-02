#!/usr/bin/env bash
set -eu
cp -R "$(dirname "$0")/files/." .
# 初态记录声明与当前产物对应（可交付初态）：把真实内容哈希注入记录的占位符，
# 让 Agent 从「记录对应当前产物」的状态出发进入改稿。
python3 - <<'PY'
import hashlib
import json
from pathlib import Path

d = Path("materials")


def sha(name: str) -> str:
    return hashlib.sha256((d / name).read_bytes()).hexdigest()


path = d / "report-record.json"
record = json.loads(path.read_text(encoding="utf-8"))
record["links"]["sha256"]["report"] = sha("report.html")
record["links"]["sha256"]["script"] = sha("script.md")
record["sources"][0]["sha256"] = sha("q3-notes.md")
path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

#!/usr/bin/env bash
set -euo pipefail
: "${AE_SKILL_DIR:?Set AE_SKILL_DIR to the temporary installed annotation-everything copy}"
: "${AE_HOME:?Set AE_HOME to a temporary evaluation ledger}"
case_dir="$(cd "$(dirname "$0")" && pwd)"
cp -R "$case_dir/files/." .
python3 "$AE_SKILL_DIR/scripts/build_viewer.py" --report report.html --script script.md --out coffee-review --locale en --no-scan-skills
python3 "$AE_SKILL_DIR/scripts/notes.py" import coffee-review annotations.md --locale en
python3 - <<'PY'
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(os.environ['AE_SKILL_DIR']) / 'scripts'))
import notes_store

notes_store.handoff_submit(Path('coffee-review').resolve(),
                          'Apply my annotations. Stop after the initial edit. Leave the supplier decision to me.',
                          target='review', allow_subagents=False, full_check=True)
PY

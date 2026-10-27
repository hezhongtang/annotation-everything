#!/usr/bin/env bash
# 把 skill 安装（覆盖）到个人级 skills 目录：~/.claude/skills/annotation-everything
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
src="$here/skill/annotation-everything"
dst="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/annotation-everything"
mkdir -p "$dst"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$src/" "$dst/"
echo "已安装到 $dst"

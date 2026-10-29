#!/usr/bin/env bash
# 启用仓库自带的 git 钩子（拒绝在 main 上提交、推送到 main、删除 main）。克隆后运行一次即可，可重复运行。
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$here/tools/git-hooks/"*
git -C "$here" config core.hooksPath tools/git-hooks
echo "已启用 git 钩子：core.hooksPath = $(git -C "$here" config core.hooksPath)"

#!/usr/bin/env bash
# 启用仓库自带的 git 钩子（拒绝在 main 上提交、推送到 main、删除 main）与 git blame 忽略配置。克隆后运行一次即可，可重复运行。
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$here/tools/git-hooks/"*
git -C "$here" config core.hooksPath tools/git-hooks
echo "已启用 git 钩子：core.hooksPath = $(git -C "$here" config core.hooksPath)"
git -C "$here" config --local blame.ignoreRevsFile .git-blame-ignore-revs
echo "已配置 git blame 忽略列表：blame.ignoreRevsFile = $(git -C "$here" config --local --get blame.ignoreRevsFile)"

#!/usr/bin/env bash
# 启用仓库自带的 git 钩子（拒绝在 main 上提交、推送到 main、删除 main）与 git blame 忽略配置。克隆后运行一次即可，可重复运行。
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$here/tools/git-hooks/"*
git -C "$here" config core.hooksPath tools/git-hooks
echo "已启用 git 钩子：core.hooksPath = $(git -C "$here" config core.hooksPath)"
if [[ -f "$here/.git-blame-ignore-revs" ]]; then
  # 只匹配项目清单。Git 在没有匹配值时追加，已有匹配值时合并为一项。
  git -C "$here" config --local --replace-all blame.ignoreRevsFile .git-blame-ignore-revs '^[.]git-blame-ignore-revs$'
  echo "已配置 git blame 忽略列表 .git-blame-ignore-revs"
fi

## 关联 issue

Closes #

<!-- 一个 PR 只关闭一个 issue。例外：父 issue 的最后一个子 issue，在这里同时写 Closes #父编号。没有 issue 的小改动删除上一行。 -->

## 改动

- 

## 验收标准核对

<!-- 逐条对照 issue 的验收标准；已满足的在 issue 里勾选，未满足的列在这里并说明原因。 -->

## 验证

- [ ] `python3 -m unittest discover tests`
- [ ] 改了页面模板或服务：`uv run --no-project --with playwright python tests/e2e_browser.py`
- [ ] 改了服务：`python3 skill/annotation-everything/scripts/serve.py check`
- [ ] 改了 skill：已运行 `python3 ae.py install`
- [ ] 按 AGENTS.md「README 维护」检查过中英两份 README

## 兼容性

<!-- 对已有审阅目录、旧版服务和旧数据格式的影响；没有影响也写明。 -->

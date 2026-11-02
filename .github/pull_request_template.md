## 关联 issue

Closes #

<!-- 一个 PR 只关闭一个 issue。例外：父 issue 的最后一个子 issue，在这里同时写 Closes #父编号。没有 issue 的小改动删除上一行。 -->

## 改动

- 

## 验收标准核对

<!-- 逐条对照 issue 的验收标准；已满足的在 issue 里勾选，未满足的列在这里并说明原因。 -->

## 验证

<!-- 统一入口是命令与范围的唯一来源；直接命令（unittest / e2e_browser / serve.py check）只作诊断，其输出不是可复用的验证记录。 -->

- [ ] 开发 / 审查：`python3 verify.py run unit-related <路径>` 完成本阶段验证并留下记录；引用已有验证时附 `python3 verify.py report --latest` 的证据链，按疑点补测点名到类 / 用例，独立复测加 `--no-reuse`
- [ ] PR 收敛门禁：`python3 verify.py run --group all --phase pr`（完整单元 + 服务安全自检 + 完整浏览器；`pr` 阶段不引用旧证据，必须实际执行）
- [ ] 改了 skill：已运行 `python3 ae.py install`
- [ ] 按 AGENTS.md「README 维护」检查过中英两份 README

- [ ] 附 run ID、输入指纹 / 提交、环境、实际范围与 `report --run <ID>` 证据链；记录 `report --efficiency` 的可比性、缺口和未达标项。未完成的总验收保持 issue 开放，只有全部满足才写关闭引用。

## 兼容性

<!-- 对已有审阅目录、旧版服务和旧数据格式的影响；没有影响也写明。 -->

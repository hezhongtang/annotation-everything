# 同机资源隔离协议

同机多个工作区（worktree、独立 checkout）同时跑验证时，资源敏感的组会互相干扰：
安装维护检查用真实 `ps` 枚举本机全部进程并探测端口，浏览器测试又启动真实的
serve.py——并行时盘点结果里混进对方的服务，被误判成残留服务，触发假失败与
串行重测（onboarding 记录过的实测干扰）。目录隔离挡不住这种干扰，因为进程
视图和端口池属于机器与用户，不属于任何一个检出。

本协议（issue #52）让所有验证入口按**共享资源**互斥，而不是按目录。本文说明
协议的参与方、资源声明、锁机制、超时语义与安全边界。代码在仓库根的
`host_isolation.py`；机器可读的声明用 `python3 verify.py plan --json` 的
`isolation` 字段读取。

## 谁参与协议

| 入口 | 接入方式 | 持锁范围 |
| --- | --- | --- |
| `verify.py run` 的 `unit` 组 | 组级取锁（`execute_group`） | 组执行期间 |
| `verify.py run` 的 `browser` 组 | 组级取锁 | 组执行期间 |
| `python3 -m unittest discover tests` | 敏感测试模块导入时取锁（`require_for_module`） | 进程生命周期 |
| `python3 -m unittest tests.test_ae_cli`（等敏感模块） | 同上 | 进程生命周期 |
| `uv run … python tests/e2e_browser.py` | 文件头取锁（启动服务之前） | 进程生命周期 |
| `verify.py run unit-related <选择器>` | 选择器命中敏感模块时组级取锁 | 组执行期间 |

纯逻辑组（`serve-check`、未命中敏感模块的 `unit-related`）不取锁，不会被无关
资源阻塞。旧入口没有绕过路径：所有会触及宿主机状态的入口都在同一张锁上排队。

## 资源组声明

| 资源 | 互斥 | 归属 | 原因 |
| --- | --- | --- | --- |
| `host-state` | 是 | 同机同用户的全部检出 | doctor / uninstall 的真实全机进程枚举与端口探测；任何并行启动的真实 serve.py 都会进入盘点结果。端口分配共享同一内核端口池。按检出加锁挡不住别的 worktree / checkout 里的参与者 |
| `verification-reports` | 是 | 同机验证聚合参与方 | 仅更新效率投影、周期与去重状态时持有；产品测试执行不持有，沿用本协议的共享锁 |
| `ledger-and-skill-copy` | 否 | 同机同用户 | 测试与 eval 已全部通过 `AE_HOME`、`CLAUDE_SKILLS_DIR`、`HOME` 注入合成位置，真实台账与技能副本从不被读写。登记「独立」是为了写明理由：一旦测试开始共享这些位置，必须改成互斥资源 |

多资源按声明顺序（名称排序）获取，所有参与方一致，交叉请求不会死锁。
`synthetic-a` / `synthetic-b` 是协议自测用的合成资源名，不对应真实状态。

## 锁机制

- **flock 排他锁**：`fcntl.flock`，macOS 与 Linux 均支持。入口将实际持有的锁
  描述符传给组子进程；正常结束或可处理的中断先回收组，再释放锁。入口单独被
  `SIGKILL` 时，仍运行的组继续持有该描述符；最后一个持有者退出后内核释放锁，
  后继运行无需删除锁文件，也不会在前一组仍使用资源时提前进入。
- **锁文件位置**：同一用户共享、不依赖个人绝对路径。优先级：
  `AE_LOCK_DIR`（测试与特殊环境覆盖）→ `$XDG_RUNTIME_DIR/annotation-everything-locks-<UID>`
  → 系统临时目录下同名目录。目录按用户 UID 区分并以私有权限创建，Linux 回退到
  共享 `/tmp` 时不同用户也不会争用写权限。同一用户的任何检出解析到相同位置，
  因此跨 worktree、跨独立 checkout 生效。
- **锁文件内容**：持有者诊断信息（pid、资源、时间），只用于报告「谁在持有」；
  所有权判断以 flock 为准，锁文件残留不构成占用。协议不删除锁文件（删除有
  竞态），它们只有几十字节且可重复使用。
- **平台差异**：依赖 POSIX `flock`；Windows 不在支持范围（CI 是 Linux、开发机
  是 macOS）。只用 Python 标准库，语法兼容 Python 3.9。

## 等待与超时

- 等待上限缺省 600 秒，`AE_LOCK_WAIT_SECONDS` 可调。
- **超时不是通过**：verify.py 的组按 `timeout` 结论、退出码 124 记录，note 写明
  等待的资源与当前持有者 pid，该组没有启动子进程；直接跑 unittest 时模块导入
  报错，明确记为失败，绝不悄悄当通过。
- 锁等待单独计时：记录的 `timing.lock_wait_seconds` 与各组的
  `groups[].lock.wait_seconds`。锁等待不计入用例执行耗时，也不计入入口自身
  开销（`wrapper_seconds`）。等待期间取消也保留已等待的时间、资源与非成功状态。

## 嵌套与重入

入口持锁后拉起的子流程（`_unit-runner`、e2e_browser 被 verify.py 拉起）对同一
资源按**借入**处理：入口取锁时把持有集合写进环境变量 `AE_HELD_LOCKS`，子进程
看到即豁免，不再对同一锁文件重复 flock（不同进程重复 flock 会互相等待，不豁免
就是自锁）。豁免只沿父子链传递：没有继承该变量的进程照常排队。

## 安全边界

- **锁只协调参与者**。不参与协议的进程（用户真实服务、别的程序）不受锁影响，
  也绝不会因锁被停止：等不到就等，等到上限就失败。
- 等待超时的报告区分「真实占用」（持有者 pid 与资源）与自身故障；清理仍然只
  回收本次入口登记的子进程（`children.json` 登记簿语义不变）。
- 不停止进程、不读写真实台账与技能副本、不修改已运行服务的约束全部保留。

## 怎么观察与验证

- `python3 verify.py plan --json` 的 `isolation` 字段是机器可读的资源声明；
  文本 `plan` 附带人读的互斥 / 独立说明。
- 报告的耗时行有「锁等待」一列；组行有「锁：<资源>，等待 N 秒」。
- 协议的回归测试：`tests/test_host_isolation.py`（跨进程互斥、嵌套豁免、
  SIGKILL 释放、超时语义、多资源顺序、外部占用者不被停止、新旧入口端到端）；
  `tests/test_host_interference.py` 复现并修复「浏览器组 × 安装维护组」的真实
  干扰场景；`tests/test_verify_entry.py` 的 `IsolationLockTests` 覆盖入口组级
  行为。这些测试都使用合成锁目录与登记过的合成进程，不影响并行会话。

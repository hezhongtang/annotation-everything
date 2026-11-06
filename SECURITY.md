# Security policy / 安全策略

[中文](#中文) · [English](#english)

## 中文

### 报告漏洞

不要在公开的 issue、PR 或讨论里披露漏洞。

1. 打开仓库的 **Security** 页面。
2. 选择 **Report a vulnerability**，提交私密报告。
3. 写明受影响的版本、复现步骤和影响范围。

报告里只放合成数据。不要附上真实报告、批注、密钥或本机绝对路径。

### 支持的版本

只修复最新的正式版本。

### 范围

以下问题属于本项目的安全范围：

- 绕过本机服务的边界：只监听 `127.0.0.1`、Host 校验、写操作的 Origin 同源与 JSON Content-Type 校验、只开放 `index.html` 和 `/api/*`。
- 通过审阅内容或审阅目录读写边界之外的文件，或在对照页执行脚本。
- 通过安装或自动更新流程运行未发布的代码。

Agent 宿主、模型或第三方技能本身的缺陷，请报告给对应的维护方。

## English

### Report a vulnerability

Do not disclose vulnerabilities in public issues, pull requests or discussions.

1. Open the repository's **Security** page.
2. Choose **Report a vulnerability** and submit a private report.
3. Include the affected version, reproduction steps and impact.

Use synthetic data only. Do not attach real reports, annotations, secrets or absolute local paths.

### Supported versions

Only the latest release receives fixes.

### Scope

These issues are in scope:

- Bypassing the local service boundary: listening on `127.0.0.1` only, Host validation, same-origin and JSON Content-Type checks on writes, and serving only `index.html` and `/api/*`.
- Reading or writing outside the intended boundary through review content or a review directory, or running script in the review page.
- Running unreleased code through installation or automatic updates.

Report defects in an agent host, a model or a third-party skill to its maintainers.

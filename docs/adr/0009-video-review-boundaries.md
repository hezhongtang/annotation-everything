---
status: accepted
---

# 视频审阅的本机边界：渲染来源凭据与源工程确认接口

视频审阅（issue #191）复用 ADR 0001 的审阅目录隔离、审阅台账与专属端口，以及 ADR 0004 的接收方绑定。实现过程中有两个本机边界需要相对 ADR 0001 显式说明并加约束：

1. **引擎渲染来源凭据放在台账目录的 `video-renders/`**。同一源工程在不同审阅之间复用已渲染成片时，不能只看文件名或宽高帧率（外来同规格成片不能冒充当前工程产物），也不能向用户的源工程写入缓存文件。因此 `video_engines.py` 将 AE 自己完成的渲染凭据写入 `AE_HOME/video-renders/<sha256>.json`（由 `ledger.VIDEO_RENDERS_DIR` 统一命名）。每条凭据只记录 `{root, engine, entry, inputs, sha256}`，不算审阅业务数据。目录保持 `0700`、文件保持 `0600` 并经 `fsutil.read_private_text` 校验；`ae.py uninstall --purge` 在 `purge_guard` 与 `purge_ledger` 中显式识别并清理合法凭据文件，目录内出现未知文件或符号链接时仍拒绝清理。
2. **源工程路径只通过同源实时接口 `/api/video/source-project` 提供给确认弹窗**。审阅人在对照页的「源工程」弹窗中输入或核对本机 Remotion / HyperFrames 工程根目录，并在多候选歧义时确认所选工程；因此 `GET/POST /api/video/source-project` 返回 `sourceProject.root` 与 `candidates[*].root` 供当场核对。这是对 ADR 0001「对照页里不写本机路径」的定向放宽，仅限该实时接口：静态生成的 `index.html` 与 `video_review.public_manifest()` 仍不写入任何本机路径，视频媒体读取严格限定在 `/api/video/<reviewId>/versions/<versionId>/media` 并核对清单副本。

3. **渲染、媒体探测与解码失败的自由文本留在本机诊断中**。接收会话可以向 `notes.py video-receipt --render-error` 传入引擎报错，其中可能含源文件的绝对路径。私有 `review.json` 的 `video.failures[]` 和本机 `notes.py show` 保留原文。对照页、公开版本清单和单版接口按失败阶段生成固定摘要。这个投影同时适用于 `lastFailure` 和各版本的 `playbackFailure`。初次生成、普通重建和锁内采用并发更新后重新生成的页面共用同一投影。已有失败历史在读取时应用这个约定，无须迁移。

## Considered Options

- **把渲染凭据写进用户的源工程目录（如 `.ae-render.json`）**：污染用户仓库工作树，容易被误提交或被工程构建清理。
- **把渲染凭据只留在单份审阅目录**：新建第二份审阅打开同一源工程时无法证明已有成片由当前源码产出，只能每次强制重渲染。
- **对照页完全隐藏源工程根路径**：审阅人在多候选或先批 MP4 后关联工程时无法确认自己绑定的究竟是哪个本机工程。

## Consequences

- `ledger.py` 与 `ae.py` 同步维护 `VIDEO_RENDERS_DIR` 常量及凭据文件命名约束（64 位小写十六进制 `.json` 与 `.render-*.tmp`），避免渲染后阻塞 `ae.py uninstall --purge`。
- 静态 `index.html`、`GET /api/video/versions` 与 `GET /api/video/versions/<id>` 不暴露本机文件路径；工程探测与指纹计算拒绝读取指向工程外的符号链接。
- `serve.py check` 覆盖新增的视频只读、Range 媒体与写接口，验证 Host、同源 Origin、JSON Content-Type、`Sec-Fetch-Site`、`X-AE-Review` 与无 CORS 头边界。

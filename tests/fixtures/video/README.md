# 视频引擎合成 fixture

Remotion 与 HyperFrames 的最小合成工程骨架，供 `tests/test_video_engines.py`
做引擎探测、复用判定与渲染 argv 的单元测试。

- 目录里只有静态工程文件（`package.json`、入口源码、composition HTML），
  不包含任何媒体文件，也不包含 `node_modules`。
- 测试把整个骨架复制到临时目录后，再向临时副本写入假的
  `node_modules/.bin/remotion` / `node_modules/.bin/hyperframes` 脚本
  （内容见 `tests/test_video_engines.py`），用于验证 AE 传给引擎的固定
  argv、超时与失败恢复；不会调用本机真实的 Node / Chrome / FFmpeg。
- 实机双引擎演示（真实渲染、真实依赖）属于 #198 / PR review 的验收，
  不在本 fixture 范围内。

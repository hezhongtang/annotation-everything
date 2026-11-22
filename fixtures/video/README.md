# 双引擎视频审阅样例

在仓库根目录运行以下命令，生成两个独立的 640 × 360、30 fps 工程。每个工程只声明自己的引擎依赖。生成器使用 Python 标准库，图片、文字和声音均为合成材料。

```sh
demo_root=$(mktemp -d)
python3 fixtures/video/make_demos.py "$demo_root" --version V1 --ambiguous
(cd "$demo_root/remotion" && npm install)
(cd "$demo_root/hyperframes" && npm install)
```

Remotion 入口为 `src/index.tsx`，主合成 ID 为 `Demo`。HyperFrames 入口为 `index.html`，根合成元素声明帧率和时长。`--ambiguous` 还会生成 Remotion 的 `Alternate` 以及 HyperFrames 的 `compositions/alternate.html`。只需一个工程时，加 `--engine remotion` 或 `--engine hyperframes`。

| 源版本 | 帧数 | 参考帧 | 字幕框左上角（像素） | 440 Hz 目标音段 | PCM 峰值（满幅比例） |
| --- | ---: | ---: | --- | --- | ---: |
| V1 | 180 | 96 | (64, 288)，遮住 ICON | 4.0–6.0 秒 | 0.8 |
| V2 | 192 | 108 | (64, 216)，ICON 可见 | 4.4–6.4 秒 | 0.4 |
| V3 | 192 | 108 | (64, 216)，ICON 可见 | 4.4–6.4 秒 | 0.2 |

字幕框宽 320、高 36 像素，V1 归一化框为 `x=0.1, y=0.8, width=0.5, height=0.1`。带 ICON 的镜头从第 90 帧开始，持续 30 帧。V2 加入 12 帧片头，整段镜头和声音跟着后移。声音用 `wave` 写为 24 kHz、单声道、16 位 PCM，目标时段外的峰值均为 0.04。目标区每次返工约降低 6.02 dB。

给同一输出根传入新的 `--version` 即可改源。要保留歧义候选，每次均加 `--ambiguous`。以下示例逐次改源并在工程目录外存放各版成片，方便分别提交回执。

```sh
for version in V1 V2 V3; do
  python3 fixtures/video/make_demos.py "$demo_root" --version "$version" --ambiguous
  (cd "$demo_root/remotion" && ./node_modules/.bin/remotion render src/index.tsx Demo "$demo_root/remotion-$version.mp4" --codec=h264 --overwrite=false)
  (cd "$demo_root/hyperframes" && ./node_modules/.bin/hyperframes render . --format mp4 -o "$demo_root/hyperframes-$version.mp4" -c index.html)
done
```

不同版本的片头帧数、字幕左上角和音段振幅在生成器的 `REVISIONS` 中配置。生成的 Remotion 素材在 `public/`，HyperFrames 素材在工程根。可检查 `scene.svg` 中字幕与 ICON 的覆盖关系，并逐版测量解码后的音量。

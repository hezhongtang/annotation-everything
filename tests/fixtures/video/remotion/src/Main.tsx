// 合成主合成组件：30fps、640x360、180 帧的占位动画。
import { AbsoluteFill } from "remotion";

export const Main: React.FC = () => (
  <AbsoluteFill style={{ backgroundColor: "#102a43" }}>
    <h1 style={{ color: "#f0f4f8" }}>Synthetic Main composition</h1>
  </AbsoluteFill>
);

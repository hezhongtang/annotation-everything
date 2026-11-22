// 合成片头组件：24fps、320x180、48 帧的占位动画。
import { AbsoluteFill } from "remotion";

export const Intro: React.FC = () => (
  <AbsoluteFill style={{ backgroundColor: "#2b4162" }}>
    <h2 style={{ color: "#f0f4f8" }}>Synthetic Intro composition</h2>
  </AbsoluteFill>
);

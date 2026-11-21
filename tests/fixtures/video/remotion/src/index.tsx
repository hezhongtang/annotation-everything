// 合成 Remotion 入口：AE 只静态确认该文件存在，合成清单由引擎 CLI 输出。
import { Composition } from "remotion";
import { Main } from "./Main";
import { Intro } from "./Intro";

export const RemotionRoot: React.FC = () => (
  <>
    <Composition id="Main" component={Main} durationInFrames={180} fps={30} width={640} height={360} />
    <Composition id="Intro" component={Intro} durationInFrames={48} fps={24} width={320} height={180} />
  </>
);

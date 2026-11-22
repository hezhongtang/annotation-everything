"""Generate independent, synthetic Remotion and HyperFrames projects for video review."""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from string import Template


@dataclass(frozen=True)
class Revision:
    intro_frames: int
    subtitle_x: int
    subtitle_y: int
    amplitude: float


REVISIONS = {"V1": Revision(0, 64, 288, 0.8), "V2": Revision(12, 64, 216, 0.4), "V3": Revision(12, 64, 216, 0.2)}

REMOTION = """import React from "react";
import {AbsoluteFill, Composition, Html5Audio, Img, registerRoot, Sequence, staticFile} from "remotion";

const introFrames = $intro_frames;

const Demo = () => (
  <AbsoluteFill style={{backgroundColor: "#152b3c", color: "white", font: "32px sans-serif"}}>
    <div style={{margin: 32}}>Timing and sound check</div>
    {introFrames > 0 && <Sequence durationInFrames={introFrames}>
      <AbsoluteFill style={{background: "#634452", padding: 32}}>Synthetic intro</AbsoluteFill>
    </Sequence>}
    <Sequence from={90 + introFrames} durationInFrames={30}>
      <Img src={staticFile("scene.svg")} />
    </Sequence>
    <Html5Audio src={staticFile("tone.wav")} />
  </AbsoluteFill>
);

const Root = () => <>
  <Composition id="Demo" component={Demo} fps={30} width={640} height={360} durationInFrames={180 + introFrames}/>
  $alternate
</>;
registerRoot(Root);
"""

HYPERFRAMES = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Synthetic video review</title>
<style>
  * { box-sizing: border-box }
  body { margin: 0 }
  #root { position: relative; width: 640px; height: 360px; overflow: hidden;
          background: #152b3c; color: white; font: 32px sans-serif }
  .label { position: absolute; top: 32px; left: 32px }
  img { position: absolute; inset: 0; width: 640px; height: 360px }
</style></head><body>
<div id="root" data-composition-id="demo" data-fps="30" data-width="640" data-height="360" data-duration="$duration">
  <div class="label">Timing and sound check</div>
  $intro
  <img id="shot" class="clip" src="scene.svg" data-start="$shot_start" data-duration="1" data-track-index="1" alt="">
  <audio id="tone" class="clip" src="tone.wav" data-start="0" data-duration="$duration" data-track-index="2"></audio>
</div></body></html>
"""

SCENE = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
<rect width="640" height="360" fill="#152b3c"/>
<g font-family="sans-serif">
  <text x="32" y="68" font-size="36" fill="white">Subtitle placement</text>
  <rect x="270" y="290" width="88" height="32" rx="4" fill="#ffba4e"/>
  <text x="282" y="314" font-size="22" fill="#172434">ICON</text>
  <rect id="subtitle" x="$x" y="$y" width="320" height="36" fill="#16566b"/>
  <text x="$text_x" y="$text_y" font-size="22" fill="white">Synthetic subtitle</text>
</g></svg>
"""

ALTERNATE_HTML = """<!doctype html><html lang="en"><body>
<div id="root" data-composition-id="alternate" data-width="320" data-height="180" data-duration="1" data-fps="30">
  Alternate composition
</div></body></html>
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_tone(path: Path, revision: Revision) -> None:
    rate = 24000
    before = (120 + revision.intro_frames) * (rate // 30)
    pcm = array(
        "h",
        (
            round(32767 * (0.04 if i < before else revision.amplitude) * math.sin(2 * math.pi * 440 * i / rate))
            for i in range(before + 2 * rate)
        ),
    )
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        out.writeframes(pcm.tobytes())


def generate(root: Path, engine: str, revision: Revision, ambiguous: bool) -> None:
    project = root / engine
    if engine == "remotion":
        deps = {"@remotion/cli": "4.0.545", "remotion": "4.0.545", "react": "19.3.1", "react-dom": "19.3.1"}
        alternate = (
            '<Composition id="Alternate" component={Demo} fps={30} width={320} height={180} durationInFrames={30}/>'
        )
        entry = Template(REMOTION).substitute(
            intro_frames=revision.intro_frames, alternate=alternate if ambiguous else ""
        )
        write(project / "src/index.tsx", entry)
        assets = project / "public"
    else:
        deps = {"hyperframes": "0.9.80"}
        intro_seconds = revision.intro_frames / 30
        intro = (
            f'<div class="clip" id="intro" data-start="0" data-duration="{intro_seconds:g}" data-track-index="0" '
            'style="position:absolute;inset:0;padding:32px;background:#634452">Synthetic intro</div>'
        )
        html = Template(HYPERFRAMES).substitute(
            duration=f"{6 + intro_seconds:g}", shot_start=f"{3 + intro_seconds:g}", intro=intro if intro_seconds else ""
        )
        write(project / "index.html", html)
        assets = project
        alternate_path = project / "compositions/alternate.html"
        if ambiguous:
            write(alternate_path, ALTERNATE_HTML)
        elif alternate_path.is_file() and alternate_path.read_text(encoding="utf-8") == ALTERNATE_HTML:
            alternate_path.unlink()
    package = {"name": f"ae-video-demo-{engine}", "private": True, "dependencies": deps}
    write(project / "package.json", json.dumps(package, indent=2) + "\n")
    x, y = revision.subtitle_x, revision.subtitle_y
    write(assets / "scene.svg", Template(SCENE).substitute(x=x, y=y, text_x=x + 11, text_y=y + 26))
    write_tone(assets / "tone.wav", revision)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--engine", choices=("remotion", "hyperframes", "both"), default="both")
    parser.add_argument("--version", choices=list(REVISIONS), default="V1")
    parser.add_argument("--ambiguous", action="store_true", help="Include a second named composition in each project")
    args = parser.parse_args()
    for engine in ("remotion", "hyperframes") if args.engine == "both" else (args.engine,):
        generate(args.root, engine, REVISIONS[args.version], args.ambiguous)


if __name__ == "__main__":
    main()

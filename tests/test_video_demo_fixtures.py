"""通过生成器 CLI 与工程探测、PCM 文件验证可重复使用的双引擎验收材料。"""

import math
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "fixtures" / "video" / "make_demos.py"
sys.path.insert(0, str(ROOT / "skill" / "annotation-everything" / "scripts"))
import video_engines


class VideoDemoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def generate(self, *options):
        subprocess.run([sys.executable, str(GENERATOR), str(self.root), *options], check=True, capture_output=True)

    def test_independent_projects_have_supported_entries(self):
        self.generate()
        remotion = video_engines.detect_project(self.root / "remotion")
        self.assertEqual(remotion["engine"], "remotion")
        self.assertEqual(remotion["entry"], (self.root / "remotion/src/index.tsx").resolve())
        hyperframes = video_engines.detect_project(self.root / "hyperframes")
        self.assertEqual(hyperframes["engine"], "hyperframes")
        self.assertEqual([c["id"] for c in hyperframes["compositions"]], ["index.html"])

    def test_ambiguous_projects_offer_two_named_candidates(self):
        self.generate("--ambiguous")
        entry = video_engines.detect_project(self.root / "remotion")["entry"]
        self.assertEqual(set(re.findall(r'<Composition id="([^"]+)"', entry.read_text())), {"Demo", "Alternate"})
        info = video_engines.detect_project(self.root / "hyperframes")
        candidates = video_engines.list_compositions(info)
        with self.assertRaises(video_engines.EngineError) as error:
            video_engines.choose_composition(candidates, None, info["engine"], info["root"])
        self.assertEqual(error.exception.code, "compositionAmbiguous")
        self.assertEqual([c["id"] for c in candidates], ["compositions/alternate.html", "index.html"])

    def test_revisions_shift_the_subtitle_and_reduce_only_the_target_audio(self):
        rms_by_engine = {"remotion": [], "hyperframes": []}
        for version, start, top, duration in (("V1", 4.0, 288, 6.0), ("V2", 4.4, 216, 6.4), ("V3", 4.4, 216, 6.4)):
            self.generate("--version", version)
            for engine, levels in rms_by_engine.items():
                assets = self.root / engine
                if engine == "remotion":
                    assets /= "public"
                subtitle = ET.parse(assets / "scene.svg").find(".//*[@id='subtitle']")
                self.assertEqual(int(subtitle.get("y")), top)
                with wave.open(str(assets / "tone.wav")) as pcm:
                    rate = pcm.getframerate()
                    self.assertEqual(pcm.getnframes(), round(duration * rate))
                    self.assertEqual((pcm.getnchannels(), pcm.getsampwidth()), (1, 2))

                    def rms_at(t):
                        pcm.setpos(round(t * rate))
                        samples = [sample for (sample,) in struct.iter_unpack("<h", pcm.readframes(rate // 10))]
                        return math.sqrt(sum(x * x for x in samples) / len(samples))

                    background = rms_at(start - 0.2)
                    level = rms_at(start + 0.2)
                    self.assertGreater(level, background * 2)
                    levels.append(level)
        for v1, v2, v3 in rms_by_engine.values():
            self.assertGreater(v1, v2 * 1.5)
            self.assertGreater(v2, v3 * 1.5)

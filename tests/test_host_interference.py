"""浏览器组 × 安装维护组干扰场景的回归测试（issue #52）。

复现真实机制：同一检出里并行启动的真实 serve.py（浏览器组的行为）会进入
ae.py doctor 的真实全机进程枚举，且不在安装维护侧的台账里，被判成残留服务——
这就是 onboarding 记录过的「进程盘点干扰触发串行重测」。

修复不是删断言，而是隔离协议：两个参与方都持有 host-state 锁后必然串行，
读全机视图的一方不会再看到另一方临界区里的真实服务。本模块自身启动真实
serve.py 并跑真实 doctor，因此在敏感清单里、模块导入时取锁。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

# 本模块触及真实进程视图与端口：与 verify.py / unittest 直跑共用同一隔离协议
host_isolation.require_for_module(__name__)

import e2e_servers  # noqa: E402

AE = ROOT / "ae.py"
REVIEW_ID = "0000000000000052"


def make_review_dir(root: Path) -> Path:
    directory = root / "review"
    directory.mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>干扰复现合成页</title>",
                                          encoding="utf-8")
    (directory / "review.json").write_text(
        json.dumps({"reviewId": REVIEW_ID, "name": "干扰复现合成审阅"}, ensure_ascii=False),
        encoding="utf-8")
    return directory


def empty_ledger(home: Path) -> Path:
    """安装维护侧的空台账：能读到、但不含浏览器组登记的任何审阅。"""
    home.mkdir(parents=True, exist_ok=True)
    path = home / "reviews.json"
    path.write_text('{"version": 1, "reviews": {}}', encoding="utf-8")
    return path


class InterferenceRegressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ae-interference-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # 两个参与者各自持有一个合成台账：浏览器组登记在 server 侧，维护侧读自己的空台账
        self.server_home = self.root / "server-home"
        self.doctor_home = self.root / "doctor-home"
        empty_ledger(self.doctor_home)
        self.user_home = self.root / "user-home"
        self.user_home.mkdir()
        self.skills = self.user_home / ".claude" / "skills"

    def env_for(self, ae_home: Path) -> dict:
        return {**os.environ,
                "HOME": str(self.user_home),
                "CLAUDE_SKILLS_DIR": str(self.skills),
                "AE_HOME": str(ae_home),
                "PYTHONDONTWRITEBYTECODE": "1"}

    def doctor(self) -> dict:
        done = subprocess.run([sys.executable, str(AE), "doctor", "--json"],
                              env=self.env_for(self.doctor_home),
                              capture_output=True, text=True, timeout=90)
        return json.loads(done.stdout)

    @staticmethod
    def orphan_item(doc: dict) -> dict:
        return next(item for item in doc["results"] if item["code"] == "orphan-servers")

    def test_unserialised_real_server_is_reported_by_doctor_as_orphan(self):
        """干扰复现：真实 serve.py 运行时，维护侧 doctor 的全机枚举把它判成残留服务。"""
        review = make_review_dir(self.root)
        server = e2e_servers.start_server(review, e2e_servers.free_port(),
                                          scripts=ROOT / "skill" / "annotation-everything" / "scripts")
        # serve 在自己的台账侧登记；让它真正出现在 /api/health 里再盘点
        self.addCleanup(e2e_servers.stop, server)
        item = self.orphan_item(self.doctor())
        self.assertEqual(item["level"], "失败",
                         "并行真实服务应当被 doctor 的全机枚举捕获（干扰复现）")
        self.assertIn(str(server.pid), item["message"], "报告的残留应是本测试启动的 serve.py")

    def test_serialised_by_protocol_doctor_no_longer_sees_the_server(self):
        """隔离修复：serve 所在临界区结束（进程已退出）后，同一 doctor 不再报告它。"""
        review = make_review_dir(self.root)
        server = e2e_servers.start_server(review, e2e_servers.free_port(),
                                          scripts=ROOT / "skill" / "annotation-everything" / "scripts")
        e2e_servers.stop(server)
        item = self.orphan_item(self.doctor())
        self.assertNotEqual(item["level"], "失败",
                            "服务退出后 doctor 仍报告残留：干扰没有被隔离修复")
        self.assertNotIn(str(server.pid), item.get("message", ""))

    def test_module_participates_in_host_state_protocol(self):
        """本模块（真实干扰回归）在敏感清单里：直接跑 unittest 也受锁保护。

        用归一入口查询：discover 加载时 __name__ 无 tests. 前缀，直接查
        MODULE_RESOURCES 会落空，恰是协议失效的形态。
        """
        self.assertEqual(host_isolation.resources_for_module(__name__), ("host-state",))


if __name__ == "__main__":
    unittest.main()

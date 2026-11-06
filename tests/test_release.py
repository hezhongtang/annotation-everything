"""自动发版：只在版本号前进时打标签，重复运行时只补缺失的 Release。"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("release_tool", ROOT / "tools" / "release.py")
release = importlib.util.module_from_spec(SPEC)
sys.modules["release_tool"] = release
SPEC.loader.exec_module(release)

REPO = "owner/project"
SHA = "a" * 40


class PlanTests(unittest.TestCase):
    def test_new_version_creates_tag_and_release(self):
        plan = release.make_plan("0.12.1", ["v0.12.0"], ["v0.12.0"])
        self.assertEqual(plan.new_tag, "v0.12.1")
        self.assertEqual(plan.missing, [("v0.12.1", "v0.12.0")])
        self.assertEqual(plan.latest, "v0.12.1")

    def test_existing_tag_is_not_recreated(self):
        plan = release.make_plan("0.12.0", ["v0.12.0"], ["v0.12.0"])
        self.assertIsNone(plan.new_tag)
        self.assertEqual(plan.missing, [])

    def test_version_lower_than_latest_tag_never_publishes(self):
        plan = release.make_plan("0.11.5", ["v0.12.0"], ["v0.12.0"])
        self.assertIsNone(plan.new_tag)

    def test_backfill_uses_numeric_order_and_previous_tag(self):
        plan = release.make_plan("0.10.0", ["v0.10.0", "v0.9.0", "v0.2.0"], [])
        self.assertEqual(plan.missing, [("v0.2.0", None), ("v0.9.0", "v0.2.0"), ("v0.10.0", "v0.9.0")])
        self.assertEqual(plan.latest, "v0.10.0")

    def test_non_release_tags_are_ignored(self):
        plan = release.make_plan("1.0.0", ["v1.0.0", "v1.0.0-rc1", "nightly"], ["v1.0.0"])
        self.assertEqual(plan.missing, [])

    def test_malformed_version_is_rejected(self):
        with self.assertRaises(release.ReleaseError):
            release.make_plan("1.0", [], [])


class FakeHost:
    """记录写操作，并按合成状态回答查询。"""

    def __init__(self, version="0.12.1", tags=("v0.12.0",), releases=(), milestones=(), on_main=True,
                 ci_success=1):
        self.version, self.tags, self.releases = version, list(tags), list(releases)
        self.milestones, self.on_main = list(milestones), on_main
        self.ci_success = ci_success
        self.ci_queries = []
        self.calls = []

    def __call__(self, args):
        if args[:2] == ["git", "show"]:
            return f'VERSION = "{self.version}"\n'
        if args[:2] == ["git", "tag"] and "--list" in args:
            return "\n".join(self.tags)
        if args[:2] == ["git", "merge-base"]:
            if not self.on_main:
                raise release.ReleaseError("not on main")
            return ""
        if args[:3] == ["gh", "release", "list"]:
            return "\n".join(self.releases)
        if args[:2] == ["gh", "api"] and "actions/workflows/ci.yml/runs" in args[2]:
            self.ci_queries.append(args[2])
            return str(self.ci_success)
        if args[:2] == ["gh", "api"] and "--method" not in args:
            return json.dumps(self.milestones)
        self.calls.append(args)
        return ""


class ExecuteTests(unittest.TestCase):
    def test_publishes_tag_release_and_closes_finished_milestone(self):
        host = FakeHost(releases=["v0.12.0"],
                        milestones=[{"number": 6, "title": "v0.12.1", "open_issues": 0}])
        release.execute(host, REPO, SHA)
        joined = [" ".join(call) for call in host.calls]
        self.assertTrue(any("tag -a v0.12.1 " + SHA in call for call in joined), joined)
        self.assertTrue(any(call.endswith("push origin refs/tags/v0.12.1") for call in joined), joined)
        created = next(call for call in host.calls if call[:3] == ["gh", "release", "create"])
        self.assertEqual(created[3], "v0.12.1")
        self.assertIn("--verify-tag", created)
        self.assertIn("--generate-notes", created)
        self.assertEqual(created[created.index("--notes-start-tag") + 1], "v0.12.0")
        self.assertIn("--latest", created)
        self.assertTrue(any("repos/owner/project/milestones/6" in call for call in joined), joined)

    def test_new_tag_requires_successful_ci_on_the_exact_commit(self):
        host = FakeHost(releases=["v0.12.0"], ci_success=0)
        with self.assertRaises(release.ReleaseError):
            release.execute(host, REPO, SHA)
        self.assertEqual(host.calls, [], "CI 未成功时不能有任何写操作")
        self.assertEqual(len(host.ci_queries), 1)
        query = host.ci_queries[0]
        for part in (f"head_sha={SHA}", "event=push", "status=success"):
            self.assertIn(part, query)

    def test_dry_run_also_reports_unverified_ci(self):
        host = FakeHost(releases=["v0.12.0"], ci_success=0)
        with self.assertRaises(release.ReleaseError):
            release.execute(host, REPO, SHA, dry_run=True)

    def test_backfilling_existing_tags_does_not_need_ci(self):
        host = FakeHost(version="0.12.0", tags=["v0.12.0"], ci_success=0)
        release.execute(host, REPO, SHA)
        self.assertEqual(host.ci_queries, [])
        self.assertEqual(host.calls[0][:4], ["gh", "release", "create", "v0.12.0"])

    def test_milestone_with_open_issues_stays_open(self):
        host = FakeHost(releases=["v0.12.0"],
                        milestones=[{"number": 6, "title": "v0.12.1", "open_issues": 2}])
        release.execute(host, REPO, SHA)
        self.assertFalse(any("milestones/6" in " ".join(call) for call in host.calls))

    def test_backfilled_older_release_is_not_marked_latest(self):
        host = FakeHost(version="0.12.0", tags=["v0.11.0", "v0.12.0"])
        release.execute(host, REPO, SHA)
        created = [call for call in host.calls if call[:3] == ["gh", "release", "create"]]
        self.assertEqual([call[3] for call in created], ["v0.11.0", "v0.12.0"])
        self.assertIn("--latest=false", created[0])
        self.assertIn("--latest", created[1])

    def test_second_run_changes_nothing(self):
        host = FakeHost(version="0.12.0", tags=["v0.12.0"], releases=["v0.12.0"])
        release.execute(host, REPO, SHA)
        self.assertEqual(host.calls, [])

    def test_commit_outside_main_is_refused_before_any_write(self):
        host = FakeHost(on_main=False)
        with self.assertRaises(release.ReleaseError):
            release.execute(host, REPO, SHA)
        self.assertEqual(host.calls, [])

    def test_dry_run_writes_nothing(self):
        host = FakeHost(releases=["v0.12.0"])
        release.execute(host, REPO, SHA, dry_run=True)
        self.assertEqual(host.calls, [])

    def test_malformed_sha_is_refused(self):
        with self.assertRaises(release.ReleaseError):
            release.execute(FakeHost(), REPO, "--upload-pack=x")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_runs_only_after_successful_ci_on_main_push(self):
        self.assertIn("workflows: [CI]", self.text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", self.text)
        self.assertIn("github.event.workflow_run.event == 'push'", self.text)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", self.text)

    def test_checks_out_default_branch_with_history(self):
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", self.text)
        self.assertIn("fetch-depth: 0", self.text)

    def test_grants_only_the_write_scopes_it_needs(self):
        self.assertIn("contents: write", self.text)
        self.assertIn("issues: write", self.text)
        self.assertIn("actions: read", self.text)
        self.assertNotIn("write-all", self.text)
        self.assertNotIn("pull_request_target", self.text)


if __name__ == "__main__":
    unittest.main()

"""Full publication and lost-response recovery against fake GitHub and local Git."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_release

release = test_release.release
github = release.github_release


class Remote(test_release.Remote):
    def __init__(self, manifest):
        super().__init__(manifest)
        self.remote_source = None
        self.item = None
        self.history = []
        self.latest = None
        self.github_writes = []
        self.github_fail = None
        self.before_effect = False
        self.api_error = None
        self.can_push = True
        self.bare = None

    def run(self, argv, root, timeout=120):
        if argv[0] == "git":
            self.calls.append(argv)
            if argv[1] == "ls-remote":
                if self.bare:
                    return release.Runner().run([*argv[:3], str(self.bare), *argv[4:]], root)
                if self.remote_source is None:
                    raise release.CommandFailure(2)
                tag = "v" + self.manifest["version"]
                return f"{'f' * 40}\trefs/tags/{tag}\n{self.remote_source}\trefs/tags/{tag}^{{}}"
            if argv[1] == "push":
                operation = "tag:push"
                effect = lambda: self.push(argv, root)
            elif argv[1:3] == ["tag", "-a"]:
                operation = "tag:create"
                effect = lambda: release.Runner().run(argv, root)
            else:
                return release.Runner().run(argv, root)
        elif argv[:2] == ["gh", "api"]:
            self.calls.append(argv)
            endpoint = argv[-1]
            if self.api_error and endpoint != "repos/example/ExampleApp":
                raise release.CommandFailure(1, f"HTTP/2.0 {self.api_error} Error\n\n{{}}", "SECRET")
            if endpoint == "repos/example/ExampleApp":
                data = {"id": 123, "full_name": "example/ExampleApp", "permissions": {"push": self.can_push}}
            elif endpoint.endswith("?per_page=100"):
                return json.dumps([self.history, [self.item] if self.item else []])
            elif endpoint.endswith("/latest"):
                data = self.latest
            elif "/releases/tags/" in endpoint:
                data = self.item
            else:
                raise AssertionError(argv)
            if data is None:
                raise release.CommandFailure(1, 'HTTP/2.0 404 Not Found\n\n{"message":"Not Found"}', "not found")
            return "HTTP/2.0 200 OK\nContent-Type: application/json\n\n" + json.dumps(data)
        elif argv[:3] == ["gh", "release", "create"]:
            self.calls.append(argv)
            operation = "release:create"
            def effect():
                self.item = {"id": 321, "tag_name": argv[3], "name": self.flag(argv, "--title"),
                    "body": Path(self.flag(argv, "--notes-file")).read_text(), "draft": False, "prerelease": False,
                    "published_at": "2026-10-16T00:00:00Z", "html_url": f"https://github.com/example/ExampleApp/releases/tag/{argv[3]}"}
                if "--latest=true" in argv:
                    self.latest = self.item
        else:
            return super().run(argv, root, timeout)
        self.github_writes.append(operation)
        if self.github_fail == operation and self.before_effect:
            raise release.CommandFailure(1)
        effect()
        if self.github_fail == operation:
            raise release.CommandFailure(1)
        return ""

    def push(self, argv, root):
        if self.bare:
            return release.Runner().run([*argv[:2], str(self.bare), *argv[3:]], root)
        self.remote_source = self.build_source


class GitHubTest(unittest.TestCase):
    accept_all = test_release.ReleaseTest.accept_all
    pipeline = test_release.ReleaseTest.pipeline
    finish = test_release.ReleaseTest.finish
    count = test_release.ReleaseTest.count

    def setUp(self):
        test_release.ReleaseTest.setUp(self)
        for args in (("remote", "add", "origin", "https://github.com/example/ExampleApp.git"),
                     ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            release.git(self.root, *args)
        self.remote = Remote(self.manifest)

    def full(self):
        result = self.pipeline().run("github")
        if result["status"] == "pending":
            result = self.pipeline().run("github")
        return result

    def journal(self):
        return release.read_json(self.pipeline().path.with_name("github.json"))

    def test_full_release_and_repeated_resume_publish_once(self):
        result = self.full()
        self.assertEqual(result["through"], "github")
        self.assertEqual(result["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.pipeline().run("github")["status"], "complete")
        self.assertEqual(self.remote.github_writes, ["tag:create", "tag:push", "release:create"])
        for command in (["eas", "build"], ["eas", "submit"], ["asc", "review", "submit"]):
            self.assertEqual(self.count(command), 1)
        self.assertEqual(self.journal()["intent"]["source_commit"], self.manifest["source_commit"])
        self.assertNotIn("SECRET", self.pipeline().path.read_text() + json.dumps(self.journal()))
        create = next(argv for argv in self.remote.calls if argv[:3] == ["gh", "release", "create"])
        self.assertIn("--verify-tag", create)
        self.assertIn("github.com/example/ExampleApp", create)

    def test_real_annotated_tag_and_push_use_only_a_local_bare_remote(self):
        self.remote.bare = self.root / ".artifacts/remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote.bare)], check=True, capture_output=True)
        self.full()
        source = release.git(self.root, "--git-dir=" + str(self.remote.bare), "rev-parse", "refs/tags/v9.9.9^{commit}")
        kind = release.git(self.root, "--git-dir=" + str(self.remote.bare), "cat-file", "-t", "refs/tags/v9.9.9")
        self.assertEqual(source, self.manifest["source_commit"])
        self.assertEqual(kind, "tag")

    def test_apple_only_does_not_use_gh_or_create_tags(self):
        self.finish()
        self.assertFalse(any(argv[0] == "gh" for argv in self.remote.calls))
        self.assertEqual(self.remote.github_writes, [])
        self.assertFalse(self.pipeline().path.with_name("github.json").exists())

    def test_offline_commands_have_no_service_calls_or_publication_journal(self):
        original = release.Runner.run
        def offline(instance, argv, root, timeout=120):
            self.assertEqual(argv[0], "git")
            self.assertNotIn(argv[1], ("push", "ls-remote"))
            return original(instance, argv, root, timeout)
        for args in (("run", "--through", "github"), ("plan", "--through", "github"), ("github",)):
            with self.subTest(args=args), patch.object(release.Runner, "run", offline), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release.main([*args, "--manifest", str(self.path)]), 0)
                data = json.loads(output.getvalue())
                self.assertEqual(data["remote_calls"], 0)
                self.assertEqual(data["phases"][-1]["phase"], "github")
        self.assertFalse(self.pipeline().path.with_name("github.json").exists())

    def test_each_lost_response_reconciles_without_duplicate_mutation(self):
        for operation in ("tag:create", "tag:push", "release:create"):
            with self.subTest(operation=operation):
                self.finish()
                self.remote.github_fail = operation
                with self.assertRaises(github.GitHubUncertain):
                    self.pipeline().github()
                self.assertEqual(self.journal()["pending"]["operation"], operation)
                self.remote.github_fail = None
                self.assertEqual(self.pipeline().run("github")["status"], "complete")
                self.assertEqual(self.remote.github_writes.count(operation), 1)
                self.assertEqual(self.count(["asc", "review", "submit"]), 1)
                # Use a fresh fixture for the next independent fault.
                if operation != "release:create":
                    self.tearDown()
                    self.setUp()

    def test_absence_after_unknown_write_never_replays(self):
        self.finish()
        self.remote.github_fail, self.remote.before_effect = "release:create", True
        with self.assertRaises(github.GitHubUncertain):
            self.pipeline().github()
        self.remote.github_fail = None
        with self.assertRaisesRegex(github.GitHubUncertain, "unresolved"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes.count("release:create"), 1)

    def test_backfill_and_full_resume_work_after_head_moves_to_next_version(self):
        self.finish()
        for name in ("app.json", "package.json"):
            value = release.read_json(self.root / name)
            (value["expo"] if name == "app.json" else value)["version"] = "10.0.0"
            release.atomic_json(self.root / name, value)
        release.git(self.root, "add", "app.json", "package.json")
        release.git(self.root, "commit", "-qm", "next version")
        self.assertEqual(self.pipeline().github()["status"], "complete")
        self.assertEqual(self.pipeline().run("github")["status"], "complete")
        self.assertEqual(release.git(self.root, "rev-parse", "v9.9.9^{commit}"), self.manifest["source_commit"])
        self.assertEqual(self.count(["eas", "build"]), 1)

    def test_missing_github_permission_blocks_full_release_before_build(self):
        self.remote.can_push = False
        with self.assertRaisesRegex(github.GitHubError, "write access"):
            self.full()
        self.assertEqual(self.remote.built, 0)
        self.assertEqual(self.remote.github_writes, [])

    def test_conflicting_local_and_remote_tags_are_preserved(self):
        self.finish()
        self.remote.remote_source = "a" * 40
        with self.assertRaisesRegex(github.GitHubError, "tag conflicts"):
            self.pipeline().github()
        self.assertEqual(self.remote.remote_source, "a" * 40)
        self.assertEqual(self.remote.github_writes, [])

    def test_403_is_not_treated_as_missing_release(self):
        self.finish()
        self.remote.api_error = 403
        with self.assertRaisesRegex(github.GitHubError, "not absence"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, [])

    def test_older_backfill_does_not_take_latest(self):
        self.remote.history = [{"id": 999, "tag_name": "v10.0.0", "draft": False, "prerelease": False}]
        self.remote.latest = self.remote.history[0]
        result = self.full()
        self.assertFalse(result["github"]["latest"])
        self.assertEqual(self.remote.latest["id"], 999)

    def test_existing_release_conflict_does_not_overwrite(self):
        self.full()
        self.remote.item["body"] = "Changed externally"
        writes = list(self.remote.github_writes)
        with self.assertRaisesRegex(github.GitHubError, "conflicts"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, writes)

    def test_changed_body_during_pending_operation_preserves_intent(self):
        self.finish()
        self.remote.github_fail = "release:create"
        with self.assertRaises(github.GitHubUncertain):
            self.pipeline().github()
        previous = self.journal()
        (self.directory / "release-notes.md").write_text("Fixed a different thing.\n")
        release.changelog.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        with self.assertRaises((release.Blocked, github.GitHubUncertain)):
            self.pipeline().github()
        self.assertEqual(self.journal(), previous)

    def test_rejected_apple_version_blocks_github_without_apple_mutations(self):
        self.finish()
        self.remote.version["attributes"]["appVersionState"] = "REJECTED"
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(release.Blocked, "formally submitted"):
            self.pipeline().github()
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.remote.github_writes, [])

    def test_status_is_exact_read_only_and_reports_both_channels(self):
        self.full()
        before = self.pipeline().path.read_bytes(), self.pipeline().path.with_name("github.json").read_bytes()
        result = release.release_status(self.path, self.remote)
        self.assertEqual(result["overall_status"], "complete")
        self.assertEqual(result["completed_through"], "github")
        self.assertEqual(result["apple"]["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(result["github"]["status"], "verified")
        self.assertEqual(before, (self.pipeline().path.read_bytes(), self.pipeline().path.with_name("github.json").read_bytes()))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_status_reports_pending_github_and_keeps_apple_result(self):
        self.finish()
        self.remote.github_fail = "release:create"
        with self.assertRaises(github.GitHubUncertain):
            self.pipeline().github()
        result = release.release_status(self.path, self.remote)
        self.assertEqual(result["overall_status"], "uncertain")
        self.assertEqual(result["apple"]["status"], "verified")
        self.assertEqual(result["pending_operations"], [{"channel": "github", "operation": "release:create"}])

    def test_notes_pending_blocks_publication_and_candidate(self):
        self.finish()
        release.atomic_json(self.pipeline().path.with_name("notes.json"), {"operations": {"github:body": {"status": "pending"}}})
        for method in (self.pipeline().github, self.pipeline().candidate):
            with self.assertRaisesRegex(github.GitHubUncertain, "pending notes"):
                method()
        self.assertEqual(self.remote.github_writes, [])

    def test_push_destinations_are_checked_before_build(self):
        release.git(self.root, "remote", "set-url", "--push", "origin", "https://github.com/other/Repo.git")
        with self.assertRaisesRegex(github.GitHubError, "destinations differ"):
            self.full()
        self.assertEqual(self.remote.built, 0)

    def test_matching_remote_release_is_reused_without_local_tag_or_journal(self):
        self.full()
        before = list(self.remote.github_writes)
        self.pipeline().path.with_name("github.json").unlink()
        release.git(self.root, "tag", "-d", "v9.9.9")
        self.assertEqual(self.pipeline().github()["status"], "complete")
        self.assertEqual(self.remote.github_writes, before)
        self.assertEqual(release.git(self.root, "tag", "--list", "v9.9.9"), "")

    def test_existing_release_with_missing_tag_is_not_repaired_implicitly(self):
        self.full()
        self.pipeline().path.with_name("github.json").unlink()
        self.remote.remote_source = None
        before = list(self.remote.github_writes)
        with self.assertRaisesRegex(github.GitHubError, "no verified matching remote tag"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, before)

    def test_conflicting_local_tag_is_preserved_after_head_moves(self):
        self.finish()
        release.git(self.root, "commit", "--allow-empty", "-qm", "later commit")
        release.git(self.root, "tag", "v9.9.9")
        before = release.git(self.root, "rev-parse", "v9.9.9")
        with self.assertRaisesRegex(github.GitHubError, "tag conflicts"):
            self.pipeline().github()
        self.assertEqual(release.git(self.root, "rev-parse", "v9.9.9"), before)
        self.assertEqual(self.remote.github_writes, [])

    def test_unknown_api_failure_with_404_in_stderr_is_not_absence(self):
        self.finish()
        original = self.remote.run
        def run(argv, root, timeout=120):
            if argv[:2] == ["gh", "api"] and "/releases/tags/" in argv[-1]:
                raise release.CommandFailure(1, "", "404 in an unrelated proxy error SECRET")
            return original(argv, root, timeout)
        with patch.object(self.remote, "run", run), self.assertRaisesRegex(github.GitHubError, "not absence"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, [])

    def test_paginated_numeric_history_and_ambiguous_tags(self):
        self.remote.history = [{"tag_name": "v9.10.0", "draft": False, "prerelease": False}]
        self.remote.latest = {"id": 999}
        self.assertFalse(self.full()["github"]["latest"])
        self.remote.history = [{"tag_name": "release-10", "draft": False, "prerelease": False}]
        with self.assertRaisesRegex(github.GitHubError, "ambiguous"):
            self.pipeline().github()

    def test_latest_conflict_prevents_status_from_claiming_completion(self):
        self.full()
        self.remote.latest = {"id": 999}
        self.assertEqual(release.release_status(self.path, self.remote)["overall_status"], "blocked")

    def test_release_lock_prevents_concurrent_publication(self):
        self.finish()
        with release.locked(self.pipeline().path), self.assertRaisesRegex(release.Blocked, "Another release"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, [])

    def test_cli_failure_keeps_apple_result_and_identifies_github_phase(self):
        self.finish()
        self.remote.github_fail = "release:create"
        with patch.object(release, "Pipeline", return_value=self.pipeline()), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release.main(["github", "--manifest", str(self.path), "--execute"]), 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result["phase"], "github")
        self.assertEqual(result["completed_through"], "review")
        self.assertNotIn("SECRET", output.getvalue())

    def test_publication_journal_blocks_source_rebinding(self):
        self.full()
        with self.assertRaisesRegex(release.Blocked, "GitHub publication intent"):
            self.pipeline().candidate()

    def test_dynamic_source_does_not_trust_stale_static_config_or_execute_current_config(self):
        app = release.read_json(self.root / "app.json")
        app["expo"]["version"] = "0.0.0"
        release.atomic_json(self.root / "app.json", app)
        (self.root / "app.config.ts").write_text("throw new Error('must not execute config during backfill');\n")
        release.git(self.root, "add", "app.json", "app.config.ts")
        release.git(self.root, "commit", "-qm", "dynamic candidate fixture")
        self.manifest["source_commit"] = release.git(self.root, "rev-parse", "HEAD")
        release.atomic_json(self.path, self.manifest)
        source, title = self.pipeline().publisher().source_info("example/ExampleApp")
        self.assertEqual(source, self.manifest["source_commit"])
        self.assertEqual(title, "ExampleApp v9.9.9")

    def test_optional_title_is_frozen_before_first_publication(self):
        self.manifest["github"] = {"title": "Custom App 9.9.9"}
        release.atomic_json(self.path, self.manifest)
        self.full()
        self.assertEqual(self.remote.item["name"], "Custom App 9.9.9")
        self.manifest["github"]["title"] = "Changed title"
        release.atomic_json(self.path, self.manifest)
        with self.assertRaisesRegex(github.GitHubUncertain, "changed"):
            self.pipeline().github()

    def test_ambiguous_history_on_later_page_is_not_hidden_by_a_newer_version(self):
        self.finish()
        self.remote.history = [{"tag_name": "v10.0.0", "draft": False, "prerelease": False},
                               {"tag_name": "legacy-release", "draft": False, "prerelease": False}]
        with self.assertRaisesRegex(github.GitHubError, "ambiguous"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, [])

    def test_command_error_string_does_not_expose_captured_output(self):
        error = release.CommandFailure(1, "SECRET response", "SECRET credential failure")
        self.assertNotIn("SECRET", str(error))

    def test_recreated_release_id_is_a_conflict_even_when_body_and_tag_match(self):
        self.full()
        self.remote.item["id"] = 999
        before = list(self.remote.github_writes)
        with self.assertRaisesRegex(github.GitHubError, "Release ID changed"):
            self.pipeline().github()
        self.assertEqual(self.remote.github_writes, before)
        self.assertEqual(self.journal()["release_id"], 321)


if __name__ == "__main__":
    unittest.main()

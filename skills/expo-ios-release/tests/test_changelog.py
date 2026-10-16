"""Notes generation and simulated service writes; never contacts a real service."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_release

release = test_release.release
notes = release.changelog
BODY = "【新增】\n1、人物资料可查看与复制。\n\n【优化】\n1、日期选择更方便。\n\n谢谢支持。"


class FakeNotesRemote:
    def __init__(self):
        self.writes = []
        self.fail = None
        self.before_effect = False
        self.corrupt_review_access = False
        self.version = {"id": "version", "attributes": {"versionString": "9.9.9", "releaseType": "MANUAL", "appVersionState": "WAITING_FOR_REVIEW"}, "relationships": {"build": {"data": {"id": "build"}}}}
        self.locale = {"id": "locale", "attributes": {"locale": "zh-Hans", "whatsNew": "old", "description": "keep description"}}
        self.review = {"id": "review", "attributes": {"notes": "old review", "contactEmail": "review@example.invalid", "demoAccountRequired": True}}
        self.github = {"body": "old github", "tagName": "v9.9.9", "name": "ExampleApp v9.9.9", "isDraft": False, "isPrerelease": False}
        self.beta = {"id": "beta", "attributes": {"locale": "zh-Hans", "whatsNew": "old tests"}}
        self.github_id = 321

    def run(self, argv, root):
        if argv[:3] == ["git", "remote", "get-url"]:
            return "https://github.com/example/ExampleApp.git"
        if argv[:2] == ["gh", "api"]:
            data = ({"id": 123, "full_name": "example/ExampleApp", "permissions": {"push": True}}
                    if argv[-1] == "repos/example/ExampleApp" else {"id": self.github_id})
            return "HTTP/2.0 200 OK\n\n" + json.dumps(data)
        if argv[:3] == ["asc", "versions", "list"]: return json.dumps({"data": [self.version]})
        if argv[:3] == ["asc", "localizations", "list"]: return json.dumps({"data": [self.locale]})
        if argv[:3] == ["asc", "review", "details-for-version"]: return json.dumps({"data": self.review})
        if argv[:3] == ["gh", "release", "view"]: return json.dumps(self.github)
        if argv[:4] == ["asc", "builds", "test-notes", "list"]: return json.dumps({"data": [self.beta]})
        self.writes.append(argv)
        def flag(name): return argv[argv.index(name) + 1]
        if self.fail and self.before_effect: raise OSError("request failed before result")
        if argv[:3] == ["asc", "localizations", "update"]:
            operation = "asc:whatsNew"; self.locale["attributes"]["whatsNew"] = flag("--whats-new")
        elif argv[:3] == ["asc", "review", "details-update"]:
            operation = "asc:reviewNotes"; self.review["attributes"]["notes"] = flag("--notes")
            if self.corrupt_review_access: self.review["attributes"]["demoAccountRequired"] = False
        elif argv[:3] == ["gh", "release", "edit"]:
            operation = "github:body"; self.github["body"] = Path(flag("--notes-file")).read_text()
        elif argv[:4] == ["asc", "builds", "test-notes", "update"]:
            operation = "testflight:whatToTest"; self.beta["attributes"]["whatsNew"] = flag("--whats-new")
        else: raise AssertionError(argv)
        if self.fail == operation: raise OSError("side effect happened, response lost")
        return "{}"


class ChangelogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.directory = self.root / "release-docs/versions/9.9.9"
        self.directory.mkdir(parents=True)
        (self.directory / "README.md").write_text("Confirmed scope")
        (self.directory / "release-notes.md").write_text(BODY + "\n")
        (self.directory / "review-notes.template.txt").write_text("Changes:\n{{CHANGELOG}}\n\nPrivacy and access: keep these instructions.\n")
        self.manifest = {"version": "9.9.9", "locales": ["zh-Hans"], "source_commit": "a" * 40}
        notes.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        self.remote = FakeNotesRemote()
        self.state = self.root / ".git/expo-ios-release/9.9.9/state.json"
        release.atomic_json(self.state, {"version_id": "version", "apple_build": {"id": "build"}})
        def git(root, *args):
            if args[:2] == ("remote", "get-url"): return "https://github.com/example/ExampleApp.git"
            if args[0] == "ls-remote": return "a" * 40 + "\trefs/tags/v9.9.9^{}"
            raise AssertionError(args)
        self.helpers = {"state_path": lambda root, version: self.state, "now": release.now,
                        "atomic_json": release.atomic_json, "app_id": "1000000001", "git": git, "github": release.github_release}

    def sync(self, execute=True, channels=None):
        return notes.remote_notes(self.root, self.directory, self.manifest, channels or ["asc", "github"], execute, self.remote, self.helpers)

    def test_plain_english_notes_and_invalid_content(self):
        self.assertEqual(notes.parse("Improved search.\n\nFixed calendar scrolling.")["body"], "Improved search.\n\nFixed calendar scrolling.")
        for text in ("", "TODO", "TBD", "x" * 4001):
            with self.subTest(text=text[:8]), self.assertRaises(notes.NotesError): notes.parse(text)

    def test_pending_publication_and_apple_writes_block_notes_mutations(self):
        for name in ("github", "state"):
            path = self.state.with_name(name + ".json")
            previous = path.read_bytes() if path.exists() else None
            release.atomic_json(path, {"pending": {"operation": "release:create" if name == "github" else "review"}})
            with self.subTest(name=name), self.assertRaisesRegex(release.github_release.GitHubUncertain, "pending"):
                self.sync(channels=["github"])
            self.assertEqual(self.remote.writes, [])
            if previous is None:
                path.unlink()
            else:
                path.write_bytes(previous)

    def test_notes_sync_refreshes_current_publication_hash_and_keeps_original(self):
        original = notes.body_digest("old github")
        path = self.state.with_name("github.json")
        release.atomic_json(path, {"schema": 1, "status": "verified", "publication_body_sha256": original,
            "intent": {"repository": "example/ExampleApp", "source_commit": self.manifest["source_commit"],
                       "tag": "v9.9.9", "body_sha256": original}})
        self.assertEqual(self.sync(channels=["github"])["status"], "verified")
        journal = release.read_json(path)
        self.assertEqual(journal["intent"]["body_sha256"], notes.body_digest(BODY))
        self.assertEqual(journal["publication_body_sha256"], original)
        self.assertEqual(journal["notes_history"][0]["body_sha256"], original)
        self.sync(channels=["github"])
        self.assertEqual(len(release.read_json(path)["notes_history"]), 1)

    def test_notes_sync_preserves_a_conflicting_publication_binding(self):
        path = self.state.with_name("github.json")
        release.atomic_json(path, {"schema": 1, "intent": {"repository": "other/Repository"}})
        before = path.read_bytes()
        with self.assertRaisesRegex(notes.NotesError, "journal differs"):
            self.sync(channels=["github"])
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(path.read_bytes(), before)

    def test_recreated_release_cannot_replace_publication_identity_during_notes_sync(self):
        release.atomic_json(self.state.with_name("github.json"), {"schema": 1, "release_id": 999,
            "intent": {"repository": "example/ExampleApp", "repository_id": 123,
                       "source_commit": self.manifest["source_commit"], "tag": "v9.9.9"}})
        with self.assertRaisesRegex(notes.NotesError, "Release ID changed"):
            self.sync(channels=["github"])
        self.assertEqual(self.remote.writes, [])

    def test_generate_is_repeatable_and_keeps_review_details(self):
        initial = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        notes.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        self.assertEqual(initial, {p: p.read_bytes() for p in initial})
        self.assertIn("Privacy and access: keep these instructions.", (self.directory / "review-notes.txt").read_text())
        data = notes.check_local(self.directory, "9.9.9", ["zh-Hans"])
        self.assertEqual(data["releaseNotes"], BODY)

    def test_local_drift_blocks_remote_calls(self):
        release.atomic_json(self.directory / "metadata/version/9.9.9/zh-Hans.json", {"whatsNew": "edited independently"})
        with self.assertRaisesRegex(notes.NotesError, "copies differ"): self.sync()
        self.assertEqual(self.remote.writes, [])

    def test_missing_or_duplicate_placeholder_does_not_write_files(self):
        before = (self.directory / "release-notes.json").read_bytes()
        for text in ("instructions only", "{{CHANGELOG}}\n{{CHANGELOG}}"):
            (self.directory / "review-notes.template.txt").write_text(text)
            with self.assertRaises(notes.NotesError): notes.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
            self.assertEqual(before, (self.directory / "release-notes.json").read_bytes())

    def test_dry_run_and_second_sync_do_not_write(self):
        self.assertEqual(self.sync(False)["status"], "differences")
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(self.sync()["status"], "verified")
        self.assertEqual(len(self.remote.writes), 3)
        self.assertEqual(self.sync()["status"], "verified")
        self.assertEqual(len(self.remote.writes), 3)
        self.assertEqual(self.remote.locale["attributes"]["description"], "keep description")
        self.assertTrue(self.remote.review["attributes"]["demoAccountRequired"])
        self.assertEqual(self.remote.version["attributes"]["releaseType"], "MANUAL")

    def test_lost_response_reconciles_without_repeating_write(self):
        self.remote.fail = "asc:whatsNew"
        with self.assertRaises(notes.NotesUncertain): self.sync()
        self.remote.fail = None
        self.assertEqual(self.sync()["status"], "verified")
        self.assertEqual(len(self.remote.writes), 3)

    def test_unresolved_write_is_not_retried(self):
        self.remote.fail = "asc:whatsNew"; self.remote.before_effect = True
        with self.assertRaises(notes.NotesUncertain): self.sync()
        self.remote.fail = None
        with self.assertRaisesRegex(notes.NotesUncertain, "unresolved"): self.sync()
        self.assertEqual(len(self.remote.writes), 1)

    def test_changed_body_cannot_replace_pending_write(self):
        self.remote.fail = "github:body"
        with self.assertRaises(notes.NotesUncertain): self.sync()
        (self.directory / "release-notes.md").write_text(BODY.replace("谢谢支持。", "感谢陪伴。"))
        notes.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        count = len(self.remote.writes)
        with self.assertRaisesRegex(notes.NotesUncertain, "previous"): self.sync()
        self.assertEqual(count, len(self.remote.writes))

    def test_wrong_build_is_rejected_before_notes_writes(self):
        self.remote.version["relationships"]["build"]["data"]["id"] = "other-build"
        with self.assertRaisesRegex(notes.NotesError, "checkpoint"): self.sync()
        self.assertEqual(self.remote.writes, [])

    def test_missing_build_checkpoint_blocks_writes_but_allows_inspection(self):
        self.state.unlink()
        with self.assertRaisesRegex(notes.NotesError, "checkpoint"): self.sync()
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(self.sync(False)["status"], "differences")

    def test_unexpected_review_access_change_stops_with_pending_receipt(self):
        self.remote.corrupt_review_access = True
        with self.assertRaisesRegex(notes.NotesUncertain, "Non-note fields"): self.sync()
        journal = json.loads((self.state.parent / "notes.json").read_text())
        self.assertEqual(journal["operations"]["asc:reviewNotes"]["status"], "pending")
        self.assertEqual(len(self.remote.writes), 2)

    def test_combined_testflight_and_asc_channels_keep_their_own_readers(self):
        (self.directory / "what-to-test.template.txt").write_text("{{CHANGELOG}}\n\nCheck copying.\n")
        notes.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        self.assertEqual(self.sync(channels=["asc", "testflight", "github"])["status"], "verified")
        self.assertEqual(len(self.remote.writes), 4)

    def test_testflight_uses_exact_build_and_separate_instructions(self):
        (self.directory / "what-to-test.template.txt").write_text("{{CHANGELOG}}\n\nTest focus: verify copying.\n")
        notes.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        self.assertEqual(self.sync(channels=["testflight"])["status"], "verified")
        self.assertIn(BODY, self.remote.beta["attributes"]["whatsNew"])
        self.assertIn("Test focus: verify copying.", self.remote.beta["attributes"]["whatsNew"])

    def test_translations_are_explicit_and_preserved(self):
        before = (self.directory / "release-notes.json").read_bytes()
        with self.assertRaisesRegex(notes.NotesError, "en-US"):
            notes.generate(self.directory, "9.9.9", ["zh-Hans", "en-US"], release.atomic_json)
        self.assertEqual(before, (self.directory / "release-notes.json").read_bytes())
        (self.directory / "release-notes.en-US.md").write_text("Improved profile sharing.")
        notes.generate(self.directory, "9.9.9", ["zh-Hans", "en-US"], release.atomic_json)
        self.assertEqual(json.loads((self.directory / "metadata/version/9.9.9/en-US.json").read_text())["whatsNew"], "Improved profile sharing.")
        self.assertEqual(json.loads((self.directory / "metadata/version/9.9.9/zh-Hans.json").read_text())["whatsNew"], BODY)

    def test_local_check_cli_is_read_only(self):
        config = {"schema": 1, "app_id": "1000000001", "project_id": "11111111-1111-4111-8111-111111111111",
                  "bundle_id": "com.example.demo", "build_profile": "production", "submit_profile": "production", "checks": [["node", "--test"]]}
        release.atomic_json(self.root / ".expo-ios-release.json", config)
        release.atomic_json(self.directory / "release.json", {**self.manifest, "schema": 2, "app_id": config["app_id"],
            "project_id": config["project_id"], "bundle_id": config["bundle_id"], "profile": "production", "submit_profile": "production",
            "release_type": "MANUAL", "acceptance_policy": {"production": "required", "device": "required"}})
        before = {p: p.read_bytes() for p in self.directory.rglob("*") if p.is_file()}
        with patch.object(release.Runner, "run", side_effect=AssertionError("service call")), contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["notes-check", "--root", str(self.root), "--version", "9.9.9"])
        self.assertEqual(code, 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_multi_locale_sync_updates_each_language_and_preserves_other_fields(self):
        self.manifest["locales"] = ["zh-Hans", "en-US"]
        (self.directory / "release-notes.en-US.md").write_text("Improved profile sharing.")
        (self.directory / "what-to-test.template.txt").write_text("{{CHANGELOG}}\n\n验证复制。")
        (self.directory / "what-to-test.en-US.template.txt").write_text("{{CHANGELOG}}\n\nVerify copying.")
        notes.generate(self.directory, "9.9.9", self.manifest["locales"], release.atomic_json)
        locales = {loc: {"id": loc, "attributes": {"locale": loc, "whatsNew": "old", "description": "keep " + loc}}
                   for loc in self.manifest["locales"]}
        beta = {loc: {"id": "beta-" + loc, "attributes": {"locale": loc, "whatsNew": "old"}}
                for loc in self.manifest["locales"]}
        original = self.remote.run

        def run(argv, root):
            if argv[:3] == ["asc", "localizations", "list"]:
                return json.dumps({"data": list(locales.values())})
            if argv[:4] == ["asc", "builds", "test-notes", "list"]:
                return json.dumps({"data": list(beta.values())})
            if argv[:3] == ["asc", "localizations", "update"]:
                self.remote.writes.append(argv)
                target = argv[argv.index("--id") + 1]
                locales[target]["attributes"]["whatsNew"] = argv[argv.index("--whats-new") + 1]
                return "{}"
            if argv[:4] == ["asc", "builds", "test-notes", "update"]:
                self.remote.writes.append(argv)
                target = argv[argv.index("--localization-id") + 1].removeprefix("beta-")
                beta[target]["attributes"]["whatsNew"] = argv[argv.index("--whats-new") + 1]
                return "{}"
            return original(argv, root)

        self.remote.run = run
        self.assertEqual(self.sync(channels=["asc", "testflight", "github"])["status"], "verified")
        self.assertEqual(len(self.remote.writes), 6)
        self.assertEqual(locales["zh-Hans"]["attributes"]["whatsNew"], BODY)
        self.assertEqual(locales["en-US"]["attributes"]["whatsNew"], "Improved profile sharing.")
        self.assertIn("Verify copying.", beta["en-US"]["attributes"]["whatsNew"])
        self.assertIn("验证复制。", beta["zh-Hans"]["attributes"]["whatsNew"])
        self.assertEqual(locales["en-US"]["attributes"]["description"], "keep en-US")
        self.assertEqual(self.remote.github["body"], BODY + "\n")
        self.assertEqual(self.sync(channels=["asc", "testflight", "github"])["status"], "verified")
        self.assertEqual(len(self.remote.writes), 6)


if __name__ == "__main__":
    unittest.main()

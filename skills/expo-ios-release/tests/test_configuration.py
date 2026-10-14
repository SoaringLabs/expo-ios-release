"""Behavior coverage for independently configured applications."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import test_release

release = test_release.release


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_release.ReleaseTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def write_config(self, **changes):
        config = release.read_json(self.root / release.CONFIG_NAME)
        config.update(changes)
        release.atomic_json(self.root / release.CONFIG_NAME, config)

    def freeze(self):
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "configured candidate"], cwd=self.root, check=True, capture_output=True)
        self.fixture.pipeline().candidate()
        self.fixture.manifest = release.read_json(self.fixture.path)
        self.fixture.remote = test_release.Remote(self.fixture.manifest)
        self.fixture.accept_all()

    def test_two_apps_remain_independent_when_interleaved(self):
        other = test_release.ReleaseTest()
        other.setUp()
        self.addCleanup(other.doCleanups)
        config = release.read_json(other.root / release.CONFIG_NAME)
        config.update(app_id="1000000002", bundle_id="org.example.another",
                      project_id="22222222-2222-4222-8222-222222222222")
        release.atomic_json(other.root / release.CONFIG_NAME, config)
        app = release.read_json(other.root / "app.json")
        app["expo"]["ios"]["bundleIdentifier"] = config["bundle_id"]
        app["expo"]["extra"]["eas"]["projectId"] = config["project_id"]
        release.atomic_json(other.root / "app.json", app)
        eas = release.read_json(other.root / "eas.json")
        eas["submit"]["production"]["ios"]["ascAppId"] = config["app_id"]
        release.atomic_json(other.root / "eas.json", eas)
        other.path.unlink()
        release.prepare(other.root, "9.9.9", "HEAD", ["zh-Hans"])
        subprocess.run(["git", "add", "."], cwd=other.root, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "second app"], cwd=other.root, check=True, capture_output=True)
        other.pipeline().candidate()
        other.manifest = release.read_json(other.path)
        other.remote = test_release.Remote(other.manifest)
        other.accept_all()
        self.assertEqual(other.pipeline().run("review")["status"], "pending")
        self.assertEqual(self.fixture.pipeline().run("review")["status"], "pending")
        self.assertEqual(other.pipeline().run("review")["through"], "review")
        self.assertEqual(self.fixture.pipeline().run("review")["through"], "review")
        for fixture in (self.fixture, other):
            for argv in fixture.remote.calls:
                if argv[0] == "asc" and "--app" in argv:
                    self.assertEqual(argv[argv.index("--app") + 1], fixture.manifest["app_id"])
        self.assertNotEqual(release.state_path(other.root, "9.9.9"), release.state_path(self.root, "9.9.9"))

    def test_distinct_inherited_build_and_submission_profiles(self):
        self.write_config(build_profile="app-store", submit_profile="apple")
        eas = release.read_json(self.root / "eas.json")
        eas["build"]["app-store"] = {"extends": "production"}
        eas["submit"]["apple"] = {"extends": "production"}
        release.atomic_json(self.root / "eas.json", eas)
        manifest = release.read_json(self.fixture.path)
        manifest.update(profile="app-store", submit_profile="apple")
        release.atomic_json(self.fixture.path, manifest)
        self.freeze()
        self.fixture.pipeline().run("review")
        builds = [a for a in self.fixture.remote.writes if a[:2] == ["eas", "build"]]
        uploads = [a for a in self.fixture.remote.writes if a[:2] == ["eas", "submit"]]
        self.assertEqual(builds[0][builds[0].index("--profile") + 1], "app-store")
        self.assertEqual(uploads[0][uploads[0].index("--profile") + 1], "apple")

    def test_default_production_policy_requires_evidence_before_build(self):
        self.write_config(acceptance_policy={})
        manifest = release.read_json(self.fixture.path)
        manifest["acceptance_policy"] = {"production": "required", "device": "required"}
        release.atomic_json(self.fixture.path, manifest)
        self.freeze()
        with self.assertRaisesRegex(release.Blocked, "production acceptance"):
            self.fixture.pipeline().run("review")
        self.assertEqual(self.fixture.remote.writes, [])

    def test_required_device_acceptance_is_bound_to_exact_build(self):
        self.write_config(acceptance_policy={"production": "user-managed", "device": "required"})
        manifest = release.read_json(self.fixture.path)
        manifest["acceptance_policy"]["device"] = "required"
        release.atomic_json(self.fixture.path, manifest)
        self.freeze()
        self.fixture.pipeline().run("review")
        acceptance = release.read_json(self.fixture.directory / "acceptance.json")
        acceptance["device"] = {**acceptance["artifact"], "eas_build_id": "another-build"}
        release.atomic_json(self.fixture.directory / "acceptance.json", acceptance)
        with self.assertRaisesRegex(release.Blocked, "different EAS Build"):
            self.fixture.pipeline().run("review")
        self.assertEqual(self.fixture.count(["asc", "review", "submit"]), 0)

    def test_identity_change_cannot_retarget_an_existing_manifest(self):
        self.write_config(app_id="1000000002")
        with self.assertRaisesRegex(release.Blocked, "differs"):
            self.fixture.pipeline()
        self.assertEqual(self.fixture.remote.writes, [])

    def test_configured_checks_do_not_require_project_specific_scripts(self):
        self.assertEqual(release.checks(self.fixture.path, self.fixture.remote)["status"], "passed")
        self.assertFalse(any("check:motion" in a for a in self.fixture.remote.calls))

    def test_shell_command_strings_are_rejected(self):
        self.write_config(checks=["pnpm lint && echo success"])
        with self.assertRaisesRegex(release.Blocked, "argument arrays"):
            release.project_config(self.root)

    def test_traversal_and_easignore_negations_are_blocking(self):
        self.write_config(release_docs="../other/versions")
        with self.assertRaisesRegex(release.Blocked, "repository-relative"):
            release.project_config(self.root)
        self.write_config(release_docs="release-docs/versions")
        with (self.root / ".easignore").open("a") as stream:
            stream.write("!release-docs/shipped.ts\n")
        with self.assertRaisesRegex(release.Blocked, "negations"):
            release.configuration(self.root)

    def test_profile_inheritance_cycle_is_rejected(self):
        eas = release.read_json(self.root / "eas.json")
        eas["build"]["base"]["extends"] = "production"
        with self.assertRaisesRegex(release.Blocked, "circular"):
            release.resolve_profile(eas, "build", "production")

    def test_dynamic_config_uses_installed_local_expo_without_npx(self):
        (self.root / "app.config.ts").write_text("export default {};")
        binary = self.root / "node_modules/.bin/expo"
        binary.parent.mkdir(parents=True)
        binary.touch()
        expected = release.read_json(self.root / "app.json")["expo"]
        with patch.object(release.Runner, "run", return_value=json.dumps(expected)) as run:
            self.assertEqual(release.app_config(self.root), expected)
        self.assertEqual(run.call_args.args[0], [str(binary), "config", "--json"])

    def test_missing_dynamic_config_runtime_blocks_instead_of_using_stale_app_json(self):
        (self.root / "app.config.js").write_text("module.exports = {};")
        with self.assertRaisesRegex(release.Blocked, "dynamic app config"):
            release.configuration(self.root)

    def test_custom_document_directory_is_resolved_from_project_root(self):
        old = self.root / "release-docs"
        new = self.root / "shipping"
        old.rename(new)
        self.write_config(release_docs="shipping/versions")
        path = new / "versions/9.9.9/release.json"
        root, directory, manifest = release.load_manifest(path)
        self.assertEqual(root, self.root.resolve())
        self.assertEqual(directory, path.parent.resolve())
        self.assertEqual(manifest["app_id"], self.fixture.manifest["app_id"])

    def test_npm_project_does_not_need_pnpm_specific_commands(self):
        package = release.read_json(self.root / "package.json")
        package["packageManager"] = "npm@11.0.0"
        release.atomic_json(self.root / "package.json", package)
        original = self.fixture.remote.run
        def run(argv, root, timeout=120):
            if argv == ["npm", "--version"]:
                return "11.0.0"
            self.assertNotEqual(argv[0], "pnpm")
            return original(argv, root, timeout)
        release.toolcheck(self.root, type("Runner", (), {"run": staticmethod(run)})())

    def test_english_only_release_reaches_formal_review(self):
        manifest = release.read_json(self.fixture.path)
        manifest["locales"] = ["en-US"]
        release.atomic_json(self.fixture.path, manifest)
        self.fixture.manifest = manifest
        for path in (self.fixture.directory / "metadata/version/9.9.9").glob("*.json"):
            path.unlink()
        (self.fixture.directory / "release-notes.md").write_text("Improved search and calendar scrolling.")
        release.changelog.generate(self.fixture.directory, "9.9.9", ["en-US"], release.atomic_json)
        self.fixture.remote = test_release.Remote(manifest)
        self.fixture.accept_all()
        self.fixture.pipeline().run("review")
        result = self.fixture.pipeline().run("review")
        self.assertEqual(result["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.fixture.remote.locales, {"en-US": "Improved search and calendar scrolling."})

    def test_invalid_configuration_reports_blocked_without_traceback(self):
        for invalid in ([], {"schema": 1, "release_docs": None}):
            release.atomic_json(self.root / release.CONFIG_NAME, invalid)
            with self.assertRaises(release.Blocked):
                release.project_config(self.root)

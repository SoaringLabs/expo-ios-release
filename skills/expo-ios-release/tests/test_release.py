"""Behavior tests using temp Git checkouts and a fake remote, never real services."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class Remote:
    """Implements the installed CLI's JSON contracts, with fault injection."""
    def __init__(self, manifest):
        self.manifest = manifest
        self.calls = []
        self.writes = []
        self.build_status = "FINISHED"
        self.build_source = manifest["source_commit"]
        self.asc_auth = True
        self.apple = None
        self.submissions = []
        self.version = None
        self.locales = {}
        self.detail = {"id": "review-detail", "attributes": {"notes": "Earlier instructions", "demoAccountRequired": True}}
        self.fail = None
        self.review_state = "WAITING_FOR_REVIEW"
        self.readiness_failed = False
        self.validation_checks = []
        self.promoted_purchases = []
        self.built = 0

    def flag(self, argv, name):
        return argv[argv.index(name) + 1]

    def build(self, build_id="eas-build"):
        return {"id": build_id, "status": self.build_status, "appVersion": self.manifest["version"],
            "appBuildVersion": "36", "appIdentifier": self.manifest["bundle_id"], "buildProfile": self.manifest["profile"],
            "distribution": "STORE", "gitCommitHash": self.build_source,
            "app": {"id": self.manifest["project_id"]},
            "artifacts": {"url": "https://private.invalid/ipa?signature=SECRET"}, "token": "SECRET"}

    def apple_json(self):
        if not self.apple:
            return {"data": []}
        return {"data": [{"id": self.apple["id"], "attributes": {"version": "36", "processingState": self.apple["processingState"]},
            "relationships": {"app": {"data": {"id": self.manifest["app_id"]}}, "preReleaseVersion": {"data": {"id": "prerelease"}}}}],
            "included": [{"id": "prerelease", "attributes": {"version": self.manifest["version"], "platform": "IOS"}}]}

    def run(self, argv, root, timeout=120):
        self.calls.append(argv)
        if argv == ["eas", "--version"]:
            return "eas-cli/22.4.0 darwin-arm64 node-v24.15.0"
        if argv == ["asc", "version"]:
            return "5.14.1 (commit: unknown)"
        if argv == ["pnpm", "--version"]:
            return "12.4.1"
        if argv == ["node", "--version"]:
            return "v24.15.0"
        if argv[:3] == ["asc", "auth", "status"]:
            return json.dumps({"environmentCredentialsComplete": self.asc_auth})
        if argv[:3] == ["asc", "apps", "view"]:
            return json.dumps({"data": {"id": self.manifest["app_id"], "attributes": {"bundleId": self.manifest["bundle_id"]}}})
        if argv[:2] == ["eas", "build:view"]:
            return json.dumps(self.build(argv[2]))
        if argv[:3] == ["asc", "builds", "list"]:
            return json.dumps(self.apple_json())
        if argv[:2] == ["eas", "submit:list"]:
            return json.dumps(self.submissions)
        if argv[:2] == ["eas", "submit:view"]:
            return json.dumps(next(s for s in self.submissions if s["id"] == argv[2]))
        if argv[:3] == ["asc", "versions", "list"]:
            return json.dumps({"data": [self.version] if self.version else []})
        if argv[:3] == ["asc", "review", "details-for-version"]:
            return json.dumps({"data": self.detail})
        if argv[:3] == ["asc", "localizations", "list"]:
            return json.dumps({"data": [{"id": loc, "attributes": {"locale": loc, "whatsNew": text}} for loc, text in self.locales.items()]})
        if argv[:2] == ["asc", "validate"]:
            if self.readiness_failed:
                raise release.CommandFailure(1)
            return json.dumps({"ready": True, "checks": self.validation_checks})
        if argv[:3] == ["asc", "subscriptions", "promoted-purchases"]:
            return json.dumps({"data": self.promoted_purchases})
        if argv[:2] == ["eas", "submit:status"]:
            return json.dumps({"ios": {"ascAppIdentifier": self.manifest["app_id"], "live": {
                "versionString": "1.5.0", "buildNumber": "34", "state": "READY_FOR_DISTRIBUTION",
                "password": "SECRET", "url": "https://private.invalid/?signature=SECRET"}, "testFlightBuilds": []}, "secret": "SECRET"})
        if argv[:2] in (["pnpm", "exec"], ["pnpm", "lint"], ["node", "--test"]) or argv[0] == "pnpm" and argv[1].startswith("check:"):
            return "tests passed"
        self.writes.append(argv)
        if argv[:2] == ["eas", "build"]:
            self.built += 1
            answer = [self.build()]
            operation = "build"
        elif argv[:2] == ["eas", "submit"]:
            self.submissions.append({"id": f"eas-submission-{len(self.submissions) + 1}", "status": "FINISHED",
                "submittedBuild": self.build(), "iosConfig": {"ascAppIdentifier": self.manifest["app_id"]}, "createdAt": release.now()})
            self.apple = {"id": "apple-build", "processingState": "VALID"}
            answer = {}
            operation = "upload"
        elif argv[:3] == ["asc", "versions", "create"]:
            self.version = {"id": "apple-version", "attributes": {"versionString": self.manifest["version"],
                "platform": "IOS", "appVersionState": "PREPARE_FOR_SUBMISSION", "releaseType": self.flag(argv, "--release-type")},
                "relationships": {"build": {"data": None}}}
            answer = self.version
            operation = "stage:create"
        elif argv[:3] == ["asc", "metadata", "apply"]:
            directory = Path(self.flag(argv, "--dir")) / "version" / self.manifest["version"]
            self.locales = {p.stem: json.loads(p.read_text())["whatsNew"] for p in directory.glob("*.json")}
            answer = {}
            operation = "stage:metadata"
        elif argv[:3] == ["asc", "versions", "attach-build"]:
            self.version["relationships"]["build"]["data"] = {"id": self.flag(argv, "--build-id")}
            answer = {}
            operation = "stage:attach"
        elif argv[:3] == ["asc", "review", "details-update"]:
            self.detail["attributes"]["notes"] = self.flag(argv, "--notes")
            answer = {}
            operation = "stage:notes"
        elif argv[:3] == ["asc", "versions", "update"]:
            self.version["attributes"]["releaseType"] = self.flag(argv, "--release-type")
            if "--earliest-release-date" in argv:
                self.version["attributes"]["earliestReleaseDate"] = self.flag(argv, "--earliest-release-date")
            answer = {}
            operation = "stage:strategy"
        elif argv[:3] == ["asc", "review", "submit"]:
            self.version["attributes"]["appVersionState"] = self.review_state
            answer = {}
            operation = "review"
        else:
            raise AssertionError(f"Unsupported fake command: {argv}")
        if self.fail == operation:
            raise release.CommandFailure(1)  # Side effect happened, output lost.
        return json.dumps(answer)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="expo-ios-release-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        def write(name, data):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        write(".expo-ios-release.json", {"schema": 1, "app_id": "1000000001",
            "project_id": "11111111-1111-4111-8111-111111111111", "bundle_id": "com.example.demo",
            "build_profile": "production", "submit_profile": "production",
            "checks": [["pnpm", "exec", "tsc", "--noEmit"], ["pnpm", "lint"], ["node", "--test", "test/unit.mjs"]],
            "acceptance_policy": {"production": "user-managed", "device": "user-managed"}})
        write("app.json", {"expo": {"version": "9.9.9", "ios": {"bundleIdentifier": "com.example.demo"}, "extra": {"eas": {"projectId": "11111111-1111-4111-8111-111111111111"}}}})
        write("package.json", {"version": "9.9.9", "packageManager": "pnpm@12.4.1"})
        write("eas.json", {"cli": {"appVersionSource": "remote"}, "build": {"base": {"node": "24.15.0", "pnpm": "12.4.1"},
            "production": {"extends": "base", "environment": "production", "autoIncrement": True}}, "submit": {"production": {"ios": {"ascAppId": "1000000001"}}}})
        write(".gitignore", "/.artifacts\n")
        write(".easignore", "/.agents\n/.artifacts\n/release-docs\n/docs\n")
        write("source.ts", "export const value = 1;\n")
        write("test/unit.mjs", "// isolated fixture\n")
        write("release-docs/versions/9.9.9/README.md", "Confirmed fixture scope\n")
        for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(argv, cwd=self.root, check=True, capture_output=True)
        release.prepare(self.root, "9.9.9", "HEAD", ["zh-Hans"])
        self.directory = self.root / "release-docs/versions/9.9.9"
        self.path = self.directory / "release.json"
        self.manifest = release.read_json(self.path)
        (self.directory / "release-notes.md").write_text("【优化】\n1、提醒更可靠，日期选择更方便。\n")
        (self.directory / "review-notes.template.txt").write_text("Changes in this update:\n{{CHANGELOG}}\n\nCore flows work without sign-in. Use Apple sign-in to verify cloud sync.\n")
        for loc in self.manifest["locales"]:
            release.atomic_json(self.directory / f"metadata/version/9.9.9/{loc}.json", {"whatsNew": "Improved reminder reliability and date selection."})
        release.changelog.generate(self.directory, "9.9.9", ["zh-Hans"], release.atomic_json)
        self.remote = Remote(self.manifest)
        self.accept_all()

    def accept_all(self):
        report = self.directory / "qa.md"
        report.write_text("Actual results in this fixture: acceptance of build eas-build.\n")
        proof = [{"path": str(report.relative_to(self.root)), "sha256": release.digest(report)}]
        groups = {group: {"passed": True, "source_commit": self.manifest["source_commit"], "evidence": proof} for group in ("code", "artifact", "store")}
        for group in ("artifact", "store"):
            groups[group]["eas_build_id"] = "eas-build"
        groups["store"]["metadata_digest"] = release.inputs(self.directory, self.manifest)[2]
        release.atomic_json(self.directory / "acceptance.json", groups)

    def pipeline(self):
        return release.Pipeline(self.path, self.remote)

    def finish(self):
        first = self.pipeline().run("review")
        if first["status"] == "pending":
            first = self.pipeline().run("review")
        return first

    def count(self, prefix):
        return sum(argv[:len(prefix)] == prefix for argv in self.remote.writes)

    def test_full_release_and_resume_uploads_and_submits_once(self):
        result = self.finish()
        self.assertEqual(result["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.pipeline().run("review")["status"], "complete")
        self.assertEqual(self.count(["eas", "build"]), 1)
        self.assertEqual(self.count(["eas", "submit"]), 1)
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)
        self.assertNotIn("SECRET", self.pipeline().path.read_text())
        self.assertTrue(self.remote.detail["attributes"]["demoAccountRequired"])
        self.assertFalse(any("--demo-account-required" in args for args in self.remote.writes))

    def test_optional_image_warning_for_unpromoted_subscriptions_allows_review(self):
        self.remote.validation_checks = [{"id": "subscriptions.images.recommended", "severity": "warning"}]
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)

    def test_promoted_subscriptions_keep_image_warning_blocking(self):
        self.remote.validation_checks = [{"id": "subscriptions.images.recommended", "severity": "warning"}]
        self.remote.promoted_purchases = [{"id": "promoted-subscription"}]
        with self.assertRaisesRegex(release.Blocked, "Promoted subscriptions"):
            self.finish()
        self.assertEqual(self.count(["asc", "review", "submit"]), 0)

    def test_other_store_warnings_and_errors_block_review(self):
        for check in ({"id": "metadata.keywords.locale_duplicates", "severity": "warning"},
                      {"id": "subscriptions.images.recommended", "severity": "error"}):
            with self.subTest(check=check):
                self.remote.validation_checks = [check]
                with self.assertRaisesRegex(release.Blocked, "readiness"):
                    self.finish()
                self.assertEqual(self.count(["asc", "review", "submit"]), 0)

    def test_default_run_and_plan_make_zero_service_tool_calls(self):
        original = release.Runner.run
        def local_git_only(instance, argv, root, timeout=120):
            if argv[0] != "git":
                raise AssertionError("Service tool used in offline plan")
            return original(instance, argv, root, timeout)
        with patch.object(release.Runner, "run", local_git_only):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(release.main(["run", "--manifest", str(self.path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["remote_calls"], 0)

    def test_build_only_needs_neither_asc_auth_nor_notes(self):
        self.remote.asc_auth = False
        (self.directory / "review-notes.txt").write_text("TBD")
        self.assertEqual(self.pipeline().run("build")["through"], "build")
        self.assertFalse(any(args[0] == "asc" for args in self.remote.calls))

    def test_missing_asc_auth_blocks_full_release_before_build(self):
        self.remote.asc_auth = False
        with self.assertRaisesRegex(release.Blocked, "authentication"):
            self.pipeline().run("review")
        self.assertFalse(self.remote.writes)

    def test_full_release_needs_no_production_environment_acceptance(self):
        acceptance = release.read_json(self.directory / "acceptance.json")
        self.assertNotIn("production", acceptance)
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "build"]), 1)
        self.assertEqual(self.count(["eas", "submit"]), 1)

    def test_historical_failed_production_acceptance_does_not_block_release(self):
        acceptance = release.read_json(self.directory / "acceptance.json")
        acceptance["production"] = {"passed": False, "source_commit": "old-source"}
        release.atomic_json(self.directory / "acceptance.json", acceptance)
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "build"]), 1)

    def test_full_release_needs_no_device_acceptance(self):
        acceptance = release.read_json(self.directory / "acceptance.json")
        self.assertNotIn("device", acceptance)
        self.assertNotIn("production", acceptance)
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "build"]), 1)
        self.assertEqual(self.count(["eas", "submit"]), 1)
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)

    def test_historical_failed_device_acceptance_does_not_block_release(self):
        acceptance = release.read_json(self.directory / "acceptance.json")
        acceptance["device"] = {"passed": False, "source_commit": "old-source", "eas_build_id": "old-build"}
        release.atomic_json(self.directory / "acceptance.json", acceptance)
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)

    def test_remaining_acceptance_gates_still_block_their_operations(self):
        for group, forbidden in (("code", ["eas", "build"]),
                                 ("artifact", ["eas", "submit"]),
                                 ("store", ["asc", "review", "submit"])):
            with self.subTest(group=group):
                checkpoint = self.pipeline().path
                if checkpoint.exists():
                    checkpoint.unlink()
                self.remote = Remote(self.manifest)
                self.accept_all()
                acceptance = release.read_json(self.directory / "acceptance.json")
                acceptance[group]["passed"] = False
                release.atomic_json(self.directory / "acceptance.json", acceptance)
                with self.assertRaisesRegex(release.Blocked, f"{group} acceptance"):
                    self.finish()
                self.assertEqual(self.count(forbidden), 0)

    def test_prepare_preserves_existing_files_and_duplicate_manifest(self):
        self.path.unlink()
        metadata = self.directory / "metadata/version/9.9.9/zh-Hans.json"
        acceptance = self.directory / "acceptance.json"
        before = (metadata.read_bytes(), acceptance.read_bytes())
        release.prepare(self.root, "9.9.9", "HEAD", ["zh-Hans", "en-US"])
        self.assertEqual(before, (metadata.read_bytes(), acceptance.read_bytes()))
        with self.assertRaisesRegex(release.Blocked, "already exists"):
            release.prepare(self.root, "9.9.9", "HEAD", ["zh-Hans"])

    def test_pending_build_is_not_rebuilt(self):
        self.remote.build_status = "IN_PROGRESS"
        self.assertEqual(self.pipeline().run("review")["phase"], "build")
        self.assertEqual(self.pipeline().run("review")["phase"], "build")
        self.assertEqual(self.count(["eas", "build"]), 1)

    def test_unknown_build_dispatch_requires_exact_adoption(self):
        self.remote.fail = "build"
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        with self.assertRaisesRegex(release.Uncertain, "adopt"):
            self.pipeline().run("review")
        self.remote.fail = None
        self.pipeline().adopt("eas-build")
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "build"]), 1)

    def test_upload_timeout_reconciles_without_duplicate_upload(self):
        self.remote.fail = "upload"
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        self.remote.fail = None
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "submit"]), 1)

    def test_unknown_absent_upload_is_never_replayed(self):
        self.remote.fail = "upload"
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        self.remote.apple = None
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        with self.assertRaises(release.Uncertain):
            self.pipeline().retry_upload()
        self.assertEqual(self.count(["eas", "submit"]), 1)

    def test_review_timeout_reconciles_accepted_state(self):
        self.pipeline().run("review")
        self.remote.fail = "review"
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        self.remote.fail = None
        self.assertEqual(self.pipeline().run("review")["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)

    def test_review_draft_does_not_count_as_submitted(self):
        self.remote.review_state = "READY_FOR_REVIEW"
        self.pipeline().run("review")
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        with self.assertRaises(release.Uncertain):
            self.pipeline().run("review")
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)

    def test_each_uncertain_stage_write_can_reconcile(self):
        for operation in ("stage:create", "stage:metadata", "stage:attach", "stage:notes", "stage:strategy"):
            with self.subTest(operation=operation):
                pipeline = self.pipeline()
                if pipeline.path.exists():
                    pipeline.path.unlink()
                self.remote = Remote(self.manifest)
                self.remote.apple = {"id": "apple-build", "processingState": "VALID"}
                self.manifest["release_type"] = "AFTER_APPROVAL" if operation == "stage:strategy" else "MANUAL"
                release.atomic_json(self.path, self.manifest)
                self.remote.manifest = self.manifest
                self.accept_all()
                # Start with a different strategy when checking its write.
                if operation == "stage:strategy":
                    self.remote.version = {"id": "apple-version", "attributes": {"versionString": "9.9.9", "platform": "IOS",
                        "appVersionState": "PREPARE_FOR_SUBMISSION", "releaseType": "MANUAL"}, "relationships": {"build": {"data": None}}}
                self.remote.fail = operation
                with self.assertRaises(release.Uncertain):
                    self.pipeline().run("review")
                writes = len(self.remote.writes)
                self.remote.fail = None
                self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
                self.assertGreaterEqual(len(self.remote.writes), writes)

    def test_changed_evidence_and_metadata_block_submission(self):
        (self.directory / "qa.md").write_text("modified evidence")
        with self.assertRaisesRegex(release.Blocked, "evidence"):
            self.pipeline().run("review")
        self.assertFalse(self.remote.writes)
        self.accept_all()
        self.pipeline().run("stage")  # first call may return upload pending
        self.pipeline().run("stage")
        path = self.directory / "metadata/version/9.9.9/zh-Hans.json"
        release.atomic_json(path, {"whatsNew": "New copy after acceptance."})
        with self.assertRaisesRegex(release.Blocked, "Changelog copies differ|Store acceptance is stale"):
            self.pipeline().run("review")
        self.assertEqual(self.count(["asc", "review", "submit"]), 0)

    def test_wrong_source_or_build_profile_blocks_upload(self):
        self.remote.build_source = "a" * 40
        with self.assertRaisesRegex(release.Blocked, "does not match"):
            self.pipeline().run("review")
        self.assertEqual(self.count(["eas", "submit"]), 0)

    def test_uncommitted_shipped_files_block_all_mutations(self):
        (self.root / "source.ts").write_text("changed")
        with self.assertRaisesRegex(release.Blocked, "uncommitted"):
            self.pipeline().run("review")
        self.assertFalse(self.remote.writes)

    def test_failed_upload_can_retry_same_build_after_known_failure(self):
        self.pipeline().run("review")
        self.remote.apple = None
        self.remote.submissions[0]["status"] = "ERRORED"
        with self.assertRaisesRegex(release.Blocked, "upload failed"):
            self.pipeline().run("review")
        self.assertEqual(self.pipeline().retry_upload()["status"], "retry-prepared")
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "build"]), 1)
        self.assertEqual(self.count(["eas", "submit"]), 2)
        self.assertTrue(list((self.pipeline().path.parent / "history").glob("*.json")))

    def test_retry_refuses_a_running_or_successful_submission(self):
        self.pipeline().run("review")
        with self.assertRaisesRegex(release.Blocked, "verified failed"):
            self.pipeline().retry_upload()
        self.assertEqual(self.count(["eas", "submit"]), 1)

    def test_new_candidate_supports_same_marketing_version_after_fix(self):
        self.remote.build_status = "ERRORED"
        with self.assertRaises(release.Blocked):
            self.pipeline().run("review")
        (self.root / "source.ts").write_text("export const value = 2;\n")
        for argv in (["git", "add", "source.ts"], ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fix"]):
            subprocess.run(argv, cwd=self.root, check=True, capture_output=True)
        result = self.pipeline().candidate()
        self.manifest = release.read_json(self.path)
        self.remote.manifest = self.manifest
        self.remote.build_source = result["source_commit"]
        self.remote.build_status = "FINISHED"
        self.assertEqual(release.read_json(self.directory / "acceptance.json"), {})
        self.accept_all()
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["eas", "build"]), 2)

    def test_new_candidate_refuses_active_review(self):
        self.finish()
        with self.assertRaisesRegex(release.Blocked, "active"):
            self.pipeline().candidate()

    def test_status_never_returns_signed_links_or_secrets(self):
        result = release.status(self.root, self.remote)
        self.assertEqual(result["live"]["versionString"], "1.5.0")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(self.remote.writes)

    def test_existing_eas_upload_is_reused_before_apple_visibility(self):
        self.pipeline().run("build")
        self.remote.submissions = [{"id": "existing-submission", "status": "IN_PROGRESS", "submittedBuild": self.remote.build(),
                                   "iosConfig": {"ascAppIdentifier": "1000000001"}}]
        self.assertEqual(self.pipeline().run("upload")["submission_id"], "existing-submission")
        self.assertEqual(self.count(["eas", "submit"]), 0)

    def test_checks_record_actual_exit_statuses(self):
        result = release.checks(self.path, self.remote)
        self.assertEqual(result["status"], "passed")
        acceptance = release.read_json(self.directory / "acceptance.json")
        self.assertEqual(len(acceptance["code"]["evidence"]), len(release.project_config(self.root)["checks"]))
        release.evidence(self.root, acceptance, "code", self.manifest)
        self.assertFalse(self.remote.writes)

    def test_doctor_reports_missing_pinned_app_instead_of_ready(self):
        config = release.read_json(self.root / "eas.json")
        config["submit"]["production"]["ios"] = {}
        release.atomic_json(self.root / "eas.json", config)
        result = release.doctor(self.root, self.remote)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("Pin" in issue for issue in result["issues"]))
        self.assertFalse(self.remote.writes)

    def test_rejected_version_requires_feedback_evidence_before_resubmitting(self):
        self.finish()
        self.remote.version["attributes"]["appVersionState"] = "REJECTED"
        with self.assertRaisesRegex(release.Blocked, "review_feedback"):
            self.pipeline().run("review")
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)
        acceptance = release.read_json(self.directory / "acceptance.json")
        acceptance["review_feedback"] = {**acceptance["store"], "version_id": "apple-version",
            "resolution_summary": "Resolved reviewer access and verified the corrected instructions."}
        release.atomic_json(self.directory / "acceptance.json", acceptance)
        result = self.pipeline().run("review")
        self.assertEqual(result["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.count(["asc", "review", "submit"]), 2)

    def test_stale_feedback_evidence_blocks_metadata_rejected_resubmission(self):
        self.finish()
        self.remote.version["attributes"]["appVersionState"] = "METADATA_REJECTED"
        acceptance = release.read_json(self.directory / "acceptance.json")
        acceptance["review_feedback"] = {**acceptance["store"], "metadata_digest": "old", "version_id": "apple-version",
            "resolution_summary": "Corrected the localized release notes and verified the change."}
        release.atomic_json(self.directory / "acceptance.json", acceptance)
        with self.assertRaisesRegex(release.Blocked, "feedback evidence is stale"):
            self.pipeline().run("review")
        self.assertEqual(self.count(["asc", "review", "submit"]), 1)

    def test_wrong_asc_app_access_blocks_before_any_build(self):
        original = self.remote.run
        def wrong_app(argv, root, timeout=120):
            if argv[:3] == ["asc", "apps", "view"]:
                return json.dumps({"data": {"id": "other-app", "attributes": {"bundleId": "com.example.demo"}}})
            return original(argv, root, timeout)
        self.remote.run = wrong_app
        with self.assertRaisesRegex(release.Blocked, "app identity"):
            self.pipeline().run("review")
        self.assertFalse(self.remote.writes)

    def test_scheduled_release_requires_timezone_and_verifies_strategy(self):
        self.manifest.update(release_type="SCHEDULED", earliest_release_date="2026-11-01T09:00:00")
        release.atomic_json(self.path, self.manifest)
        with self.assertRaisesRegex(release.Blocked, "timezone"):
            self.pipeline()
        self.manifest["earliest_release_date"] += "+08:00"
        release.atomic_json(self.path, self.manifest)
        self.accept_all()
        self.assertEqual(self.finish()["apple_state"], "WAITING_FOR_REVIEW")
        self.assertEqual(self.remote.version["attributes"]["earliestReleaseDate"], self.manifest["earliest_release_date"])

    def test_failed_quality_command_cannot_be_recorded_as_passed(self):
        original = self.remote.run
        def fail_lint(argv, root, timeout=120):
            if argv == ["pnpm", "lint"]:
                raise release.CommandFailure(1)
            return original(argv, root, timeout)
        self.remote.run = fail_lint
        result = release.checks(self.path, self.remote)
        self.assertEqual(result["status"], "blocked")
        with self.assertRaisesRegex(release.Blocked, "code acceptance"):
            self.pipeline().run("review")
        self.assertFalse(self.remote.writes)

    def test_apple_processing_waits_without_another_upload(self):
        self.pipeline().run("build")
        self.remote.apple = {"id": "apple-build", "processingState": "PROCESSING"}
        self.assertEqual(self.pipeline().run("upload")["phase"], "apple-processing")
        self.remote.apple["processingState"] = "VALID"
        self.assertEqual(self.pipeline().run("upload")["through"], "upload")
        self.assertEqual(self.count(["eas", "submit"]), 0)


if __name__ == "__main__":
    unittest.main()

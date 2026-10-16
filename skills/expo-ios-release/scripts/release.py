#!/usr/bin/env python3
"""Expo iOS release helpers. Planning is offline; mutations require --execute."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

_notes_spec = importlib.util.spec_from_file_location("expo_release_changelog", Path(__file__).with_name("changelog.py"))
changelog = importlib.util.module_from_spec(_notes_spec)
_notes_spec.loader.exec_module(changelog)

_github_spec = importlib.util.spec_from_file_location("expo_release_github", Path(__file__).with_name("github_release.py"))
github_release = importlib.util.module_from_spec(_github_spec)
_github_spec.loader.exec_module(github_release)

CONFIG_NAME = ".expo-ios-release.json"
TOOLS = {"eas": "22.4.0", "asc": "5.14.1"}
STEPS = ["build", "upload", "stage", "review", "github"]
SUBMITTED = {"WAITING_FOR_REVIEW", "IN_REVIEW", "ACCEPTED", "PENDING_DEVELOPER_RELEASE",
             "PENDING_APPLE_RELEASE", "PROCESSING_FOR_DISTRIBUTION",
             "READY_FOR_DISTRIBUTION", "READY_FOR_SALE"}
CONFIG_FILES = (CONFIG_NAME, "app.json", "app.config.ts", "app.config.js", "package.json", "eas.json",
                "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", ".easignore")



class Blocked(Exception):
    pass


class Uncertain(Blocked):
    pass


CommandFailure = github_release.CommandFailure


class Runner:
    def run(self, argv, root, timeout=120):
        env = {**os.environ, "DO_NOT_TRACK": "1", "CI": "1"}
        if argv[:2] == ["eas", "build"]:
            # Archive upload and native fingerprinting happen before a Build ID exists.
            timeout = max(timeout, 900)
        result = subprocess.run(argv, cwd=root, env=env, capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise CommandFailure(result.returncode, result.stdout, result.stderr)
        return result.stdout.strip()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".release-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def records(response):
    if isinstance(response, list):
        return response
    data = response.get("data", [])
    return data if isinstance(data, list) else [data] if data else []


def attrs(record):
    return record.get("attributes", record)


def linked(record, name):
    data = record.get("relationships", {}).get(name, {}).get("data")
    return data.get("id") if isinstance(data, dict) else None


def command_json(runner, argv, root):
    return json.loads(runner.run(argv, root))


def git(root, *args):
    return Runner().run(["git", *args], root)


def source(root):
    head = git(root, "rev-parse", "HEAD")
    hashes = {name: digest(root / name) for name in CONFIG_FILES if (root / name).exists()}
    return {"commit": head, "config_digest": hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def project_config(root):
    root = Path(root).resolve()
    if not (root / CONFIG_NAME).is_file():
        raise Blocked(f"Configure {CONFIG_NAME} in the app repository first")
    data = read_json(root / CONFIG_NAME)
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise Blocked("Unsupported project configuration schema")
    for field in ("app_id", "project_id", "bundle_id", "build_profile", "submit_profile"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise Blocked(f"Configure {field} in {CONFIG_NAME}")
    if not re.fullmatch(r"[0-9]+", data["app_id"]):
        raise Blocked("app_id must be the numeric ASC app ID")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", data["project_id"]):
        raise Blocked("project_id must be the EAS project UUID")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", data["bundle_id"]):
        raise Blocked("Configure a valid iOS bundle identifier")
    data.setdefault("release_docs", "release-docs/versions")
    data.setdefault("artifact_dir", ".artifacts")
    for field in ("release_docs", "artifact_dir"):
        if not isinstance(data[field], str):
            raise Blocked(f"{field} must be a repository-relative directory")
        value = Path(data[field])
        if value.is_absolute() or ".." in value.parts or not value.parts or value.parts[0] == ".git":
            raise Blocked(f"{field} must be a repository-relative directory")
        if not (root / value).resolve().is_relative_to(root):
            raise Blocked(f"{field} resolves outside the repository")
    adapter = data.get("notes_adapter")
    if adapter is not None and (not isinstance(adapter, list) or not adapter or
                                any(not isinstance(arg, str) or not arg for arg in adapter)):
        raise Blocked("notes_adapter must be a nonempty command argument array")
    commands = data.get("checks")
    if not isinstance(commands, list) or not commands or any(
            not isinstance(cmd, list) or not cmd or any(not isinstance(arg, str) or not arg for arg in cmd)
            for cmd in commands):
        raise Blocked("Configure a nonempty checks array of command argument arrays")
    policy = data.setdefault("acceptance_policy", {})
    if not isinstance(policy, dict) or set(policy) - {"production", "device"}:
        raise Blocked("Invalid acceptance policy")
    for group in ("production", "device"):
        policy.setdefault(group, "required")
        if policy[group] not in ("required", "user-managed"):
            raise Blocked(f"Invalid {group} acceptance policy")
    versions = data.setdefault("tool_versions", dict(TOOLS))
    if not isinstance(versions, dict) or set(versions) != set(TOOLS) or any(
            not isinstance(v, str) or not re.fullmatch(r"\d+\.\d+\.\d+", v) for v in versions.values()):
        raise Blocked("Pin eas and asc tool_versions as x.y.z")
    return data


def archive_exclusions(root, config):
    # Only anchored whole-directory exclusions are accepted as proof. Negations
    # could reinclude shipped files, so require explicit reconciliation first.
    lines = (root / ".easignore").read_text().splitlines()
    if any(line.strip().startswith("!") for line in lines):
        raise Blocked("Reconcile .easignore negations before relying on archive exclusions")
    required = {config["release_docs"].split("/")[0], config["artifact_dir"], ".agents"}
    if any(not any(line.strip().rstrip("/") == "/" + name for line in lines) for name in required):
        raise Blocked("Exclude release docs, .agents and artifact_dir from the EAS archive")
    return tuple(name + "/" for name in required)


def source_clean(root):
    ignored = archive_exclusions(root, project_config(root))
    raw = subprocess.check_output(["git", "status", "--porcelain", "-z"], cwd=root)
    entries = raw.decode().split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        paths = [entry[3:]]
        if "R" in entry[:2] or "C" in entry[:2]:
            paths.append(entries[index])
            index += 1
        if any(not p.startswith(ignored) for p in paths):
            return False
    return True


def load_manifest(path):
    path = Path(path).resolve()
    root = next((parent for parent in path.parents if (parent / CONFIG_NAME).is_file()), None)
    if root is None:
        raise Blocked(f"Find {CONFIG_NAME} in the app repository before loading a release")
    config = project_config(root)
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise Blocked("Release manifest must be a JSON object")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")):
        raise Blocked("Use a numeric x.y.z release version")
    expected = root / config["release_docs"] / manifest["version"] / "release.json"
    if path != expected.resolve():
        raise Blocked("Manifest must be release_docs/VERSION/release.json")
    if manifest.get("schema") != 2:
        raise Blocked("Unsupported release manifest schema; migrate old releases explicitly")
    for field in ("app_id", "project_id", "bundle_id", "submit_profile"):
        if manifest.get(field) != config[field]:
            raise Blocked("Manifest app/project/profile differs from project configuration")
    if manifest.get("profile") != config["build_profile"] or manifest.get("acceptance_policy") != config["acceptance_policy"]:
        raise Blocked("Manifest profile/acceptance policy differs from project configuration")
    release_type = manifest.get("release_type")
    if release_type not in ("MANUAL", "AFTER_APPROVAL", "SCHEDULED"):
        raise Blocked("Invalid release strategy")
    if release_type == "SCHEDULED":
        try:
            date = dt.datetime.fromisoformat(manifest["earliest_release_date"].replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError()
        except (KeyError, ValueError):
            raise Blocked("Scheduled release needs an ISO date with timezone")
    locales = manifest.get("locales", [])
    if not isinstance(locales, list) or not locales or len(set(locales)) != len(locales) or any(
            not isinstance(loc, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", loc) for loc in locales):
        raise Blocked("Specify unique actual ASC locales")
    return root, path.parent, manifest


def text_ready(text):
    return bool(text.strip()) and len(text) <= 4000 and not re.search(
        r"\b(?:TODO|TBD|FIXME)\b|Lorem ipsum|待填写|<[^>]+>", text, re.I)


def inputs(directory, manifest, config=None, root=None):
    try:
        changelog.check_local(directory, manifest["version"], manifest["locales"], config, root)
    except changelog.NotesError as error:
        raise Blocked(str(error)) from error
    notes = (directory / "review-notes.txt").read_text().strip()
    if not text_ready(notes):
        raise Blocked("Reviewer notes are empty, placeholders, or over 4000 characters")
    localizations = {}
    for loc in manifest["locales"]:
        path = directory / "metadata" / "version" / manifest["version"] / f"{loc}.json"
        value = read_json(path)
        if set(value) != {"whatsNew"} or not isinstance(value["whatsNew"], str) or not text_ready(value["whatsNew"]):
            raise Blocked(f"Provide concrete whatsNew only for {loc}")
        localizations[loc] = value["whatsNew"]
    expected_files = {f"{loc}.json" for loc in manifest["locales"]}
    metadata = directory / "metadata"
    files = {str(p.relative_to(metadata)) for p in metadata.rglob("*") if p.is_file()}
    expected_paths = {f"version/{manifest['version']}/{f}" for f in expected_files}
    if files != expected_paths:
        raise Blocked("Metadata directory must contain only this version's whatsNew files")
    fingerprint = hashlib.sha256(json.dumps({"notes": notes, "localizations": localizations,
        "release_type": manifest["release_type"], "date": manifest.get("earliest_release_date")},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return notes, localizations, fingerprint


def evidence(root, acceptance, group, manifest, build=None):
    root = Path(root).resolve()
    item = acceptance.get(group, {})
    if item.get("passed") is not True or item.get("source_commit") != manifest["source_commit"]:
        raise Blocked(f"Missing valid {group} acceptance for this source")
    if build and item.get("eas_build_id") != build["id"]:
        raise Blocked(f"{group} acceptance is for a different EAS Build")
    references = item.get("evidence", [])
    if not references:
        raise Blocked(f"{group} needs concrete evidence files")
    for ref in references:
        path = (root / ref["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != ref["sha256"]:
            raise Blocked(f"{group} evidence missing, outside repository, or changed")


def resolve_profile(eas, section, name):
    profiles = eas.get(section, {})
    def visit(current, seen):
        if current in seen or current not in profiles:
            raise Blocked("Missing or circular EAS profile inheritance")
        profile = profiles[current]
        parent = visit(profile["extends"], seen | {current}) if profile.get("extends") else {}
        combined = {**parent, **profile}
        for platform in ("ios", "android"):
            if platform in parent or platform in profile:
                combined[platform] = {**parent.get(platform, {}), **profile.get(platform, {})}
        return combined
    return visit(name, set())


def app_config(root):
    if any((root / name).is_file() for name in ("app.config.ts", "app.config.js")):
        binary = root / "node_modules/.bin/expo"
        if not binary.is_file():
            raise Blocked("Install the project's Expo CLI to resolve dynamic app config locally")
        app = command_json(Runner(), [str(binary), "config", "--json"], root)
        return app.get("expo", app)
    return read_json(root / "app.json")["expo"]


def configuration(root, version=None):
    config = project_config(root)
    package = read_json(root / "package.json")
    app = app_config(root)
    eas = read_json(root / "eas.json")
    if package["version"] != app["version"] or version is not None and package["version"] != version:
        raise Blocked("App/package version differs from release manifest")
    if app["ios"]["bundleIdentifier"] != config["bundle_id"] or app["extra"]["eas"]["projectId"] != config["project_id"]:
        raise Blocked("Local app/project identity differs")
    profile = resolve_profile(eas, "build", config["build_profile"])
    if profile.get("environment") != "production" or profile.get("distribution", "store") != "store" or profile.get("developmentClient"):
        raise Blocked("Build profile must use the production environment and store distribution")
    increment = profile.get("ios", {}).get("autoIncrement", profile.get("autoIncrement"))
    if increment not in (True, "buildNumber") or eas.get("cli", {}).get("appVersionSource") != "remote":
        raise Blocked("Keep remote EAS iOS build-number auto-increment")
    submit = resolve_profile(eas, "submit", config["submit_profile"])
    if str(submit["ios"].get("ascAppId")) != config["app_id"]:
        raise Blocked("Pin the existing ASC app ID before executing")
    archive_exclusions(root, config)
    return config


def local_preflight(root, manifest):
    configuration(root, manifest["version"])
    current = source(root)
    if current["commit"] != manifest["source_commit"] or not source_clean(root):
        raise Blocked("Candidate source changed or contains uncommitted shipped files")
    return current


def toolcheck(root, runner, require_asc=True):
    config = project_config(root)
    for tool, version in config["tool_versions"].items():
        if tool == "asc" and not require_asc:
            continue
        output = runner.run([tool, "--version" if tool == "eas" else "version"], root)
        actual = re.search(r"(?:eas-cli/)?(\d+\.\d+\.\d+)", output)
        if not actual or actual[1] != version:
            raise Blocked(f"Require configured {tool} {version}")
    package = read_json(root / "package.json")
    eas = read_json(root / "eas.json")
    manager = package.get("packageManager", "").split("@", 1)
    if len(manager) != 2 or manager[0] not in {"pnpm", "npm", "yarn", "bun"}:
        raise Blocked("Pin a supported packageManager in package.json")
    pm, version = manager[0], manager[1].split("+", 1)[0]
    profile = resolve_profile(eas, "build", config["build_profile"])
    if runner.run([pm, "--version"], root) != version or profile.get(pm, version) != version:
        raise Blocked("Local/package/EAS package manager versions must agree")
    if profile.get("node") and runner.run(["node", "--version"], root).lstrip("v") != profile["node"]:
        raise Blocked("Local and EAS Node versions must agree")


def asc_auth(root, runner):
    result = command_json(runner, ["asc", "auth", "status", "--output", "json"], root)
    return bool(result.get("credentials") or result.get("environmentCredentialsComplete"))


def safe_build(record):
    result = {k: record.get(k) for k in ("id", "status", "appVersion", "appBuildVersion",
        "appIdentifier", "buildProfile", "distribution", "gitCommitHash")}
    result["project_id"] = (record.get("app") or {}).get("id")
    return result


def verify_build(build, manifest):
    expected = {"appVersion": manifest["version"], "appIdentifier": manifest["bundle_id"],
                "buildProfile": manifest["profile"], "distribution": "STORE",
                "gitCommitHash": manifest["source_commit"]}
    if any(build.get(k) != v for k, v in expected.items()) or (build.get("app") or {}).get("id") != manifest["project_id"]:
        raise Blocked("EAS Build source/profile/version/app does not match the candidate")
    if not build.get("appBuildVersion"):
        raise Blocked("EAS Build lacks a build number")


def state_path(root, version):
    private = Path(git(root, "rev-parse", "--absolute-git-dir"))
    return private / "expo-ios-release" / version / "state.json"


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("Another release process is active in this checkout")
        yield


class Pipeline:
    def __init__(self, path, runner=None):
        self.root, self.directory, self.manifest = load_manifest(path)
        self.config = project_config(self.root)
        self.runner = runner or Runner()
        self.path = state_path(self.root, self.manifest["version"])
        self.state = read_json(self.path) if self.path.exists() else {"schema": 1}

    def call(self, *args):
        return command_json(self.runner, list(args), self.root)

    def save(self):
        atomic_json(self.path, self.state)

    def publisher(self):
        return github_release.Publisher(self, {"atomic_json": atomic_json, "now": now, "notes": changelog})

    def github_evidence(self):
        """Verify a historical candidate without evaluating the current HEAD config."""
        if self.state.get("pending"):
            raise Uncertain("Reconcile the pending Apple write before GitHub publication")
        if (self.state.get("source", {}).get("commit") != self.manifest["source_commit"] or
                not all(self.state.get(k) for k in ("build_id", "version_id", "apple_build"))):
            raise Blocked("Restore exact source/build/version checkpoints before GitHub publication")
        config = project_config(self.root)
        for tool in ("eas", "asc"):
            output = self.runner.run([tool, "--version" if tool == "eas" else "version"], self.root)
            actual = re.search(r"(?:eas-cli/)?(\d+\.\d+\.\d+)", output)
            if not actual or actual[1] != config["tool_versions"][tool]:
                raise Blocked(f"Require configured {tool} version for publication readback")
        if not asc_auth(self.root, self.runner):
            raise Blocked("Independent ASC authentication is missing")
        apps = records(self.call("asc", "apps", "view", "--id", self.manifest["app_id"], "--output", "json"))
        if len(apps) != 1 or apps[0].get("id") != self.manifest["app_id"] or attrs(apps[0]).get("bundleId") != self.manifest["bundle_id"]:
            raise Blocked("Authenticated ASC app identity is unverified")
        build = self.call("eas", "build:view", self.state["build_id"], "--json")
        verify_build(build, self.manifest)
        if build.get("status") != "FINISHED":
            raise Blocked("GitHub publication requires the finished exact EAS Build")
        apple, version = self.apple_build(build), self.version()
        if (not apple or apple != self.state["apple_build"] or apple["processingState"] != "VALID" or
                not version or version["id"] != self.state["version_id"] or linked(version, "build") != apple["id"] or
                self.version_state(version) not in SUBMITTED):
            raise Blocked("GitHub publication requires the exact formally submitted Apple version/build")
        acceptance = read_json(self.directory / "acceptance.json")
        evidence(self.root, acceptance, "code", self.manifest)
        for group in ("artifact", "store"):
            evidence(self.root, acceptance, group, self.manifest, build)
        for group in ("production", "device"):
            if self.manifest["acceptance_policy"][group] == "required":
                evidence(self.root, acceptance, group, self.manifest, build if group == "device" else None)
        notes, localizations, fingerprint = inputs(self.directory, self.manifest, self.config, self.root)
        if acceptance["store"].get("metadata_digest") != fingerprint or not self.stage_matches(version, apple, notes, localizations):
            raise Blocked("Store acceptance or submitted metadata differs from the publication inputs")
        return self.version_state(version)

    def github(self):
        with locked(self.path):
            self.state = read_json(self.path) if self.path.exists() else {"schema": 1}
            return self._github_locked()

    def _github_locked(self):
        github_release.guard_writes(self.path.parent, allow="github")
        apple_state = self.github_evidence()
        self.state["requested_through"] = "github"
        self.save()
        published = self.publisher().publish()
        return {"status": "complete", "through": "github", "requested_through": "github", "phase": "github", "apple_state": apple_state,
            "version_id": self.state["version_id"], "github": published}

    def run(self, through):
        with locked(self.path):
            self.state = read_json(self.path) if self.path.exists() else {"schema": 1}
            if through == "github" and not self.state.get("pending") and (
                    self.state.get("receipts", {}).get("review") or self.path.with_name("github.json").exists()):
                return self._github_locked()
            github_release.guard_writes(self.path.parent, allow="state")
            self.state["requested_through"] = through
            self.save()
            if through == "github":
                github_release.Client(self.root, self.runner).preflight()
            result = self._run_apple_locked("review" if through == "github" else through)
            if through == "github" and result.get("status") == "complete":
                return self._github_locked()
            return {**result, "requested_through": through}

    def mutate(self, operation, argv):
        self.state["pending"] = {"operation": operation, "started_at": now()}
        self.save()
        try:
            output = self.runner.run(argv, self.root)
        except (CommandFailure, subprocess.TimeoutExpired, OSError):
            raise Uncertain(f"{operation} outcome unknown; reconcile remote state before retry")
        return output

    def done(self, operation):
        self.state.pop("pending", None)
        self.state.setdefault("receipts", {})[operation] = {"completed_at": now()}
        self.save()

    def apple_build(self, build):
        result = self.call("asc", "builds", "list", "--app", self.manifest["app_id"], "--platform", "IOS",
            "--version", self.manifest["version"], "--build-number", build["appBuildVersion"],
            "--processing-state", "all", "--include", "app,preReleaseVersion", "--paginate", "--output", "json")
        candidates = records(result)
        if len(candidates) > 1:
            raise Blocked("Apple returned multiple builds for the exact version/build number")
        if not candidates:
            return None
        item = candidates[0]
        if str(attrs(item).get("version")) != build["appBuildVersion"] or linked(item, "app") != self.manifest["app_id"]:
            raise Blocked("Apple Build identity mismatch")
        prerelease = next((v for v in result.get("included", [])
            if v.get("id") == linked(item, "preReleaseVersion")), None)
        if not prerelease or attrs(prerelease).get("version") != self.manifest["version"] or attrs(prerelease).get("platform") != "IOS":
            raise Blocked("Apple Build marketing version/platform is unverified")
        return {"id": item["id"], "processingState": attrs(item).get("processingState")}

    def version(self):
        result = self.call("asc", "versions", "list", "--app", self.manifest["app_id"], "--version",
            self.manifest["version"], "--platform", "IOS", "--include", "build", "--paginate", "--output", "json")
        versions = records(result)
        if len(versions) > 1:
            raise Blocked("Ambiguous target ASC version")
        if not versions:
            return None
        item = versions[0]
        if attrs(item).get("versionString") != self.manifest["version"] or attrs(item).get("platform") != "IOS":
            raise Blocked("ASC version identity mismatch")
        return item

    def version_state(self, version):
        a = attrs(version)
        return a.get("appVersionState") or a.get("appStoreState")

    def details(self, version):
        response = self.call("asc", "review", "details-for-version", "--version-id", version["id"], "--output", "json")
        data = records(response)
        return data[0] if len(data) == 1 else None

    def validate_store(self, version_id):
        # Keep errors and actionable warnings blocking, while respecting Apple's
        # conditional promotional-image requirement for unpromoted subscriptions.
        report = self.call("asc", "validate", "--app", self.manifest["app_id"], "--version-id", version_id,
                           "--platform", "IOS", "--output", "json")
        if any(c.get("severity") == "error" for c in report.get("checks", [])):
            raise Blocked("ASC readiness errors require remediation")
        warnings = [c for c in report.get("checks", []) if c.get("severity") == "warning"]
        actionable = [c for c in warnings if c.get("id") != "subscriptions.images.recommended"]
        if actionable:
            raise Blocked("ASC readiness warnings require remediation: " + ", ".join(
                c.get("id", "unknown") for c in actionable))
        if warnings:
            promoted = self.call("asc", "subscriptions", "promoted-purchases", "list",
                                 "--app", self.manifest["app_id"], "--paginate", "--output", "json")
            if records(promoted):
                raise Blocked("Promoted subscriptions need their promotional images verified")
        return report

    def stage_matches(self, version, apple, notes, localizations):
        if not version or linked(version, "build") != apple["id"]:
            return False
        a = attrs(version)
        if a.get("releaseType") != self.manifest["release_type"]:
            return False
        if self.manifest["release_type"] == "SCHEDULED":
            def date(value):
                return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not a.get("earliestReleaseDate") or date(a["earliestReleaseDate"]) != date(self.manifest["earliest_release_date"]):
                return False
        response = self.call("asc", "localizations", "list", "--version", version["id"], "--paginate", "--output", "json")
        remote = {attrs(v)["locale"]: attrs(v).get("whatsNew") for v in records(response)}
        if remote != localizations:
            return False
        detail = self.details(version)
        return bool(detail and attrs(detail).get("notes", "").strip() == notes)

    def apply_stage(self, apple, notes, localizations):
        """Reconcile each write independently; don't replay a partial staging batch."""
        def change(operation, matches, argv, readback):
            pending = self.state.get("pending", {}).get("operation")
            if matches:
                if pending == operation:
                    self.done(operation)
                return
            if pending:
                raise Uncertain(f"{pending} is not reconciled; inspect that remote result first")
            self.mutate(operation, argv)
            if not readback():
                raise Uncertain(f"{operation} readback did not match the intended result")
            self.done(operation)

        version = self.version()
        create_args = ["asc", "versions", "create", "--app", self.manifest["app_id"],
            "--version", self.manifest["version"], "--platform", "IOS", "--release-type",
            self.manifest["release_type"], "--output", "json"]
        if not version:
            live_version = status(self.root, self.runner)["live"].get("versionString")
            if not live_version or live_version == self.manifest["version"]:
                raise Blocked("Verify the previous live version before creating an update")
            create_args += ["--copy-metadata-from", live_version, "--exclude-fields", "whatsNew"]
        change("stage:create", bool(version), create_args, lambda: bool(self.version()))
        version = self.version()
        self.state["version_id"] = version["id"]
        self.save()

        def locales_match():
            response = self.call("asc", "localizations", "list", "--version", version["id"], "--paginate", "--output", "json")
            remote = {attrs(v)["locale"]: attrs(v).get("whatsNew") for v in records(response)}
            return remote == localizations

        change("stage:metadata", locales_match(), ["asc", "metadata", "apply", "--app", self.manifest["app_id"],
            "--version", self.manifest["version"], "--platform", "IOS", "--dir",
            str(self.directory / "metadata"), "--output", "json"], locales_match)
        change("stage:attach", linked(self.version(), "build") == apple["id"],
            ["asc", "versions", "attach-build", "--version-id", version["id"], "--build-id", apple["id"], "--output", "json"],
            lambda: linked(self.version(), "build") == apple["id"])
        detail = self.details(version)
        if not detail:
            raise Blocked("ASC review contact/access information is missing; prepare it with supported tools")

        def notes_match():
            current = self.details(version)
            return bool(current and attrs(current).get("notes", "").strip() == notes)

        change("stage:notes", notes_match(), ["asc", "review", "details-update", "--id", detail["id"],
            "--notes", notes, "--output", "json"], notes_match)

        def strategy_match():
            a = attrs(self.version())
            if a.get("releaseType") != self.manifest["release_type"]:
                return False
            if self.manifest["release_type"] == "SCHEDULED":
                actual = a.get("earliestReleaseDate")
                expected = self.manifest["earliest_release_date"]
                return bool(actual) and dt.datetime.fromisoformat(actual.replace("Z", "+00:00")) == dt.datetime.fromisoformat(expected.replace("Z", "+00:00"))
            return True

        args = ["asc", "versions", "update", "--version-id", version["id"], "--release-type",
                self.manifest["release_type"], "--output", "json"]
        if self.manifest["release_type"] == "SCHEDULED":
            args += ["--earliest-release-date", self.manifest["earliest_release_date"]]
        change("stage:strategy", strategy_match(), args, strategy_match)
        version = self.version()
        if not self.stage_matches(version, apple, notes, localizations):
            raise Blocked("Staged version differs from the intended build/notes/strategy")
        return version

    def adopt(self, build_id):
        with locked(self.path):
            self.state = read_json(self.path) if self.path.exists() else {"schema": 1}
            github_release.guard_writes(self.path.parent, allow="state")
            candidate = local_preflight(self.root, self.manifest)
            if self.state.get("source") not in (None, candidate):
                raise Blocked("Checkpoint source differs; use a new candidate")
            if self.state.get("build_id") not in (None, build_id):
                raise Blocked("A different Build is already bound to this release")
            build = self.call("eas", "build:view", build_id, "--json")
            verify_build(build, self.manifest)
            self.state.update(source=candidate, build_id=build_id, build=safe_build(build))
            if self.state.get("pending", {}).get("operation") == "build":
                self.done("build")
            self.save()
            return {"status": "adopted", "build": safe_build(build)}

    def archive(self):
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        atomic_json(self.path.parent / "history" / f"{stamp}.json", self.state)

    def candidate(self):
        with locked(self.path):
            if self.config.get("notes_adapter"):
                try:
                    changelog.check_local(self.directory, self.manifest["version"], self.manifest["locales"], self.config, self.root)
                except changelog.NotesError as error:
                    raise Blocked(str(error)) from error
            self.state = read_json(self.path) if self.path.exists() else {"schema": 1}
            github_release.guard_writes(self.path.parent, allow="state")
            if self.path.with_name("github.json").exists():
                raise Blocked("GitHub publication intent already binds this version; preserve its source and use a new version")
            if self.state.get("pending"):
                raise Uncertain("Reconcile the previous write before creating another candidate")
            if not source_clean(self.root):
                raise Blocked("Commit the new shipped source before creating a candidate")
            build_id = self.state.get("build_id")
            if build_id:
                build = self.call("eas", "build:view", build_id, "--json")
                if build.get("status") not in ("FINISHED", "ERRORED", "CANCELED"):
                    raise Blocked("Previous candidate build is still active")
                if self.state.get("submission_id"):
                    submission = self.call("eas", "submit:view", self.state["submission_id"], "--json")
                    if submission.get("status") not in ("FINISHED", "ERRORED", "CANCELED"):
                        raise Blocked("Previous upload is still active")
                version = self.version()
                if version and self.version_state(version) not in {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED"}:
                    raise Blocked("Existing review/release is active; resolve it before a new candidate")
            self.archive()
            self.manifest["source_commit"] = git(self.root, "rev-parse", "HEAD")
            atomic_json(self.directory / "release.json", self.manifest)
            self.state = {"schema": 1, "source": source(self.root)}
            self.save()
            atomic_json(self.directory / "acceptance.json", {})
            return {"status": "candidate-prepared", "source_commit": self.manifest["source_commit"], "remote_mutations": 0}

    def retry_upload(self):
        with locked(self.path):
            self.state = read_json(self.path) if self.path.exists() else {"schema": 1}
            github_release.guard_writes(self.path.parent, allow="state")
            if self.state.get("pending"):
                raise Uncertain("Reconcile an unknown outcome instead of retrying it")
            local_preflight(self.root, self.manifest)
            if not self.state.get("build_id") or not self.state.get("submission_id"):
                raise Blocked("No bound failed submission to retry")
            build = self.call("eas", "build:view", self.state["build_id"], "--json")
            verify_build(build, self.manifest)
            if build.get("status") != "FINISHED":
                raise Blocked("Only a finished candidate can be reuploaded")
            submission = self.call("eas", "submit:view", self.state["submission_id"], "--json")
            if submission.get("status") not in ("ERRORED", "CANCELED") or (submission.get("submittedBuild") or {}).get("id") != build["id"] or (submission.get("iosConfig") or {}).get("ascAppIdentifier") != self.manifest["app_id"]:
                raise Blocked("Remote submission is not a verified failed upload of this build")
            apple = self.apple_build(build)
            self.archive()
            if apple:
                self.state["apple_build"] = apple
            else:
                self.state.pop("submission_id", None)
                self.state.get("receipts", {}).pop("upload", None)
            self.save()
            return {"status": "retry-prepared" if not apple else "upload-found", "remote_mutations": 0}

    def _run_apple_locked(self, through):
        self.state = read_json(self.path) if self.path.exists() else {"schema": 1}
        candidate = local_preflight(self.root, self.manifest)
        if self.state.get("source") not in (None, candidate):
            raise Blocked("Checkpoint source/config changed; start a new candidate")
        self.state["source"] = candidate
        notes, localizations, fingerprint = inputs(self.directory, self.manifest, self.config, self.root) if through in ("stage", "review") else (None, None, None)
        toolcheck(self.root, self.runner, require_asc=through != "build")
        if through != "build" and not asc_auth(self.root, self.runner):
            raise Blocked("Independent ASC authentication is missing; EAS managed access is separate")
        if through != "build":
            app_response = self.call("asc", "apps", "view", "--id", self.manifest["app_id"], "--output", "json")
            app_records = records(app_response)
            if len(app_records) != 1 or app_records[0].get("id") != self.manifest["app_id"] or attrs(app_records[0]).get("bundleId") != self.manifest["bundle_id"]:
                raise Blocked("Authenticated ASC app identity is unverified")
        acceptance = read_json(self.directory / "acceptance.json")
        # Responsibility policy is explicit and frozen in the manifest.
        evidence(self.root, acceptance, "code", self.manifest)
        if self.manifest["acceptance_policy"]["production"] == "required":
            evidence(self.root, acceptance, "production", self.manifest)
        pending = self.state.get("pending", {}).get("operation")
        if pending == "build" and not self.state.get("build_id"):
            raise Uncertain("Build dispatch unknown; inspect EAS and adopt the exact Build ID")
        if not self.state.get("build_id"):
            output = self.mutate("build", ["eas", "build", "--platform", "ios", "--profile",
                self.manifest["profile"], "--no-wait", "--non-interactive", "--json", "--freeze-credentials"])
            try:
                builds = records(json.loads(output))
                if len(builds) != 1 or not builds[0].get("id"):
                    raise ValueError()
                self.state["build_id"] = builds[0]["id"]
                self.done("build")
            except (ValueError, KeyError):
                raise Uncertain("Build dispatch returned no unique ID; inspect EAS then adopt")
        build = self.call("eas", "build:view", self.state["build_id"], "--json")
        verify_build(build, self.manifest)
        self.state["build"] = safe_build(build)
        self.save()
        if build["status"] != "FINISHED":
            if build["status"] in ("ERRORED", "CANCELED"):
                raise Blocked("Candidate build failed; diagnose it before selecting a new candidate")
            return {"status": "pending", "phase": "build", "build": safe_build(build)}
        if through == "build":
            return {"status": "complete", "through": "build", "build": safe_build(build)}
        evidence(self.root, acceptance, "artifact", self.manifest, build)
        apple = self.apple_build(build)
        pending = self.state.get("pending", {}).get("operation")
        if apple and pending == "upload":
            self.done("upload")
        if not apple:
            if pending == "upload":
                raise Uncertain("Upload outcome unknown and Apple Build absent; inspect submission logs")
            if not self.state.get("submission_id"):
                result = self.call("eas", "submit:list", "--platform", "ios", "--limit", "50", "--json", "--non-interactive")
                matches = [s for s in records(result) if (s.get("submittedBuild") or {}).get("id") == build["id"]
                    and (s.get("iosConfig") or {}).get("ascAppIdentifier") == self.manifest["app_id"]
                    and s.get("status") not in ("ERRORED", "CANCELED")]
                if not matches:
                    self.mutate("upload", ["eas", "submit", "--platform", "ios", "--profile", self.manifest["submit_profile"],
                        "--id", build["id"], "--non-interactive", "--no-wait"])
                    result = self.call("eas", "submit:list", "--platform", "ios", "--limit", "50", "--json", "--non-interactive")
                    matches = [s for s in records(result) if (s.get("submittedBuild") or {}).get("id") == build["id"]
                        and (s.get("iosConfig") or {}).get("ascAppIdentifier") == self.manifest["app_id"]
                        and s.get("status") not in ("ERRORED", "CANCELED")]
                if len(matches) != 1:
                    raise Uncertain("Upload returned no unique matching submission; inspect EAS/Apple")
                self.state["submission_id"] = matches[0]["id"]
                self.done("upload")
            submission = self.call("eas", "submit:view", self.state["submission_id"], "--json")
            if (submission.get("submittedBuild") or {}).get("id") != build["id"] or (submission.get("iosConfig") or {}).get("ascAppIdentifier") != self.manifest["app_id"]:
                raise Blocked("EAS submission identity mismatch")
            if submission.get("status") in ("ERRORED", "CANCELED"):
                raise Blocked("EAS upload failed; inspect cause and reuse this build after correction")
            return {"status": "pending", "phase": "upload-or-apple-processing", "submission_id": self.state["submission_id"]}
        self.state["apple_build"] = apple
        self.save()
        if apple["processingState"] != "VALID":
            if apple["processingState"] in ("FAILED", "INVALID"):
                raise Blocked("Apple rejected the candidate binary; inspect processing errors")
            return {"status": "pending", "phase": "apple-processing", "apple_build": apple}
        if through == "upload":
            return {"status": "complete", "through": "upload", "apple_build": apple}
        if self.manifest["acceptance_policy"]["device"] == "required":
            evidence(self.root, acceptance, "device", self.manifest, build)
        version = self.version()
        if version and self.version_state(version) in SUBMITTED:
            evidence(self.root, acceptance, "store", self.manifest, build)
            if acceptance["store"].get("metadata_digest") != fingerprint:
                raise Blocked("Store acceptance is stale for these release inputs")
            if not self.stage_matches(version, apple, notes, localizations):
                raise Blocked("Submitted version does not match this build/metadata; inspect rather than edit it")
            self.state.update(version_id=version["id"], metadata_digest=fingerprint)
            self.done("review")
            return {"status": "complete", "through": "review", "version_id": version["id"], "apple_state": self.version_state(version)}
        pending = self.state.get("pending", {}).get("operation")
        if pending == "review":
            raise Uncertain("Review result unknown; draft state is not proof of submission")
        if version and self.version_state(version) in {"REJECTED", "METADATA_REJECTED"}:
            evidence(self.root, acceptance, "review_feedback", self.manifest, build)
            feedback = acceptance["review_feedback"]
            if feedback.get("version_id") != version["id"] or not text_ready(feedback.get("resolution_summary", "")):
                raise Blocked("Review feedback needs the current Apple Version ID and a concrete resolution")
            if acceptance["review_feedback"].get("metadata_digest") != fingerprint:
                raise Blocked("Review feedback evidence is stale for the current release inputs")
        elif version and self.version_state(version) not in {"PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "DEVELOPER_REJECTED"}:
            raise Blocked("Inspect the current App Review outcome before changing or resubmitting this version")
        version = self.apply_stage(apple, notes, localizations)
        self.state.update(version_id=version["id"], metadata_digest=fingerprint)
        self.done("stage")
        self.validate_store(version["id"])
        if through == "stage":
            return {"status": "complete", "through": "stage", "version_id": version["id"]}
        evidence(self.root, acceptance, "store", self.manifest, build)
        if acceptance["store"].get("metadata_digest") != fingerprint:
            raise Blocked("Store acceptance is stale for these release inputs")
        self.mutate("review", ["asc", "review", "submit", "--app", self.manifest["app_id"], "--version-id", version["id"],
            "--build-id", apple["id"], "--confirm", "--output", "json"])
        version = self.version()
        if not version or linked(version, "build") != apple["id"] or self.version_state(version) not in SUBMITTED:
            raise Uncertain("Review command returned but Apple has not verified formal submission")
        self.done("review")
        return {"status": "complete", "through": "review", "version_id": version["id"], "apple_state": self.version_state(version)}


def prepare(root, version, base_ref, locales):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise Blocked("Version must be numeric x.y.z")
    config = project_config(root)
    directory = root / config["release_docs"] / version
    if not (directory / "README.md").is_file():
        raise Blocked("Confirm version scope in its README first")
    manifest_path = directory / "release.json"
    if manifest_path.exists():
        raise Blocked("Release already exists; load and resume it")
    if not locales or len(set(locales)) != len(locales):
        raise Blocked("Specify unique actual ASC locales")
    base = git(root, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}")
    manifest = {"schema": 2, "version": version, "app_id": config["app_id"], "project_id": config["project_id"],
        "bundle_id": config["bundle_id"], "profile": config["build_profile"], "submit_profile": config["submit_profile"],
        "acceptance_policy": config["acceptance_policy"], "source_commit": git(root, "rev-parse", "HEAD"),
        "base_commit": base, "locales": locales, "release_type": "MANUAL"}
    for loc in locales:
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", loc):
            raise Blocked("Invalid ASC locale")
    atomic_json(manifest_path, manifest)
    if not (directory / "acceptance.json").exists():
        atomic_json(directory / "acceptance.json", {})
    for name in ("review-notes.txt", "what-to-test.txt"):
        if not (directory / name).exists():
            (directory / name).write_text("TBD\n")
    for loc in locales:
        path = directory / "metadata" / "version" / version / f"{loc}.json"
        if not path.exists():
            atomic_json(path, {"whatsNew": "TBD"})
    return {"status": "prepared", "manifest": str(manifest_path), "remote_calls": 0}


def plan(path, through="review", github_only=False):
    root, directory, manifest = load_manifest(path)
    config = project_config(root)
    issues = []
    fingerprint = None
    checks = [lambda: inputs(directory, manifest, config, root)]
    if not github_only:
        checks.insert(0, lambda: local_preflight(root, manifest))
    for check in checks:
        try:
            check()
        except (Blocked, github_release.GitHubError, CommandFailure, FileNotFoundError, KeyError) as error:
            issues.append(str(error))
    try:
        fingerprint = inputs(directory, manifest, config, root)[2]
    except (Blocked, FileNotFoundError, KeyError):
        pass
    result = {"status": "plan", "version": manifest["version"], "app_id": manifest["app_id"], "remote_calls": 0,
        "issues": issues, "metadata_digest": fingerprint, "phases": [
            {"phase": "build", "tool": "eas build", "profile": manifest["profile"], "creates": "EAS Build ID"},
            {"phase": "upload", "tool": "eas submit --id", "requires": "Exact verified EAS Build"},
            {"phase": "stage", "tool": "asc versions create / metadata apply / attach-build / review details-update / versions update", "release_type": manifest["release_type"]},
            {"phase": "review", "tool": "asc validate / review submit", "requires": "Same accepted Apple Build and formal state readback"},
            {"phase": "github", "tool": "git tag / git push / gh release create", "requires": "Submitted exact Apple version/build and source-bound canonical notes"}]}
    result["phases"] = result["phases"][:STEPS.index(through) + 1]
    if github_only:
        result["phases"] = result["phases"][-1:]
        result["mode"] = "github-only"
    if through == "github":
        try:
            repo = github_release.repository(root, git)
            commit, title = Pipeline(path).publisher().source_info(repo)
            body = changelog.check_local(directory, manifest["version"], manifest["locales"], config, root)
            result["github"] = {"repository": repo, "tag": "v" + manifest["version"], "source_commit": commit,
                "title": title, "body_sha256": body["bodySha256"], "draft": False, "prerelease": False,
                "latest": "resolve from full remote version history during execution"}
        except (github_release.GitHubError, changelog.NotesError, CommandFailure, OSError, ValueError, KeyError) as error:
            issues.append(str(error))
    return result


def doctor(root, runner=None, through=None):
    runner = runner or Runner()
    config = project_config(root)
    issues = []
    try:
        configuration(root)
    except (Blocked, KeyError, OSError, ValueError) as error:
        issues.append(str(error) if isinstance(error, Blocked) else "Required project configuration is missing or invalid")
    try:
        toolcheck(root, runner)
    except (Blocked, CommandFailure, OSError) as error:
        issues.append(str(error) if isinstance(error, Blocked) else "Required local tool unavailable")
    try:
        auth = asc_auth(root, runner)
    except (CommandFailure, OSError, ValueError):
        auth = False
    if not auth:
        issues.append("Independent ASC authentication not configured; EAS managed credentials are separate")
    else:
        try:
            response = command_json(runner, ["asc", "apps", "view", "--id", config["app_id"], "--output", "json"], root)
            apps = records(response)
            if len(apps) != 1 or apps[0].get("id") != config["app_id"] or attrs(apps[0]).get("bundleId") != config["bundle_id"]:
                issues.append("Authenticated ASC app identity differs")
        except (CommandFailure, OSError, ValueError):
            issues.append("ASC credentials cannot read the configured app")
    if through == "github":
        try:
            github_release.Client(root, runner).preflight()
        except (github_release.GitHubError, CommandFailure, OSError, ValueError):
            issues.append("GitHub CLI, repository identity or write access is unverified")
    return {"status": "ready" if not issues else "blocked", "required_tools": config["tool_versions"],
            "asc_credentials_available": auth, "issues": issues, "remote_mutations": 0}


def status(root, runner=None):
    runner = runner or Runner()
    config = project_config(root)
    result = command_json(runner, ["eas", "submit:status", "--platform", "ios", "--profile",
        config["submit_profile"], "--json", "--non-interactive"], root)
    ios = result.get("ios", {})
    if ios.get("ascAppIdentifier") != config["app_id"]:
        raise Blocked("Read-only status belongs to a different ASC app")
    live = ios.get("live") or {}
    return {"status": "read-only", "app_id": config["app_id"],
        "live": {k: live.get(k) for k in ("versionString", "buildNumber", "state", "easBuildId", "easSubmissionId")},
        "testflight": [{k: b.get(k) for k in ("appVersion", "buildNumber", "processingState", "internalState", "externalState")}
                       for b in ios.get("testFlightBuilds", [])], "remote_mutations": 0}


def release_status(path, runner=None):
    pipeline = Pipeline(path, runner)
    with locked(pipeline.path):
        pipeline.state = read_json(pipeline.path) if pipeline.path.exists() else {"schema": 1}
        state = pipeline.state
        requested = state.get("requested_through", "review")
        pending = []
        for name in ("state", "github", "notes"):
            receipt = pipeline.path.with_name(name + ".json")
            data = read_json(receipt) if receipt.exists() else {}
            if data.get("pending"):
                pending.append({"channel": name, "operation": data["pending"]["operation"]})
            if name == "notes":
                pending.extend({"channel": name, "operation": op} for op, value in data.get("operations", {}).items()
                               if value.get("status") == "pending")
        apple, published, completed = {"status": "not-started"}, {"status": "not-verified"}, None
        try:
            if state.get("build_id"):
                build = pipeline.call("eas", "build:view", state["build_id"], "--json")
                verify_build(build, pipeline.manifest)
                apple = {"status": "pending", "phase": "build", "build": safe_build(build)}
                if build["status"] in ("ERRORED", "CANCELED"):
                    apple["status"] = "blocked"
                elif build["status"] == "FINISHED":
                    completed = "build"
                    apple["phase"] = "upload"
                    bound = pipeline.apple_build(build)
                    if bound:
                        apple["apple_build"] = bound
                    if bound and bound["processingState"] == "VALID":
                        completed = "upload"
                        apple["phase"] = "stage"
                        version = pipeline.version()
                        if version:
                            apple.update(version_id=version["id"], apple_state=pipeline.version_state(version))
                            if linked(version, "build") == bound["id"]:
                                notes, localizations, _ = inputs(pipeline.directory, pipeline.manifest, pipeline.config, pipeline.root)
                                if not pipeline.stage_matches(version, bound, notes, localizations):
                                    raise Blocked("Staged version differs from the requested metadata")
                                pipeline.validate_store(version["id"])
                                completed, apple["phase"] = "stage", "review"
                                if pipeline.version_state(version) in SUBMITTED:
                                    pipeline.github_evidence()
                                    completed = "review"
                                    apple["status"] = "verified"
        except (Blocked, github_release.GitHubError, CommandFailure, OSError, ValueError, KeyError):
            apple.update(status="blocked", reason="Exact Apple candidate, acceptance or remote state is unverified")
        try:
            publisher = pipeline.publisher()
            identity = publisher.preflight(write=requested == "github")
            tag = "v" + pipeline.manifest["version"]
            remote = publisher.remote_tag(tag)
            item = publisher.release(identity["repository"], tag)
            published = {"status": "missing", "tag": tag, "tag_source": remote}
            if remote not in (None, pipeline.manifest["source_commit"]):
                raise github_release.GitHubError("Tag source conflict")
            if item:
                intent = publisher.intent(identity)
                publisher.verify_release(item, intent)
                if remote != intent["source_commit"]:
                    raise github_release.GitHubError("Published tag source is unverified")
                previous = publisher.journal.get("intent", {})
                if previous and any(previous.get(k) != v for k, v in intent.items()):
                    raise github_release.GitHubError("GitHub publication journal identity differs")
                latest = publisher.latest_policy(identity["repository"], tag)
                newest = publisher.api(f"repos/{identity['repository']}/releases/latest", optional=True)
                if not newest or (newest.get("id") == item["id"]) != latest:
                    raise github_release.GitHubError("GitHub Latest differs from version history")
                published.update(status="verified", release_id=item["id"], url=item["html_url"], latest=latest)
        except (Blocked, github_release.GitHubError, CommandFailure, OSError, ValueError, KeyError):
            published.update(status="blocked", reason="Exact GitHub repository, tag or Release is unverified")
        if pending:
            overall = "uncertain"
        elif apple["status"] == "blocked" or requested == "github" and published["status"] == "blocked":
            overall = "blocked"
        elif requested == "github" and completed == "review" and published["status"] == "verified":
            overall, completed = "complete", "github"
        elif requested != "github" and completed and STEPS.index(completed) >= STEPS.index(requested):
            overall = "complete"
        else:
            overall = "pending" if state.get("build_id") else "not-started"
        return {"status": "read-only", "version": pipeline.manifest["version"], "requested_through": requested,
            "completed_through": completed, "overall_status": overall, "apple": apple, "github": published,
            "pending_operations": pending, "remote_mutations": 0,
            "next_action": ("Resume the same endpoint to reconcile pending writes; inspect unresolved absence" if pending else
                            "Inspect the reported channel and resolve its blocker" if overall == "blocked" else
                            "None for the requested endpoint" if overall == "complete" else
                            "Resume the requested endpoint when its prerequisites are ready")}


def checks(path, runner=None):
    root, directory, manifest = load_manifest(path)
    local_preflight(root, manifest)
    runner = runner or Runner()
    config = project_config(root)
    logs = root / config["artifact_dir"] / "release-checks" / manifest["version"]
    logs.mkdir(parents=True, exist_ok=True)
    outcomes, refs = [], []
    for index, argv in enumerate(config["checks"]):
        try:
            runner.run(argv, root, timeout=600)
            code = 0
        except CommandFailure as error:
            code = error.code
        log = logs / f"{index}.json"
        atomic_json(log, {"command": argv, "exit_code": code, "source_commit": manifest["source_commit"], "time": now()})
        refs.append({"path": str(log.relative_to(root)), "sha256": digest(log)})
        outcomes.append({"command": argv, "exit_code": code})
    acceptance = read_json(directory / "acceptance.json")
    acceptance["code"] = {"passed": all(x["exit_code"] == 0 for x in outcomes),
        "source_commit": manifest["source_commit"], "evidence": refs}
    atomic_json(directory / "acceptance.json", acceptance)
    return {"status": "passed" if acceptance["code"]["passed"] else "blocked", "checks": outcomes}


def failure_context(args):
    if not getattr(args, "manifest", None):
        return {}
    try:
        pipeline = Pipeline(args.manifest)
        state = pipeline.state
        journal = read_json(pipeline.path.with_name("github.json")) if pipeline.path.with_name("github.json").exists() else {}
        target = "github" if args.command == "github" else getattr(args, "through", state.get("requested_through", "review"))
        pending = journal.get("pending") or state.get("pending")
        phase = "github" if args.command == "github" or journal.get("pending") or target == "github" and state.get("receipts", {}).get("review") else (
            pending.get("operation", "preflight") if pending else "preflight")
        return {"requested_through": target, "phase": phase,
                "completed_through": "review" if state.get("receipts", {}).get("review") else None}
    except (Blocked, CommandFailure, OSError, ValueError, KeyError):
        return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("notes-generate", "notes-check", "notes-sync"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--version", required=True)
        if name != "notes-generate":
            command.add_argument("--channels", nargs="+", choices=("asc", "github", "testflight"), default=[])
        if name == "notes-sync":
            command.add_argument("--execute", action="store_true")
    for name in ("doctor", "status", "prepare"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        if name == "status":
            command.add_argument("--manifest", type=Path)
        if name == "doctor":
            command.add_argument("--through", choices=STEPS)
        if name == "prepare":
            command.add_argument("--version", required=True)
            command.add_argument("--base-ref", required=True)
            command.add_argument("--locales", nargs="+", required=True)
    for name in ("plan", "checks", "run", "github", "adopt", "candidate", "retry-upload"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        if name in ("run", "github"):
            command.add_argument("--execute", action="store_true")
        if name in ("run", "plan"):
            command.add_argument("--through", choices=STEPS, default="review")
        if name == "adopt":
            command.add_argument("--build-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command.startswith("notes-"):
            root = args.root.resolve()
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
                raise Blocked("Version must be numeric x.y.z")
            config = project_config(root)
            directory = root / config["release_docs"] / args.version
            if not (directory / "README.md").is_file():
                raise Blocked("Confirm the version scope first")
            manifest_path = directory / "release.json"
            if manifest_path.is_file():
                _, _, manifest = load_manifest(manifest_path)
            else:
                raise Blocked("Prepare the release manifest with explicit locales first")
            if args.command == "notes-generate":
                result = changelog.generate(directory, args.version, manifest["locales"], atomic_json, config, root)
            elif args.channels:
                if not manifest_path.is_file():
                    raise Blocked("Remote notes operations need the existing release manifest")
                with locked(state_path(root, args.version)):
                    result = changelog.remote_notes(root, directory, manifest, args.channels,
                        getattr(args, "execute", False), Runner(),
                        {"state_path": state_path, "now": now, "atomic_json": atomic_json, "app_id": manifest["app_id"],
                         "git": git, "github": github_release, "config": config})
            elif args.command == "notes-sync":
                raise Blocked("Select notes channels explicitly")
            else:
                data = changelog.check_local(directory, args.version, manifest["locales"], config, root)
                result = {"status": "verified", "version": args.version, "body_sha256": data["bodySha256"], "remote_calls": 0}
        elif args.command == "prepare":
            result = prepare(args.root.resolve(), args.version, args.base_ref, args.locales)
        elif args.command == "doctor":
            result = doctor(args.root.resolve(), through=args.through)
        elif args.command == "status":
            result = release_status(args.manifest) if args.manifest else status(args.root.resolve())
        elif args.command == "checks":
            result = checks(args.manifest)
        elif args.command == "plan" or args.command == "run" and not args.execute:
            result = plan(args.manifest, getattr(args, "through", "review"))
        elif args.command == "github":
            result = Pipeline(args.manifest).github() if args.execute else plan(args.manifest, "github", github_only=True)
        elif args.command == "adopt":
            result = Pipeline(args.manifest).adopt(args.build_id)
        elif args.command == "candidate":
            result = Pipeline(args.manifest).candidate()
        elif args.command == "retry-upload":
            result = Pipeline(args.manifest).retry_upload()
        else:
            result = Pipeline(args.manifest).run(args.through)
    except (Uncertain, changelog.NotesUncertain, github_release.GitHubUncertain) as error:
        print(json.dumps({"status": "uncertain", "reason": str(error), **failure_context(args)}, ensure_ascii=False))
        return 3
    except (Blocked, changelog.NotesError, github_release.GitHubError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error), **failure_context(args)}, ensure_ascii=False))
        return 2
    except (CommandFailure, subprocess.TimeoutExpired, OSError, ValueError, KeyError):
        print(json.dumps({"status": "blocked", "reason": "Required command or input failed; inspect the phase using supported tools", **failure_context(args)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] in ("blocked", "differences") else 0


if __name__ == "__main__":
    sys.exit(main())

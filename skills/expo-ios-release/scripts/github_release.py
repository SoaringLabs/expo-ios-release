"""Exact-source GitHub publication and reconciliation; no Apple mutations."""
import datetime as dt
import json
from pathlib import Path
import re
import subprocess


class CommandFailure(Exception):
    def __init__(self, code, stdout="", stderr=""):
        super().__init__(f"Command failed with exit code {code}")
        self.code, self.stdout, self.stderr = code, stdout, stderr


class GitHubError(Exception):
    pass


class GitHubUncertain(GitHubError):
    pass


def repository(root, git):
    def parse(value):
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", value)
        if not match:
            raise GitHubError("Verify the GitHub origin; credential-bearing and non-GitHub URLs are unsupported")
        return match[1]
    fetch = parse(git(root, "remote", "get-url", "origin"))
    pushes = git(root, "remote", "get-url", "--push", "--all", "origin").splitlines()
    if len(pushes) != 1 or parse(pushes[0]).lower() != fetch.lower():
        raise GitHubError("GitHub fetch/push destinations differ or multiple push destinations exist")
    return fetch


def tag_source(output, tag):
    refs = {}
    for line in output.splitlines():
        sha, name = line.split()
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise GitHubError("GitHub tag returned an invalid object ID")
        if name not in (f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}") or name in refs:
            raise GitHubError("GitHub tag returned ambiguous refs")
        refs[name] = sha
    return refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))


def guard_writes(directory, allow=None):
    """All callers hold the release lock; only the owning phase may reconcile."""
    for name in ("state", "github", "notes"):
        path = Path(directory) / f"{name}.json"
        if not path.exists() or name == allow:
            continue
        data = json.loads(path.read_text())
        pending = data.get("pending") or (name == "notes" and any(
            item.get("status") == "pending" for item in data.get("operations", {}).values()))
        if pending:
            raise GitHubUncertain(f"Reconcile the pending {name} write before changing another channel")


class Client:
    def __init__(self, root, runner):
        self.root, self.runner = root, runner

    def git(self, root, *args):
        return self.runner.run(["git", *args], root)

    def api(self, endpoint, optional=False):
        try:
            output = self.runner.run(["gh", "api", "--hostname", "github.com", "--include", endpoint], self.root)
        except CommandFailure as error:
            # A real HTTP 404, after repository access verification, means absent.
            if optional and re.match(r"HTTP/[\d.]+ 404(?:\s|$)", error.stdout):
                return None
            raise GitHubError("GitHub API read failed; authentication, permission and network failures are not absence") from error
        header, separator, body = output.replace("\r\n", "\n").partition("\n\n")
        if not separator or not re.match(r"HTTP/[\d.]+ 200(?:\s|$)", header):
            raise GitHubError("GitHub API returned an unverifiable HTTP response")
        return json.loads(body)

    def preflight(self, write=True):
        repo = repository(self.root, self.git)
        remote = self.api(f"repos/{repo}")
        if not remote.get("id") or str(remote.get("full_name", "")).lower() != repo.lower():
            raise GitHubError("Authenticated GitHub repository identity differs")
        if write and not any(remote.get("permissions", {}).get(k) for k in ("push", "maintain", "admin")):
            raise GitHubError("GitHub publication requires repository write access")
        return {"repository": remote["full_name"], "repository_id": remote["id"]}

    def remote_tag(self, tag):
        try:
            output = self.git(self.root, "ls-remote", "--exit-code", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
        except CommandFailure as error:
            if error.code == 2:
                return None
            raise GitHubError("Remote tag read failed; absence is unverified") from error
        return tag_source(output, tag)

    def local_tag(self, tag):
        output = self.git(self.root, "show-ref", "--tags") if self.git(
            self.root, "tag", "--list", tag) else ""
        if not any(line.split()[1] == f"refs/tags/{tag}" for line in output.splitlines()):
            return None
        return self.git(self.root, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")

    def release(self, repo, tag):
        return self.api(f"repos/{repo}/releases/tags/{tag}", optional=True)

    def latest_policy(self, repo, tag):
        output = self.runner.run(["gh", "api", "--hostname", "github.com", "--paginate", "--slurp",
            f"repos/{repo}/releases?per_page=100"], self.root)
        pages = json.loads(output)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise GitHubError("GitHub release history must include all pages")
        current = tuple(map(int, tag[1:].split(".")))
        latest = True
        for item in (item for page in pages for item in page):
            if item.get("draft") or item.get("prerelease"):
                continue
            match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", item.get("tag_name", ""))
            if not match:
                raise GitHubError("Resolve ambiguous formal version tags before choosing Latest")
            if tuple(map(int, match.groups())) > current:
                latest = False
        return latest


class Publisher(Client):
    def __init__(self, pipeline, helpers):
        super().__init__(pipeline.root, pipeline.runner)
        self.p, self.h = pipeline, helpers
        self.path = pipeline.path.with_name("github.json")
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else {"schema": 1}
        if self.journal.get("schema") != 1:
            raise GitHubError("Unsupported GitHub journal; reconcile it explicitly")

    def save(self):
        self.h["atomic_json"](self.path, self.journal)

    def source_info(self, repo):
        manifest = self.p.manifest
        source = manifest["source_commit"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", source):
            raise GitHubError("GitHub publication requires a full source commit")
        if self.git(self.root, "rev-parse", "--verify", f"{source}^{{commit}}") != source:
            raise GitHubError("Released source commit is unavailable")
        package = json.loads(self.git(self.root, "show", f"{source}:package.json"))
        if package.get("version") != manifest["version"]:
            raise GitHubError("Released package version differs from the manifest")
        name = repo.split("/")[1]
        app_files = self.git(self.root, "ls-tree", "--name-only", source, "app.json", "app.config.js", "app.config.ts").splitlines()
        if "app.json" in app_files and not any(name in app_files for name in ("app.config.js", "app.config.ts")):
            app = json.loads(self.git(self.root, "show", f"{source}:app.json")).get("expo", {})
            if app.get("version") != manifest["version"]:
                raise GitHubError("Released app version differs from the manifest")
            name = app.get("name") or name
        settings = manifest.get("github", {})
        if not isinstance(settings, dict) or set(settings) - {"title"}:
            raise GitHubError("Manifest github supports only an optional title")
        title = settings.get("title", f"{name} v{manifest['version']}")
        if not isinstance(title, str) or not title.strip() or len(title) > 256 or "\n" in title or "\r" in title:
            raise GitHubError("Provide a concrete single-line GitHub Release title")
        return source, title

    def intent(self, identity):
        source, title = self.source_info(identity["repository"])
        data = self.h["notes"].check_local(self.p.directory, self.p.manifest["version"], self.p.manifest["locales"])
        state = self.p.state
        return {**identity, "source_commit": source, "tag": "v" + self.p.manifest["version"], "title": title,
            "body_sha256": data["bodySha256"], "notes_path": str((self.p.directory / "release-notes.md").relative_to(self.root)),
            "eas_build_id": state["build_id"], "apple_build_id": state["apple_build"]["id"],
            "apple_version_id": state["version_id"], "draft": False, "prerelease": False}

    def verify_release(self, item, intent):
        if not item:
            return False
        notes = self.h["notes"]
        expected = {"tag_name": intent["tag"], "name": intent["title"], "draft": False, "prerelease": False}
        if any(item.get(k) != v for k, v in expected.items()) or notes.body_digest(item.get("body") or "") != intent["body_sha256"]:
            raise GitHubError("Existing GitHub Release conflicts with the publication intent; inspect or use authorized notes-sync")
        if not item.get("id") or not item.get("published_at") or item.get("html_url") != f"https://github.com/{intent['repository']}/releases/tag/{intent['tag']}":
            raise GitHubError("GitHub Release publication identity is unverified")
        if self.journal.get("release_id") not in (None, item["id"]):
            raise GitHubError("GitHub Release ID changed; preserve the original publication binding")
        try:
            if dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as error:
            raise GitHubError("GitHub Release publication date is unverified") from error
        return True

    def operation(self, name, matches, argv, readback):
        pending = self.journal.get("pending")
        if matches:
            if pending and pending["operation"] != name:
                raise GitHubUncertain("Reconcile the pending GitHub operation first")
        else:
            if pending:
                raise GitHubUncertain("Previous GitHub write is unresolved; remote absence alone does not permit replay")
            if repository(self.root, self.git).lower() != self.journal["intent"]["repository"].lower():
                raise GitHubError("GitHub origin changed before publication")
            self.journal["pending"] = {"operation": name, "started_at": self.h["now"]()}
            self.save()
            try:
                self.runner.run(argv, self.root)
                if not readback():
                    raise GitHubUncertain("GitHub write readback differs; pending intent retained")
            except (CommandFailure, OSError, subprocess.TimeoutExpired) as error:
                raise GitHubUncertain("GitHub write outcome unknown; resume to read back the exact resource") from error
            except (GitHubError, ValueError) as error:
                raise GitHubUncertain("GitHub write readback failed; pending intent retained") from error
        self.journal.pop("pending", None)
        self.journal.setdefault("receipts", {})[name] = {"verified_at": self.h["now"]()}
        self.save()

    def publish(self):
        guard_writes(self.path.parent, allow="github")
        identity = self.preflight()
        intent = self.intent(identity)
        previous = self.journal.get("intent")
        if previous and any(previous.get(k) != v for k, v in intent.items()):
            raise GitHubUncertain("GitHub publication source, target or body changed; reconcile the original intent")
        tag, source, repo = intent["tag"], intent["source_commit"], intent["repository"]
        remote, local = self.remote_tag(tag), self.local_tag(tag)
        if remote not in (None, source) or local not in (None, source):
            raise GitHubError("Existing tag conflicts with the released source; preserve it")
        item = self.release(repo, tag)
        self.verify_release(item, intent)
        if item and remote != source:
            raise GitHubError("Existing Release has no verified matching remote tag; inspect it before changing refs")
        if not remote and not local:
            self.git(self.root, "var", "GIT_COMMITTER_IDENT")
        latest = self.latest_policy(repo, tag)
        if (previous and previous.get("latest") != latest and
                self.journal.get("pending", {}).get("operation") == "release:create" and not item):
            raise GitHubUncertain("Version history changed during a pending GitHub write; inspect the original Latest intent")
        intent["latest"] = latest
        self.journal["intent"] = intent
        self.journal.setdefault("publication_body_sha256", intent["body_sha256"])
        self.save()
        pending = self.journal.get("pending", {}).get("operation")
        if pending and pending not in {"tag:create", "tag:push", "release:create"}:
            raise GitHubUncertain("Unsupported pending GitHub operation; inspect the journal")
        # Reconcile the owning operation before evaluating later steps.
        if pending == "release:create":
            self.operation(pending, bool(item), [], lambda: False)
        elif pending == "tag:push":
            self.operation(pending, remote == source, [], lambda: False)
        elif pending == "tag:create":
            self.operation(pending, local == source, [], lambda: False)
        if not remote:
            self.operation("tag:create", local == source,
                ["git", "tag", "-a", tag, source, "-m", intent["title"]], lambda: self.local_tag(tag) == source)
            self.operation("tag:push", False, ["git", "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],
                lambda: self.remote_tag(tag) == source)
        if self.remote_tag(tag) != source:
            raise GitHubError("Remote tag no longer identifies the released source")
        if not item:
            # Recheck the canonical body immediately before the external write.
            if self.h["notes"].body_digest((self.p.directory / "release-notes.md").read_text()) != intent["body_sha256"]:
                raise GitHubError("Canonical notes changed before publication")
            self.operation("release:create", False, ["gh", "release", "create", tag, "--repo", "github.com/" + repo,
                "--verify-tag", "--title", intent["title"], "--notes-file", str(self.p.directory / "release-notes.md"),
                "--latest=" + str(latest).lower()], lambda: self.verify_release(self.release(repo, tag), intent))
        item = self.release(repo, tag)
        self.verify_release(item, intent)
        if self.remote_tag(tag) != source:
            raise GitHubError("Published tag source changed during verification")
        newest = self.api(f"repos/{repo}/releases/latest", optional=True)
        if not newest or (newest.get("id") == item["id"]) != latest:
            raise GitHubError("GitHub Latest does not match the version policy")
        self.journal.update(status="verified", release_id=item["id"], url=item["html_url"],
            published_at=item["published_at"], verified_at=self.h["now"]())
        self.save()
        return {"status": "verified", "tag": tag, "source_commit": source,
            "release_id": item["id"], "url": item["html_url"], "latest": latest}

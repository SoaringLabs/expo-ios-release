"""Canonical release text, local projections and independently recoverable notes writes."""
import hashlib
import json
from pathlib import Path
import re
import subprocess


class NotesError(Exception):
    pass


class NotesUncertain(NotesError):
    pass


def normalize(text):
    return text.replace("\r\n", "\n").rstrip("\n")


def body_digest(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def parse(text):
    body = normalize(text)
    if not body.strip() or len(body) > 4000 or re.search(r"\b(?:TODO|TBD|FIXME)\b|待填写|<[^>]+>", body, re.I):
        raise NotesError("Release notes are empty, unfinished or over 4000 characters")
    return {"body": body}


def localized_body(directory, locales, locale):
    # The canonical body belongs only to the first explicitly selected locale.
    path = directory / ("release-notes.md" if locale == locales[0] else f"release-notes.{locale}.md")
    if not path.is_file():
        raise NotesError(f"Provide explicit release notes for {locale}: {path.name}")
    return parse(path.read_text(encoding="utf-8"))["body"]


def project_body(directory, locales, locale, config=None, root=None):
    source = localized_body(directory, locales, locale)
    adapter = (config or {}).get("notes_adapter")
    if not adapter:
        return source, source
    if root is None:
        raise NotesError("Project notes adapter needs the application root")
    source_path = directory / ("release-notes.md" if locale == locales[0] else f"release-notes.{locale}.md")
    try:
        result = subprocess.run([*adapter, str(source_path.resolve()), locale], cwd=root,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotesError(f"Project notes adapter could not run: {error}") from error
    if result.returncode:
        raise NotesError("Project notes adapter rejected the source: " + result.stderr.strip())
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("markdown"), str) or not isinstance(value.get("plainText"), str):
            raise ValueError("Expected schema 1, markdown and plainText")
        if normalize(value["markdown"]) != source:
            raise ValueError("Adapter must preserve the canonical Markdown")
        plain = parse(value["plainText"])["body"]
    except (ValueError, TypeError) as error:
        raise NotesError(f"Invalid project notes projection: {error}") from error
    return source, plain


def projections(directory, version, locales, config=None, root=None):
    if not locales or len(set(locales)) != len(locales) or any(
            not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", loc) for loc in locales):
        raise NotesError("Select unique actual ASC locales")
    projected = {loc: project_body(directory, locales, loc, config, root) for loc in locales}
    bodies = {loc: value[0] for loc, value in projected.items()}
    plain_bodies = {loc: value[1] for loc, value in projected.items()}
    body = bodies[locales[0]]
    data = {"schema": 1, "version": version, "locale": locales[0],
            "releaseNotes": body, "bodySha256": body_digest(body),
            "localizations": {loc: {"releaseNotes": value, "bodySha256": body_digest(value)}
                              for loc, value in bodies.items()}}
    if (config or {}).get("notes_adapter"):
        data.update(format="markdown", plainText=plain_bodies[locales[0]],
                    plainTextSha256=body_digest(plain_bodies[locales[0]]))
        for loc, plain in plain_bodies.items():
            data["localizations"][loc].update(plainText=plain, plainTextSha256=body_digest(plain))
    files = {directory / "metadata" / "version" / version / f"{loc}.json":
             json.dumps({"whatsNew": value}, ensure_ascii=False, indent=2) + "\n"
             for loc, value in plain_bodies.items()}
    files[directory / "release-notes.json"] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    templates = [("review-notes", "review-notes.template.txt", "review-notes.txt", plain_bodies[locales[0]], True)]
    for loc, value in plain_bodies.items():
        suffix = "" if loc == locales[0] else "." + loc
        templates.append(("what-to-test", f"what-to-test{suffix}.template.txt", f"what-to-test{suffix}.txt", value, False))
    for name, template_name, output_name, value, required in templates:
        template = directory / template_name
        if not template.exists():
            if required:
                raise NotesError("Create review-notes.template.txt with exactly one {{CHANGELOG}}")
            continue
        text = template.read_text(encoding="utf-8")
        if text.count("{{CHANGELOG}}") != 1:
            raise NotesError(f"{template.name} must contain exactly one {{{{CHANGELOG}}}}")
        rendered = parse(text.replace("{{CHANGELOG}}", value))["body"]
        files[directory / output_name] = rendered + "\n"
    return data, files


def check_local(directory, version, locales, config=None, root=None):
    data, files = projections(directory, version, locales, config, root)
    differences = [str(p.relative_to(directory)) for p, expected in files.items()
                   if not p.is_file() or normalize(p.read_text(encoding="utf-8")) != normalize(expected)]
    if differences:
        raise NotesError("Changelog copies differ: " + ", ".join(differences) + "; run notes-generate")
    return data


def generate(directory, version, locales, atomic_json, config=None, root=None):
    data, files = projections(directory, version, locales, config, root)
    for path in files:
        if path.parent.parent.name == "version" and path.parent.parent.parent.name == "metadata" and path.suffix == ".json" and path.exists() and set(json.loads(path.read_text())) != {"whatsNew"}:
            raise NotesError("Keep unrelated metadata fields out of the generated What's New file")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            atomic_json(path, json.loads(text))
        else:
            path.write_text(text, encoding="utf-8")
    return {"status": "generated", "version": version, "body_sha256": data["bodySha256"],
            "files": [str(p) for p in files], "remote_mutations": 0}


def remote_notes(root, directory, manifest, channels, execute, runner, helpers):
    """Read first; mutations are limited to notes on existing resources."""
    data = check_local(directory, manifest["version"], manifest["locales"], helpers.get("config"), root)
    version = manifest["version"]
    state_file = helpers["state_path"](root, version)
    journal_path = state_file.parent / "notes.json"
    now, atomic_json = helpers["now"], helpers["atomic_json"]
    def call(*args):
        return json.loads(runner.run(list(args), root))
    def rows(response):
        records = response.get("data", [])
        return records if isinstance(records, list) else [records] if records else []
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {"schema": 1, "operations": {}}
    targets = []
    invariants = []
    checkpoints = json.loads(state_file.read_text()) if state_file.exists() else {}
    github_helpers = helpers["github"]
    if execute:
        github_helpers.guard_writes(state_file.parent, allow="notes")
    if "asc" in channels or "testflight" in channels:
        if execute and (not checkpoints.get("version_id") or not checkpoints.get("apple_build", {}).get("id")):
            raise NotesError("Restore the exact Apple version/build checkpoint before notes writes")
        checkpoint_source = checkpoints.get("source", {}).get("commit")
        if checkpoint_source and checkpoint_source != manifest["source_commit"]:
            raise NotesError("Release source differs from the Apple checkpoint")
        versions = rows(call("asc", "versions", "list", "--app", helpers["app_id"], "--version", version,
                             "--platform", "IOS", "--include", "build", "--paginate", "--output", "json"))
        if len(versions) != 1 or versions[0]["attributes"].get("versionString") != version:
            raise NotesError("Resolve the exact existing Apple version before syncing notes")
        apple_version = versions[0]
        build_id = apple_version.get("relationships", {}).get("build", {}).get("data", {}) or {}
        build_id = build_id.get("id")
        if (checkpoints.get("version_id") and checkpoints["version_id"] != apple_version["id"] or
            checkpoints.get("apple_build", {}).get("id") and checkpoints["apple_build"]["id"] != build_id):
            raise NotesError("Apple version/build differs from the release checkpoint")
        def apple_identity():
            latest = rows(call("asc", "versions", "list", "--app", helpers["app_id"], "--version", version,
                               "--platform", "IOS", "--include", "build", "--paginate", "--output", "json"))
            if len(latest) != 1:
                raise NotesUncertain("Apple version identity changed during notes synchronization")
            current = latest[0]
            return (current["id"], current.get("relationships", {}).get("build", {}).get("data"), current["attributes"].get("releaseType"))
        invariants.append((apple_identity, (apple_version["id"], apple_version.get("relationships", {}).get("build", {}).get("data"), apple_version["attributes"].get("releaseType"))))
        if "asc" in channels:
            def asc_locales():
                return rows(call("asc", "localizations", "list", "--version", apple_version["id"], "--paginate", "--output", "json"))
            localized = asc_locales()
            if {r["attributes"]["locale"] for r in localized} != set(manifest["locales"]):
                raise NotesError("ASC locale coverage differs from the release inputs")
            for locale in manifest["locales"]:
                loc = next(r for r in localized if r["attributes"]["locale"] == locale)
                loc_id = loc["id"]
                old_fields = {k: v for k, v in loc["attributes"].items() if k != "whatsNew"}
                def locale_fields(target=loc_id):
                    attributes = next(r for r in asc_locales() if r["id"] == target)["attributes"]
                    return {k: v for k, v in attributes.items() if k != "whatsNew"}
                invariants.append((locale_fields, old_fields))
                expected = data["localizations"][locale].get("plainText", data["localizations"][locale]["releaseNotes"])
                operation = "asc:whatsNew" if len(manifest["locales"]) == 1 else "asc:whatsNew:" + locale
                targets.append((operation, loc_id, expected,
                    lambda target=loc_id: next(r for r in asc_locales() if r["id"] == target)["attributes"]["whatsNew"],
                    ["asc", "localizations", "update", "--id", loc_id, "--whats-new", expected, "--output", "json"]))
            def detail():
                records = rows(call("asc", "review", "details-for-version", "--version-id", apple_version["id"], "--output", "json"))
                if len(records) != 1:
                    raise NotesError("Existing App Review details are required")
                return records[0]
            review = detail()
            old_review_fields = {k: v for k, v in review["attributes"].items() if k != "notes"}
            invariants.append((lambda: {k: v for k, v in detail()["attributes"].items() if k != "notes"}, old_review_fields))
            expected = normalize((directory / "review-notes.txt").read_text())
            targets.append(("asc:reviewNotes", review["id"], expected,
                            lambda: detail()["attributes"].get("notes", ""),
                            ["asc", "review", "details-update", "--id", review["id"], "--notes", expected, "--output", "json"]))
        if "testflight" in channels:
            if not build_id:
                raise NotesError("TestFlight needs the exact build checkpoint")
            def beta():
                return rows(call("asc", "builds", "test-notes", "list", "--build-id", build_id, "--paginate", "--output", "json"))
            localized = beta()
            if {r["attributes"]["locale"] for r in localized} != set(manifest["locales"]):
                raise NotesError("Resolve the exact TestFlight locale coverage first")
            for locale in manifest["locales"]:
                suffix = "" if locale == manifest["locales"][0] else "." + locale
                template = directory / f"what-to-test{suffix}.template.txt"
                if not template.is_file():
                    raise NotesError(f"TestFlight needs {template.name}")
                loc_id = next(r for r in localized if r["attributes"]["locale"] == locale)["id"]
                expected = normalize((directory / f"what-to-test{suffix}.txt").read_text())
                operation = "testflight:whatToTest" if len(manifest["locales"]) == 1 else "testflight:whatToTest:" + locale
                targets.append((operation, loc_id, expected,
                    lambda target=loc_id: next(r for r in beta() if r["id"] == target)["attributes"].get("whatsNew", ""),
                    ["asc", "builds", "test-notes", "update", "--localization-id", loc_id, "--whats-new", expected, "--output", "json"]))
    if "github" in channels:
        try:
            repo = github_helpers.repository(root, helpers["git"])
        except github_helpers.GitHubError as error:
            raise NotesError(str(error)) from error
        tag = "v" + version
        remote_tags = helpers["git"](root, "ls-remote", "--exit-code", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
        if github_helpers.tag_source(remote_tags, tag) != manifest["source_commit"]:
            raise NotesError("GitHub tag does not identify the released source")
        publication_path = state_file.with_name("github.json")
        publication = json.loads(publication_path.read_text()) if publication_path.exists() else None
        if publication and (publication.get("intent", {}).get("repository", "").lower() != repo.lower() or
                            publication["intent"].get("source_commit") != manifest["source_commit"] or
                            publication["intent"].get("tag") != tag):
            raise NotesError("GitHub publication journal differs from the notes target")
        if publication:
            client = github_helpers.Client(root, runner)
            identity = client.preflight(write=execute)
            observed = client.release(repo, tag)
            if (publication["intent"].get("repository_id") not in (None, identity["repository_id"]) or
                    not observed or publication.get("release_id") not in (None, observed["id"])):
                raise NotesError("GitHub repository or Release ID changed from the publication journal")
            invariants.append((lambda: client.release(repo, tag)["id"], observed["id"]))
            invariants.append((lambda: client.preflight(write=False)["repository_id"], identity["repository_id"]))
        invariants.append((lambda: github_helpers.tag_source(helpers["git"](root, "ls-remote", "--exit-code", "origin",
            f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"), tag), manifest["source_commit"]))
        def github():
            return call("gh", "release", "view", tag, "--repo", "github.com/" + repo, "--json", "body,tagName,name,isDraft,isPrerelease")
        release = github()
        if release["tagName"] != tag or release["isDraft"] or release["isPrerelease"]:
            raise NotesError("Resolve the existing formal GitHub Release first")
        invariants.append((lambda: {k: v for k, v in github().items() if k != "body"},
                           {k: v for k, v in release.items() if k != "body"}))
        targets.append(("github:body", repo + ":" + tag, data["releaseNotes"], lambda: github()["body"],
                        ["gh", "release", "edit", tag, "--repo", "github.com/" + repo, "--notes-file", str(directory / "release-notes.md")]))
    for operation, target, expected, _, _ in targets:
        previous = journal["operations"].get(operation, {})
        if previous.get("status") == "pending" and (previous.get("body_sha256") != body_digest(expected) or previous.get("target") != target):
            raise NotesUncertain("Reconcile the previous notes write before changing its body or target")
    results = []
    for operation, target, expected, readback, argv in targets:
        current = normalize(readback())
        previous = journal["operations"].get(operation, {})
        desired_digest = body_digest(expected)
        if previous.get("status") == "pending" and (previous.get("body_sha256") != desired_digest or previous.get("target") != target):
            raise NotesUncertain("Reconcile the previous notes write before changing its body or target")
        if current != normalize(expected) and execute:
            if previous.get("status") == "pending":
                raise NotesUncertain("Previous write is unresolved; inspect it before retry")
            journal["operations"][operation] = {"status": "pending", "target": target,
                                                "body_sha256": desired_digest, "started_at": now()}
            atomic_json(journal_path, journal)
            try:
                runner.run(argv, root)
            except Exception as error:
                raise NotesUncertain(f"{operation} result unknown; read back before retry") from error
            try:
                actual = normalize(readback())
            except Exception as error:
                raise NotesUncertain(f"{operation} readback failed; pending receipt retained") from error
            if actual != normalize(expected):
                raise NotesUncertain(f"{operation} readback differs; pending receipt retained")
            current = normalize(expected)
        matched = current == normalize(expected)
        if execute and matched:
            if any(read() != before for read, before in invariants):
                raise NotesUncertain("Non-note fields changed during synchronization; inspect the remote resource")
            journal["operations"][operation] = {"status": "verified", "target": target,
                                                "body_sha256": desired_digest, "verified_at": now()}
            atomic_json(journal_path, journal)
            if operation == "github:body" and publication:
                previous_digest = publication["intent"].get("body_sha256")
                if previous_digest != desired_digest:
                    publication.setdefault("notes_history", []).append({"body_sha256": previous_digest, "updated_at": now()})
                    publication["intent"]["body_sha256"] = desired_digest
                    atomic_json(publication_path, publication)
        results.append({"channel": operation, "target": target, "matches": matched,
                        "body_sha256": desired_digest})
    return {"status": "verified" if all(r["matches"] for r in results) else "differences",
            "version": version, "channels": results, "body_sha256": data["bodySha256"],
            "executed": execute}

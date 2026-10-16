---
name: expo-ios-release
description: Prepare, execute, inspect, or resume Expo iOS releases using EAS Build, EAS Submit, App Store Connect, and GitHub version tags and Releases. Use for an iOS store release, review submission, release recovery, or notes synchronization; respect preparation-only and no-publish requests. Does not handle Android, EAS Update, or website deployment.
---

# Expo iOS release

Resolve the helper from this skill's `scripts/release.py`; the app repository is a separate `--root`. Installing or editing the skill does not authorize an app release.

For an authorized standard release, the outcome is one production build uploaded through EAS Submit, that exact build formally submitted for App Review, and a verified GitHub tag and Release at its source commit. App Store availability is a separate result. Use MANUAL release strategy unless the user or project plan specifies another strategy. Honor narrower endpoints such as planning, build-only, TestFlight upload, Apple-only submission, or GitHub-only backfill.

## Configure and prepare

1. Resolve the app's Git root. Read its existing release scope, project instructions and changelog rules. Read [project configuration](references/configuration.md) when onboarding an app or resolving identity, paths, profiles, checks, locales, or responsibility policy. Create `.expo-ios-release.json` using actual project values; preserve existing EAS/signing/ASC credentials. Ask only for consequential facts unavailable from the project or authenticated readback.
2. Run `python3 HELPER doctor --root ROOT`; add `--through github` for a complete release to check GitHub access too. It makes read-only service probes. `prepare`, `plan`, and `run`/`github` without `--execute` make no service calls. Dynamic Expo config is resolved through the installed local Expo CLI and may execute the project's config code.
3. Create `release_docs/VERSION/README.md` with the agreed release scope. For a new release run `prepare --root ROOT --version VERSION --base-ref PREVIOUS_RELEASE_SOURCE --locales ACTUAL_ASC_LOCALES`. Choose the previous released build's verified source. Existing releases resume their manifest instead of being scaffolded again.
4. Write the ordinary-user-facing canonical body in `release-notes.md` for the first selected locale and `release-notes.LOCALE.md` for every other locale. Follow project formatting rules; the helper does not impose a product-specific Chinese format or translate automatically. Provide `review-notes.template.txt` with exactly one `{{CHANGELOG}}` slot and verified reviewer instructions. Optional `what-to-test[.LOCALE].template.txt` files retain separate testing instructions. Run `notes-generate` and `notes-check` with the root/version.
5. Align app/package versions, commit only release-scoped files and freeze the candidate source. Run `candidate --manifest PATH` to bind that exact commit, then `checks --manifest PATH`. Read [execution and recovery](references/execution.md) before recording artifact/store evidence or running mutations. Keep runtime manifests, evidence logs and checkpoints private; commit only sanitized release scope, notes and summaries.

`acceptance_policy` is project-specific. Its default requires production/device evidence. Explicit `user-managed` policies record human ownership and skip only those gates; they do not claim automated acceptance. Preserve the project's established choice without adding routine second approvals. Code checks, exact production artifact acceptance and current exact-build store acceptance remain required.

## Execute and resume

Inspect `plan --manifest PATH --through github`. For an authorized standard release, use `run --manifest PATH --execute --through github`. The helper owns build, upload, staging, formal review, tag and Release publication. Existing/default `--through review` remains Apple-only; `build`, `upload`, and `stage` retain their narrower scopes. Read [GitHub publication](references/github-release.md) for identity rules, backfills and recovery.

Use `status --manifest PATH` to read this exact release's Apple/GitHub state, requested/completed endpoints and pending operations. Existing `status --root ROOT` retains the live/TestFlight overview. For an authorized GitHub-only backfill or recovery, use `github --manifest PATH --execute`; it reads the exact Apple build/version but makes no Apple writes and supports a later working HEAD.

When pending or blocked, resolve the reported phase and resume the same manifest. Keep the user informed during bounded status checks. Missing evidence remains missing. Credential/account/contract steps belonging to the human are genuine blocking inputs; deterministic supported metadata fixes within the release scope can proceed.

Writes are journaled before dispatch. After an uncertain result, read the exact remote IDs/state first and reconcile only that operation. Preserve the Build across download-client failures, upload recovery and GitHub failures. `adopt --build-id` binds an inspected exact candidate; `retry-upload` prepares recovery only after a verified failed submission. Never select `latest` as an identity substitute.

EAS Submit owns the binary upload; ASC owns staging and review. Keep EAS Build, EAS Submission, Apple Build and Apple Version IDs distinct. Formal submission requires WAITING_FOR_REVIEW, IN_REVIEW, or a verified later submitted state; READY_FOR_REVIEW is a draft.

For authorized notes-only changes, preview `notes-sync --root ROOT --version VERSION --channels asc github` and add `--execute` to update notes on existing resources. Select TestFlight explicitly if needed. This does not build, upload, submit, create/move tags, or change release strategy.

## Finish

Verify Apple's exact version/build/state and GitHub's exact tag source/body/flags independently. `complete` applies to the requested endpoint; a standard release requires completion through `github`. Save a sanitized release record and report the furthest verified result with remaining blockers. A GitHub Release does not establish App Store approval or availability. Public App Store release, live privacy/pricing changes, backend deployment and binary attachments require their own authorized scope.

Source/config changes invalidate candidate acceptance; notes changes invalidate metadata acceptance. Resume a failed phase from existing IDs rather than manufacturing a new successful release. Report tests using fake services as regression coverage, never as live release proof.

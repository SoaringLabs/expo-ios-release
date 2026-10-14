# Execution and recovery

`HELPER` is this installed skill's `scripts/release.py`, `ROOT` is the app repository, and `PATH` is `ROOT/release_docs/VERSION/release.json`. Use the actual configured paths. The POSIX helper needs Python 3.10+, Git, EAS CLI and [App Store Connect CLI](https://github.com/rorkai/App-Store-Connect-CLI); GitHub publication uses `gh`. It does not install/authenticate tools automatically.

```bash
python3 "$HELPER" doctor --root "$ROOT"
python3 "$HELPER" status --root "$ROOT"
python3 "$HELPER" prepare --root "$ROOT" --version VERSION --base-ref PREVIOUS_SOURCE --locales en-US
python3 "$HELPER" notes-generate --root "$ROOT" --version VERSION
python3 "$HELPER" notes-check --root "$ROOT" --version VERSION
python3 "$HELPER" plan --manifest "$PATH"
python3 "$HELPER" candidate --manifest "$PATH"
python3 "$HELPER" checks --manifest "$PATH"
python3 "$HELPER" run --manifest "$PATH" --execute --through review
```

These are a workflow reference, not a blindly replayed batch. Commit the intended source/config/version/notes before `candidate`; it resets candidate evidence. `checks` records actual exit codes and hashed evidence. Missing scope README or unresolved manifests block preparation. `run` without `--execute` and `plan` are offline; `doctor`, `status`, adoption and candidate recovery may read remote services. `checks` runs the project's configured commands, which may have their own effects.

## Evidence

`acceptance.json` groups are `code`, `artifact`, `store`, optional required `production`/`device`, and `review_feedback` for rejected reviews. A group requires `passed: true`, the exact `source_commit`, and nonempty evidence references `{"path": "repository-relative-file", "sha256": "actual-file-digest"}`. Evidence outside the repository or changed since hashing blocks. Artifact, store and device evidence also need the exact `eas_build_id`. Store evidence includes `metadata_digest` from `plan`; rejected-review feedback additionally includes `version_id` and a concrete `resolution_summary` with that current digest.

Inspect the IPA's version/build, bundle ID, entitlements, signing/provisioning and intended configuration before recording artifact acceptance. For store acceptance verify the staged exact build, locales/notes, reviewer access, screenshots, declarations and `asc validate` remediation. These are observed checks, not flags set solely to satisfy the helper. Record human-owned production/device responsibility separately when the project configured `user-managed`.

EAS uploads the binary once with `eas submit --id BUILD_ID`. Independent `asc` authentication is required for Apple metadata/review even when EAS managed upload credentials work. Keep API private keys in Keychain/secure files; use installed `asc auth login --help`. Ask the human only for credential/account/contract steps they must do. Never export EAS managed private keys.

The helper creates a new version by copying unchanged textual metadata from the verified previous live version, excluding old What's New. It manages only this release's notes, build attachment and release strategy. Review contact/access, screenshots, category, privacy, prices and age ratings remain verified project/remote data. Fix missing fields through supported tools within the authorized scope, then read them back.

Readiness blocks errors and actionable warnings. The sole conditional exception is `subscriptions.images.recommended` after a fresh promoted-purchases query verifies there are no promoted purchases. All other warnings remain blocking. Read installed command help before remediation; installing this skill does not authorize live privacy/pricing changes.

## Checkpoints

Resolve the private Git directory with `git rev-parse --absolute-git-dir`. `expo-ios-release/VERSION/state.json` holds sanitized identities, source/config digests and mutation receipts, guarded by an exclusive lock and atomic writes. It stores no raw CLI responses, keys or signed download URLs. Each mutation is pending before dispatch; uncertain outcomes are never blindly replayed.

| Pending operation | Recovery |
| --- | --- |
| Build dispatch | Inspect exact candidate EAS builds and use `adopt --manifest PATH --build-id ID`; verify source/profile/app/version. It never adopts `latest`. |
| Upload | Read the exact Apple marketing version/build and EAS worker status. Matching Apple Build completes upload; absence alone does not establish a failed request. |
| `stage:create`, `stage:metadata`, `stage:attach`, `stage:notes`, `stage:strategy` | Read that operation's exact resource. Matching result completes only its receipt. A mismatch stays uncertain; inspect and complete supported remediation before resuming. |
| Review | Read the bound Apple Version, attached Build and submitted state. WAITING_FOR_REVIEW/IN_REVIEW or verified later state completes it; a draft remains uncertain. |

`retry-upload --manifest PATH` requires a bound ERRORED/CANCELED submission, valid exact Build and no unresolved write. It archives the previous receipt and reuses the Build. If Apple already has it, upload is retained instead of repeated.

`candidate --manifest PATH` handles a source fix for the same marketing version. It refuses unknown writes, active builds/uploads and active or approved reviews. It archives history, binds the new clean source and clears all acceptance. Resolve/withdraw active reviews through supported tools within authorized scope first; rejected resubmissions require current feedback evidence.

Source/config changes invalidate candidate evidence. Notes/strategy changes invalidate metadata evidence. A timeout downloading an artifact changes the download client, not the Build. Pending jobs return promptly; the caller polls in bounded intervals and reports progress.

## Notes-only updates

`notes-sync --root ROOT --version VERSION --channels asc github` previews existing-resource differences. `--execute` changes only notes and verifies identities plus non-note fields. TestFlight is an optional explicit channel. It needs exact existing Apple version/build checkpoints; GitHub readback proves the peeled tag matches `source_commit`.

Notes writes have independent `notes.json` receipts, body hashes and remote target IDs. Lost responses reconcile through readback without duplicate writes. Changed body/target cannot overwrite a pending operation. Notes refresh does not confer full store acceptance or resubmit a review. Backend deployment and endpoint schemas are project-owned.

Exit codes are 0 for a successful/plan/pending result, 2 for blocked/differing inputs, and 3 for uncertain write outcomes. Read the returned `status` and `phase`; process success alone is not completion.

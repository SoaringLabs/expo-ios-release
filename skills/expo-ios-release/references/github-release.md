# GitHub tag and Release

The helper implements this phase in `scripts/github_release.py`. A standard authorized release uses:

```bash
python3 "$HELPER" run --manifest "$PATH" --execute --through github
```

For an authorized GitHub-only backfill or recovery:

```bash
python3 "$HELPER" github --manifest "$PATH"
python3 "$HELPER" github --manifest "$PATH" --execute
python3 "$HELPER" status --manifest "$PATH"
```

The first command is an offline preview. GitHub-only execution reads the bound EAS/Apple resources and makes no build, upload, staging or review writes. A later working HEAD is supported: the tag always resolves to manifest `source_commit`. `run --through review` and the default endpoint remain Apple-only.

## Binding and verification

Publication requires the finished exact EAS Build, valid Apple Build and formally submitted exact Apple Version. Code/artifact/store acceptance and any required production/device evidence must still match the source/build; current store acceptance must match the canonical metadata digest and staged remote fields. A draft Apple version is insufficient.

The source commit must exist locally and its package/static app version must match the manifest. A dynamic Expo config is not evaluated from the current HEAD during backfill. The title uses the source's static app name or repository name; an optional manifest `github.title` preserves an established convention.

Origin fetch and its single push destination must name the same GitHub.com repository. An authenticated API read binds the numeric repository ID and verifies write access. GitHub API calls explicitly select github.com. Permission/network failures are blockers, not evidence that a tag or Release is missing. Only `git ls-remote` exit 2 or a real HTTP 404 after repository verification establishes initial absence.

The helper reads local and remote tags before writing. New tags are annotated and point to the full source commit. Existing lightweight or annotated tags are reusable if their resolved commit matches. Conflicting refs are preserved and block publication. An existing Release without a verified matching tag also blocks.

The Release uses the canonical `release-notes.md` via `--notes-file`, with `--verify-tag`, a frozen title, non-draft/non-prerelease flags and explicit Latest selection. Full paginated formal `vX.Y.Z` history is compared numerically: a newer version is Latest; an older backfill is not. Ambiguous formal tag conventions block selection. Exact body, tag source, ID, URL/date and Latest are read back after publication. `target_commitish` is not source proof. Source archives are automatic; IPA/dSYM attachments require separate scope.

## Journal and recovery

The helper owns `.git/expo-ios-release/VERSION/github.json` under the same lock as Apple and notes operations. It records repository identity, source, exact EAS/Apple IDs, tag/title, canonical notes path/hash, publish flags, and verified remote identity. Each of `tag:create`, `tag:push` and `release:create` records pending intent before dispatch and verification after readback. Private journals contain no CLI dumps, credentials or signed artifact URLs.

Resume with the same manifest and command. A matching result reconciles only the pending operation, then missing later steps continue. If a response was lost after the remote effect succeeded, no duplicate mutation is dispatched. Absence after an uncertain write does not prove failure and remains blocked for inspection; preserve the original intent until a definite outcome is established. Changed source, target, title or body cannot replace an unresolved intent. Existing different Release content is a conflict; authorized text-only changes use `notes-sync`.

`notes-sync` shares source/tag identity checks and the release lock. Its verified updates refresh the publication journal's current body hash while preserving the original publication hash and change history. A notes update does not confer store acceptance. Pending notes operations block publication and vice versa.

GitHub failure preserves Apple receipts and the original Build. The helper reports the GitHub blocker and can resume publication without another Apple write. Standard completion means formal App Review submission plus verified GitHub publication. Apple approval and public App Store availability remain separate results.

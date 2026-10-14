# GitHub tag and Release

Continue here after formal App Review submission in an authorized standard release, or for an authorized GitHub-only backfill. Planning-only tasks stop with a plan. A backfill reuses the existing binary/source and never restarts Apple release mutations.

## Bind and prepare

Read the manifest, acceptance and private checkpoint. Resolve VERSION, full SOURCE=`source_commit`, TAG=`vVERSION`, and the exact EAS/Apple IDs. Verify the EAS Build is FINISHED and matches source/app/project/profile/version/distribution. Freshly read Apple's exact Version and attached Build; require a submitted or later state supported by the helper. READY_FOR_REVIEW is insufficient.

Resolve SOURCE locally with `git rev-parse --verify "$SOURCE^{commit}"`. Inspect its app/package version. For dynamic Expo config, reuse the build's verified effective configuration; HEAD may contain later documentation and is not the tag target.

Verify the origin fetch and push destinations identify the same GitHub repository and confirm access with `gh repo view "$REPO" --json nameWithOwner`. Read installed release command help. Preserve existing authentication. Derive the title from the app's established convention, otherwise `APP_NAME vVERSION`. Standard output is non-draft and non-prerelease.

Use the reviewed `release-notes.md` directly with `--notes-file`. Verify `notes-check` locally before publication. Keep technical evidence, Apple state/availability, credentials, local paths and signed artifact links in a separate sanitized release record. Source archives are automatic; IPA/dSYM publication needs separate scope.

Maintain an independent `.git/expo-ios-release/VERSION/github.json` journal under the existing release lock. Bind SOURCE, exact build/version IDs, canonical notes path/hash, tag, title and repository. Before each mutation save pending intent; after it save verified result/time. Preserve Apple receipts and reuse saved publication intent on resume.

## Publish conditionally

Read the exact remote resources first:

```bash
git ls-remote --exit-code origin "refs/tags/$TAG" "refs/tags/$TAG^{}"
gh api "repos/$REPO/releases/tags/$TAG"
```

For `ls-remote`, exit 2 means no matching tag; other failures are unresolved. Only a real HTTP 404 after repository access verification proves no Release. For annotated tags compare the remote peeled `^{}` SHA with SOURCE; for lightweight tags compare direct SHA. Inspect existing local tag's resolved commit too. Conflicting tags remain intact and block publication.

When both local and remote tags are absent, create an annotated tag on SOURCE. Push only the exact matching local tag when remote is absent:

```bash
git tag -a "$TAG" "$SOURCE" -m "$TITLE"
git push origin "refs/tags/$TAG:refs/tags/$TAG"
```

These are individual conditional actions. Reread the remote tag's commit before creating a Release. Compare formal version history numerically to choose Latest: newer release `true`, older backfill `false`. Resolve truncated history or ambiguous version conventions before selecting Latest.

For a verified absent Release:

```bash
gh release create "$TAG" --repo "$REPO" --verify-tag \
  --title "$TITLE" --notes-file "$NOTES" --latest="$LATEST"
```

[`--verify-tag`](https://cli.github.com/manual/gh_release_create) prevents the CLI from creating a missing tag from the default branch. Verify remote tag source again, then read:

```bash
gh release view "$TAG" --repo "$REPO" \
  --json tagName,name,body,isDraft,isPrerelease,url,publishedAt,assets
```

Require intended tag/title/body, non-draft/non-prerelease state, published URL/date and exact tag commit=SOURCE. `targetCommitish` may be a branch and is not commit proof. Check `repos/$REPO/releases/latest` when Latest matters, and run `notes-check --channels github` for body/source readback. Record GitHub URL/source separately from Apple review and App Store availability.

## Resume

After an uncertain push/create, preserve pending intent and reread that tag/Release. Matching result completes the operation; continue only missing actions. Different body/title/flags are a conflict to inspect rather than silently overwrite. Absence after an uncertain write remains uncertain until a definite failed outcome is established. GitHub trouble never triggers another EAS build/upload or Apple review submission.

# Project configuration

Create `.expo-ios-release.json` in the **app** Git root. Identity values are explicit; obtain the EAS project ID and bundle identifier from resolved Expo config and the ASC app ID from the existing EAS submission profile/authenticated Apple readback. This skill never creates an app or signing key during onboarding.

```json
{
  "schema": 1,
  "app_id": "1000000001",
  "project_id": "11111111-1111-4111-8111-111111111111",
  "bundle_id": "com.example.demo",
  "build_profile": "production",
  "submit_profile": "production",
  "release_docs": "release-docs/versions",
  "artifact_dir": ".artifacts",
  "checks": [["npm", "run", "typecheck"], ["npm", "run", "lint"], ["npm", "test"]],
  "acceptance_policy": {"production": "required", "device": "required"},
  "tool_versions": {"eas": "22.4.0", "asc": "5.14.1"}
}
```

These IDs are synthetic examples. Replace all identity values and checks with the app's actual values. Store credentials through the tools' existing credential mechanisms, outside this file.

| Field | Contract |
| --- | --- |
| `app_id`, `project_id`, `bundle_id` | Required ASC app ID, EAS project UUID, and iOS bundle identifier. Must match resolved app/EAS config and remote build identities. |
| `build_profile`, `submit_profile` | Required existing profile names; may differ. Inheritance is resolved, including iOS overrides. |
| `release_docs` | Repository-relative version directory; defaults to `release-docs/versions`. |
| `artifact_dir` | Repository-relative local evidence directory; defaults to `.artifacts`. |
| `notes_adapter` | Optional read-only command argument array. Appended arguments are the absolute source path and locale; stdout must contain schema 1 `markdown` and `plainText`. Runs before generation, local/remote notes checks, staging/publication checks and candidate freezing. See [projection contract](changelog.md). |
| `checks` | Required nonempty arrays of command arguments, run without a shell. Use concrete test paths when your runner requires them. |
| `acceptance_policy` | Each of `production` and `device` defaults to `required`. Use `user-managed` only for an established human-owned responsibility; the helper skips that gate without claiming it passed. Frozen into the manifest. |
| `tool_versions` | Exact EAS/ASC versions. Defaults above match the tested CLI contracts. Overrides require compatibility validation against installed help and regression coverage. |

The build profile must use the EAS `production` environment, store distribution, no development client, and remote iOS build-number auto-increment (`true` or `buildNumber`). Set `cli.appVersionSource` to `remote`. The submission profile must pin the same `ascAppId`. The app and package marketing versions must agree.

Pin `packageManager` in `package.json` (`npm`, `pnpm`, `yarn`, or `bun`). Local versions must match that pin and any corresponding EAS profile pin. When the EAS profile pins Node, the local Node version must also match. `app.config.ts`/`app.config.js` are resolved with the installed local `node_modules/.bin/expo config --json`; missing runtime blocks instead of trusting stale `app.json`. Resolve config in the same environment intended for the production candidate.

Add anchored exclusions to `.easignore` for the top-level release docs directory, `artifact_dir`, and `.agents`. Example:

```gitignore
/release-docs
/.artifacts
/.agents
```

The helper conservatively blocks `.easignore` negations; reconcile them before relying on exclusions. Use `.gitignore` for local evidence and runtime manifests:

```gitignore
/.artifacts/
/release-docs/versions/*/release.json
/release-docs/versions/*/acceptance.json
```

Adapt those paths if configured differently. Commit the project configuration, release scope and sanitized notes. Artifacts, credential files, runtime evidence and Git checkpoints remain local.

## Locale inputs

The first locale given to `prepare --locales` owns `release-notes.md`, the GitHub Release body and the review notes' `{{CHANGELOG}}`. Every other locale needs `release-notes.LOCALE.md`. Generation validates all translations/templates before writing projections. With a configured adapter, each locale's Markdown is preserved and its plain-text projection fills that locale's What's New; without an adapter the body is copied unchanged. Reviewer instructions are one shared Apple review-notes field.

Optional TestFlight templates use `what-to-test.template.txt` for the first locale and `what-to-test.LOCALE.template.txt` for the others, each with one slot. TestFlight synchronization requires a template for every configured locale and matching existing remote localizations; locale creation is an explicit remediation step, not an implicit translation fallback.

The adapter and project rules own content format; the generic tool does not hard-code product categories or summarize source diffs.

Generated files are `metadata/version/VERSION/LOCALE.json`, `review-notes.txt`, optional `what-to-test[.LOCALE].txt`, and `release-notes.json` containing each canonical body and its normalized hash, plus `format: markdown`, `plainText` and `plainTextSha256` when an adapter is configured. CRLF/LF and final-newline differences normalize for comparisons. There is no product backend integration; consuming/deploying this data belongs to the app project.

## Existing releases

The generic manifest is schema 2 and checkpoints live in `.git/expo-ios-release/VERSION/`. Product-specific manifests/checkpoints are not automatically migrated. Finish or resume them with their original helper, or explicitly migrate after checking exact source/build/version IDs and current remote state. Installing this skill preserves the old workflow.

GitHub publication requires an authenticated `gh` with repository write access, one matching GitHub.com origin fetch/push destination and full paginated release history. It does not add `gh` to the existing EAS/ASC version-pin configuration or require it for Apple-only endpoints. The helper verifies the installed GitHub CLI contract by using its API and explicit publish flags.

For an established Release title, optionally add `"github": {"title": "Example App v2.0.0"}` to that version's private schema-2 manifest before publication. Otherwise the title uses the source commit's static Expo app name, or the repository name, followed by `vVERSION`. Dynamic Expo config is not evaluated from a later HEAD during backfill; the previously verified EAS source/build identity is reused. Title, repository identity, source, exact build/version IDs, body hash and publication flags are frozen in `github.json` before writes.

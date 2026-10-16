# Expo iOS Release

A reusable agent skill for Expo iOS releases: EAS Build → EAS Submit → formal App Review → GitHub tag and Release.

Project identities, profiles, quality checks, paths, locales and acceptance ownership come from the app's configuration. The workflow retains exact-source/build verification, evidence gates and recovery for uncertain writes. It supports planning, narrower release endpoints, notes-only synchronization and GitHub backfills.

## Install

Clone the repository and copy **only the skill folder** into your agent's skill directory. For Codex:

```bash
git clone https://github.com/SoaringLabs/expo-ios-release.git
mkdir -p ~/.codex/skills
cp -R expo-ios-release/skills/expo-ios-release ~/.codex/skills/
```

If that skill already exists, inspect/back up it before updating. A project-local installation can instead copy the folder into `.agents/skills/`. Restart or refresh the agent's skill discovery as required by your client. Installation does not start a release.

Invoke `$expo-ios-release` with your app repository and intended version, for example: “Prepare version 2.0.0 without publishing” or “Execute the standard iOS release for version 2.0.0.” State a narrower endpoint when needed.

## Configure an app

Copy [the synthetic project configuration](examples/expo-ios-release.json) to `.expo-ios-release.json` in the app root and replace its IDs and checks. Follow [configuration](skills/expo-ios-release/references/configuration.md) for EAS requirements, archive/Git exclusions, locales and responsibility policy. Existing release checkpoints are not automatically migrated.

The POSIX helper requires Python 3.10+, Git, EAS CLI and [App Store Connect CLI](https://github.com/rorkai/App-Store-Connect-CLI). `gh` is required for GitHub publication. The default CLI contracts were verified locally with EAS CLI 22.4.0 and ASC CLI 5.14.1; configure other versions only after checking compatibility. App package managers can be npm, pnpm, yarn or bun. Credentials stay in the tools' existing secure mechanisms.

Read [the skill](skills/expo-ios-release/SKILL.md), [execution/recovery](skills/expo-ios-release/references/execution.md), and [GitHub publication](skills/expo-ios-release/references/github-release.md) for phase-specific instructions. Apple's public availability and GitHub publication are independent; default Apple release strategy is MANUAL.

## Verify

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s skills/expo-ios-release/tests -v
```

Tests use temporary Git repositories, local bare remotes and fake services. They cover independent app identities, configured profiles/checks, localized notes, exact-build gates, single upload/submission/publication and uncertain-write recovery. They do not validate a live app candidate or submit real releases. CI runs this suite on Linux and macOS.

The helper's complete endpoint is `run --manifest PATH --execute --through github`. Existing/default `--through review` remains Apple-only. `github --manifest PATH --execute` backfills or resumes GitHub publication without Apple writes, even after HEAD advances. `status --manifest PATH` reads the exact Apple/GitHub release and pending operations; `status --root ROOT` keeps the live/TestFlight overview. All publication steps use the release lock and private journals; preview commands perform no service calls.

Current scope is iOS with cloud EAS builds and the Apple/GitHub CLI contracts above. Android, EAS Update, website deployment, backend schemas, public App Store rollout and binary attachments are outside the default workflow.

MIT licensed. This is a SoaringLabs project, independent of Expo, Apple and OpenAI.

# Changelog authoring and channel projection

Read this when preparing or revising release notes. The agent composes the prose; `notes-generate` only derives files from the completed canonical body.

## Establish the actual changes

1. Verify the previous publicly released version/build and its source for the intended platform/channel. Record that baseline and the candidate source in the release scope/record. Do not substitute the highest tag, latest GitHub Release or newest TestFlight build.
2. Inspect the actual source diff. Use commit titles, PRs, tasks and scope documents as discovery aids, then verify each proposed statement against the final behavior. Account for reverted changes, incomplete work, feature flags and required backend availability; do not announce unavailable behavior.
3. Group commits into user-facing changes. Follow the project's audience, terminology, inclusion and formatting policy. Keep user-visible benefits and important data/payment/compatibility/action requirements; leave implementation and verification details in the scope/QA record. Do not turn noisy commit history into public prose or invent benefits to fill categories.
4. Write one canonical body per actual selected locale. Check each item against its evidence and prepare explicit translations with the same meaning, conditions, grouping and order. Retain necessary reviewer/testing instructions separately.
5. Generate and check projections before freezing the candidate. Changes to the source require regenerating and rechecking projections and affected metadata evidence. Existing/pending remote writes must be reconciled before replacement.

Project-specific punctuation, categories, tone and formatting remain in the application's rules and adapter. They do not override the release tool's identity, metadata limits, checkpoints or publication requirements.

## Optional project adapter

Configure `notes_adapter` as a command argument array in `.expo-ios-release.json`. The helper executes it without a shell from the application root, appending the absolute source path and the locale. It must be a trusted, read-only local command with no service calls; it may reject project-invalid content but must not rewrite the source or compose new prose.

Return JSON on stdout:

```json
{"schema": 1, "markdown": "the normalized canonical Markdown", "plainText": "the deterministic plain-text projection"}
```

`markdown` must equal the source after CRLF/LF and trailing-newline normalization. The project's adapter and tests ensure `plainText` retains the exact words, conditions, item order and numbering, changing only formatting. Validate both channel limits and the complete reviewer/test templates. An adapter rejection, bad output or changed Markdown stops generation before writing projections.

With an adapter, GitHub uses the source Markdown. ASC What's New, the review template slot and optional TestFlight template slots use `plainText`. `release-notes.json` records the Markdown and plain-text bodies and their independent hashes for each locale. A Markdown hash is not the hash of a plain-text Apple field.

Without an adapter, existing projects retain opaque text passthrough and the original artifact structure. This is an explicit project opt-in, not an automatic migration of historical releases. Product backend import, update-target selection and deployment remain application responsibilities.

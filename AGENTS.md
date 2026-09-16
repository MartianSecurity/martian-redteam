# Repository instructions

Read README.md, then docs/architecture.md, docs/how-it-works.md and docs/usage.md as needed before changing this repository.

This is a local SDK/CLI, not the agent server. Client simulator/evaluator contracts must match martian-agents. The local callback runs in the caller process; replies go to the remote evaluator. Keep gate defaults and configurable exceptions accurate. Use offline config validation for docs checks; do not call the hosted API with local credentials unless the user requested a live run.

## Working preferences

- Keep changes within the requested scope and preserve unrelated dirty work.
- Do not create test cases or test infrastructure unless explicitly requested. Run existing checks appropriate to the change.
- Do not add fallback logic or alternate recovery behavior unless explicitly requested.
- Preserve the approved light desktop UI. Do not design or check mobile layouts.
- Inspect this repository's actual branch and git status; do not rely on an old branch name in a document.
- Do not commit, push or deploy unless requested. Do not expose secrets or raw customer data in docs.

## Documentation is part of the change

Before marking a task complete, check whether it changes APIs, configuration, startup commands, dependencies, schema/data ownership, user workflows, security boundaries, or what is implemented. If so, update the affected documentation in the **same change**, without waiting for a separate docs request.

- Keep README.md short: purpose, current boundaries and navigation.
- Keep architecture, how-it-works and usage explanations in their canonical linked guides. Update an existing section instead of creating another status/plan document.
- Treat source code, configuration and migrations as evidence of implemented behavior. Label proposals, mock data, historical results and unverified deployment assumptions explicitly.
- Update both ends of an integration contract when both repositories are in the workspace. Otherwise identify the follow-up repository and contract change.
- Remove superseded instructions and fix incoming links. Preserve unique evidence/research and unrelated uncommitted files. Do not keep duplicate operational instructions in agent files.
- Check changed links, paths and commands against the repository; run git diff --check. Do not run paid models, launch environments or deploy just to verify prose.
- In the final handoff/PR, state which docs changed and what was verified. If no docs were needed, give the concrete reason.

CLAUDE.md points here so both entry points share these rules. These instructions and the PR checklist make docs review part of future work; they do not guarantee that unreviewed prose is current.

# Contributing

Thank you for helping with the Martian SDK. [README.md](README.md) says what is in it, and the
[documentation index](docs/README.md) lists the guides. The project is distributed under
Apache-2.0 ([LICENSE](LICENSE)), and every contribution needs a signed
[Contributor License Agreement](#contributor-license-agreement) with MartianSecurity.

## Report a vulnerability privately

Do not report a vulnerability in an issue or a pull request. Use GitHub's private vulnerability
reporting, as [SECURITY.md](SECURITY.md) describes: the repository's **Security** tab, then
**Report a vulnerability**.

## Issues

Open an issue with the Bug, Feature or Task form before you start anything larger than a small
fix, so the change has an agreed purpose before you write it. Leave out credentials, provider keys
and customer data.

## Pull requests

- Fork the repository and branch from `main`. Keep one change to a pull request, and link the
  issue it addresses.
- Say in the description what changed and which checks you ran, with their results.
- CI (`.github/workflows/ci.yml`) runs on every pull request. A first-time contributor's run waits
  until a maintainer approves it.
- A maintainer reviews and merges. Keep commits to the purpose of the pull request; unrelated work
  goes in a pull request of its own.

## Checks to run locally

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/). From the repository root, as
CI runs them:

```bash
uv sync --locked --extra evals
uv run --extra evals ruff format --check src tests examples scripts
uv run --extra evals ruff check src tests examples scripts
uv run --extra mcp --extra evals mypy src
uv run --extra evals --extra mcp pytest -q -n auto
uv run --extra evals python -m compileall -q src
```

The store tests also run on Postgres when `MARTIAN_TEST_POSTGRES_URL` is set. CI runs further jobs
(packaging, MLflow compatibility, the actions); [development](docs/development.md) describes each,
the test rules and the repository layout.

## Tests with every change

- A change comes with tests for the behaviour it adds or changes, and a bug fix with a test that
  fails without the fix.
- Tests use no live network and no real keys: Backend, judges and providers are loopback fixtures
  or `respx` ([tests](docs/development.md#tests)).
- A catalog change needs its catalog test and, when ids, types or gate flags change, a new
  `CATALOG_VERSION` ([adding a vulnerability](docs/development.md#adding-a-vulnerability)).

## Docs in the same change

- When a change alters CLI flags, configuration, request or response shapes, catalog ids, gate
  behaviour, action inputs or what is implemented, update the guide in `docs/` in the same pull
  request. Rewrite the affected section rather than adding a dated note.
- Keep [README.md](README.md) short; details belong in `docs/`.
- Pin every GitHub Action to a full commit SHA, never a tag such as `@v4` or a branch.

## Contributor License Agreement

Before a contribution is merged, the author of every commit in the pull request must have signed
the [Individual Contributor License Agreement](cla/individual.md) with MartianSecurity. It is
based on The Apache Software Foundation's Individual CLA V2.2. In short, you grant MartianSecurity,
and the recipients of software it distributes, a perpetual, worldwide, royalty-free licence to your
contributions under your copyright, and under your patent claims that they necessarily infringe;
you keep ownership of them; you state that they are your original work, or mark work that is not,
and that you are entitled to grant the licence, with your employer's permission if it has rights
to your work; and you are not expected to support them. The agreement itself is what you sign;
read it, not this summary.

### How signing works

1. When you open a pull request, or push to it, the `CLA` check runs
   (`.github/workflows/cla.yml`, the CLA Assistant Lite action). If an author of a commit in the
   pull request has not signed, the check fails and the bot comments on the pull request.
2. The bot's comment thanks you, links the individual agreement and the corporate one, and gives
   the line to post, between two rules:

   ```text
   I have read the CLA Document and I hereby sign the CLA
   ```

   When the pull request has several authors, the comment says how many have signed and names
   those who have not. It ends with a note that commenting `recheck` runs the check again.
3. Each author named in it posts that line as a comment on the pull request, exactly and with
   nothing else in it, from their own GitHub account.
4. The bot records the signature, changes its comment to say that all contributors have signed,
   and runs the failed check again. Comment `recheck` if the check does not pass after that.

One signature covers all your later pull requests to this repository. Each commit's author must
be a GitHub account: commit with an e-mail address that is
[linked to your account](https://docs.github.com/en/pull-requests/committing-changes-to-your-project/troubleshooting-commits/why-are-my-commits-linked-to-the-wrong-user).
The bot lists an author it cannot match to an account as not a GitHub user, and the check cannot
pass until the commits are linked or rewritten.

The signature is public. The bot writes your GitHub user name and numeric account ID, the
comment's ID, when you posted it and the pull request's number to `signatures/cla.json` on this
repository's `cla-signatures` branch. When a pull request is closed, the bot locks its
conversation, so signature comments stay as they were signed.

Dependabot, GitHub Actions and MartianSecurity's maintainer account are on the check's allowlist
and are not asked to sign.

### If you contribute for your employer

If your employer has rights to what you create, section 4 of the individual agreement needs its
permission, its waiver of those rights, or its signature on the
[Software Grant and Corporate Contributor License Agreement](cla/corporate.md). A corporation signs
the corporate agreement by a pull request that adds it to [cla/corporations.md](cla/corporations.md),
opened by someone authorized to sign for it, with the entry
[How to sign](cla/corporate.md#how-to-sign) gives. The agreement is on file once a maintainer
merges it. The corporation changes its designated employees or its Point of Contact by a later
pull request to its entry. Designated employees still sign the individual agreement on their own
pull requests.

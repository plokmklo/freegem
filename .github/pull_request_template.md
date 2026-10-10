<!--
  Keep the PR title in conventional-commit form: fix:, feat:, docs:, chore:,
  refactor:, test:. The CI gates run ruff check, ruff format --check, mypy,
  and the test suite -- run them locally before pushing.

    pip install -e ".[dev]"
    ruff check .
    ruff format --check .
    mypy freegem --ignore-missing-imports
    pytest
-->

## Kind of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor, no behaviour change
- [ ] Build / packaging / CI
- [ ] Tests only

## What changed and why

<!-- Lead with the reason. If the "what" is not obvious from the diff, explain it. -->

## How you verified it

<!-- Commands and their output. "It works on my machine" is not verification. -->

## Breaking changes

<!-- Config keys, env vars, CLI flags, endpoints. Write "none" if none. -->

## Gates

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `mypy freegem --ignore-missing-imports` passes
- [ ] `pytest` passes
- [ ] Tests added for behaviour changes; no new test hits the network

## Hygiene

- [ ] No credential, cookie value, SAPISID, or XSRF token is included
- [ ] No new third-party dependency in the core (extras only)
- [ ] `CHANGELOG.md` updated under `[Unreleased]` if user-visible
- [ ] No authentication or API-key support added

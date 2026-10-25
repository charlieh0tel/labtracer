# Agent Instructions for labtracer

- Before committing: `uvx ruff check --preview . && uvx ruff format --check .`
  must pass. Fix findings rather than adding `# noqa`.
- Be Pythonic: comprehensions, `with`, unpacking, `dict.get`, `any`/`all`,
  and the stdlib before a hand-rolled helper.
- DRY: reuse or extend existing helpers instead of near-copies. Don't
  abstract something with one caller.
- Catch narrow exceptions only, and log anything skipped to stderr. "Could not
  check" must never render the same as "nothing found".
- Pin every `uses:` to a full SHA with the tag in a comment. Never pipe a
  remote script into a shell.
- Commit subjects are plain imperative with no prefix; the body says what was
  wrong and why. One change per commit.
- When starting a large change, refactor and commit first.
- Do not add Claude attribution to commit or PR bodies.


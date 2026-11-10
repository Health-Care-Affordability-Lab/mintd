# CLAUDE.md

`vocab.md` is the map of the domain; `mintd <command> --help` is the map of the CLI.

## Commands

- Set up with `uv sync --extra dev`.
- Before calling a change done, run what CI runs: `uv run ruff check src tests`,
  `uv run mypy src/mintd`, `uv run pytest`.

## Grounding

A claim about what mintd does — in an issue, a PR, a doc, a review or a code comment —
rests on something read or run in this session, not on memory, a name, or another doc.

- Evidence is the code in `src/`, the tests, and the CLI itself (`--help`, a real run).
  Cite it as `file:line`, or as the command and the output it gave.
- `README.md`, `vocab.md`, docstrings and comments describe the code but can drift. When
  one disagrees with what the code does, the code wins and the text is a bug.
- "X exists" means X is defined — a `class`, a `def`, a subparser — not mentioned.
- A bug is reproduced before it is fixed. The fix ships with a test that fails without
  it: revert the fix and watch the test go red.
- Say what was not verified. "Unverified" is an acceptable answer; a confident guess
  is not.

## Issues

One issue per unit of work, titled with the observable problem in one line.

- **Problem** — a concrete example first: the command run, what happened, what should
  have happened.
- **Done when** — checks anyone can run: the test that fails today, the mutation that
  must turn it red, the live command and the output it must give.
- **Approach** — the plan, decisions taken, open questions. Optional until work starts.
- **Links** — related issues and the evidence they rest on.

Closing an issue records how it was verified and which follow-ups were filed.

## Docs stay current

- `README.md` and `vocab.md` describe the code as it is today. A change that adds,
  renames or removes a command, flag, public class or domain term updates them in the
  same PR. `tests/test_docs.py` checks the README command table against the parser.
- Tracked docs carry no history or plans: no "slice N", "not built yet" or "was
  dropped". A decision and its reasons go in the PR description and in the docstring
  of the code it shapes.

## Commits and PRs

- Title: `<area>: <what the user now sees>`, e.g.
  `publish: the not-valid-semver error now names the value to write back`.
- PR body: TLDR, Problem (a concrete failing command), Solution, Reviewer notes.
- Every merged PR is a release. mintd's version comes from git tags (setuptools-scm),
  and merging a PR into `main` tags its merge commit (`.github/workflows/tag.yml`):
  the next patch version, or the next minor when the PR has the `minor` label. Add
  `minor` when the PR adds, renames or removes a command, flag or default users see.
  Never tag by hand.

## Tests

- Test doubles follow `tests/test_substrate_rules.py`. Read its docstrings before
  adding a fake, a monkeypatch or a lint exemption.
- A known, unfixed defect is a `pytest.mark.xfail(strict=True)` test whose docstring
  is the spec.

## Hard rules

- The repo is public. Code, tests, docs, commits and PRs never cite a gitignored or
  local-only file or a private tracker; each change explains itself.
- Never delete anything on S3: no `aws s3 rm`, no `dvc gc -c`, no cleanup of stale
  objects. A plan that needs a deletion says so and stops.
- Spawn processes through `mintd._subprocess.run_streaming`; ruff's TID251 rejects a
  raw `subprocess` import outside the seams listed in `pyproject.toml`.

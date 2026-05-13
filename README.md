# Slice 1 scaffold

Starter scaffold for slice 1 of the mintd rewrite. See `SLICE-1.md` for the full spec.

## What's here

```
slice-1-scaffold/
├── SLICE-1.md                       # the spec — read this first
├── pyproject.toml                   # minimal: pydantic + pytest
├── README.md                        # this file
├── src/mintd/
│   ├── __init__.py
│   ├── model.py                     # Metadata + sub-models — TODOs marking what to implement
│   └── check.py                     # check_project() — TODOs marking what to implement
└── tests/
    ├── fixtures/
    │   └── metadata_v2_minimal.json # a valid 2.0 fixture to test against
    ├── test_model.py                # test stubs with docstrings describing acceptance
    └── test_check.py                # test stubs with docstrings describing acceptance
```

## How to use this

**If starting a new repo:**

```bash
mkdir mintd-v2 && cd mintd-v2
cp -r /workspace/slice-1-scaffold/* .
cp /workspace/CONTEXT.md .
cp -r /workspace/docs/plans .  # docs/plans/metadata-standard.md is the design reference
git init && git add -A && git commit -m "initial scaffold for slice 1"

# Set up the env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Confirm baseline:
pytest tests/  # should show NotImplementedError for each test — expected at start
```

**If starting in the existing repo on a branch:**

```bash
cd /workspace
git checkout -b rewrite/slice-1
cp -r slice-1-scaffold/src/mintd/model.py src/mintd/model.py
cp -r slice-1-scaffold/src/mintd/check.py src/mintd/check.py   # collides if you already have one — likely no
cp -r slice-1-scaffold/tests/test_model.py tests/test_model.py
cp -r slice-1-scaffold/tests/test_check.py tests/test_check.py
mkdir -p tests/fixtures
cp slice-1-scaffold/tests/fixtures/metadata_v2_minimal.json tests/fixtures/
# Your existing pyproject.toml already has most of what you need; just add pydantic.
```

## What to do

1. Read `SLICE-1.md` fully.
2. Read `docs/plans/metadata-standard.md` "The model" section (it's the spec for the Pydantic shape).
3. Read `CONTEXT.md` (lightweight glossary).
4. Implement `src/mintd/model.py`, then `src/mintd/check.py`. Each test in the tests files is a checkpoint — make one pass at a time.
5. When all tests pass, run `mypy src/mintd/model.py` to confirm types are clean.
6. Write up a short note: what surprised you, what felt clunky. Bring those to the slice 2 conversation.

Slice 1 is sized at ~2 days of focused work. If it's taking significantly longer, slow down — there's likely a design subtlety worth surfacing rather than working around.

## When you're stuck

- "Why this design?" → check `docs/plans/metadata-standard.md` for the rationale
- "Why this term?" → check `CONTEXT.md`
- Anything else → ask

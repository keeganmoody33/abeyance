# Naming — read before the first push

`detached` / `detached-approval` is a **working title**. It was chosen to describe the
mechanism (the wait is detached from any running process) and to be trivially replaceable, not
because it is the right name.

Nothing has been published under it: no PyPI release, no GitHub repo, no external links. The
rename is a mechanical find-and-replace across seven surfaces.

## What the name has to carry

The one-line claim is *human approval for agents that aren't running*. A good name signals
the **asynchrony** or the **consent**, ideally both, and does not read as another generic
"approvals" package. Things to check before committing:

- PyPI availability (`pip index versions <name>`) and GitHub org availability.
- Whether the CLI verb reads well: `<name> poll`, `<name> apply --id T`.
- Whether it survives being said out loud in "we put it behind `<name>`".

## The rename, in order

```bash
NEW_PKG=yourname          # python import name, e.g. `import yourname`
NEW_DIST=your-name        # PyPI/distribution name
```

1. **Package directory** — `git mv detached/ $NEW_PKG/`
2. **Imports** — every `from detached…` / `import detached` in `$NEW_PKG/`, `tests/`,
   `examples/`, `README.md`, `docs/`:
   ```bash
   grep -rl 'detached' --include='*.py' --include='*.md' --include='*.toml' . \
     | xargs sed -i '' "s/\bdetached\b/$NEW_PKG/g"
   ```
3. **`pyproject.toml`** — `name`, `[project.scripts]` entry point, `[project.urls]` (three),
   `[tool.setuptools.packages.find] include`, and the `all` extra which self-references
   `detached-approval[gmail,postgres]`.
4. **`NOTICE`** — the first line.
5. **`.gitignore`** — the `.detached-state/` and `*.detached.json` entries.
6. **Logger names** — `logging.getLogger("detached")` in `loop.py` and
   `"detached.notify"` in `adapters/notifiers.py`. These are the strings downstream users
   configure logging against, so changing them later is a breaking change.
7. **Error base class** — `DetachedError` in `errors.py`, re-exported from `__init__.py`.
   Catching it is part of the public API.
8. **Env var** — `DETACHED_STATE` in `examples/cli_app.py`.

Then:

```bash
grep -rin 'detached' . --exclude-dir=.git --exclude=RENAME.md   # should return nothing
python -m pytest -q                                             # 124 tests
python examples/01_single_approver.py
git rm RENAME.md
```

## Prose to revisit, not just replace

The word *detached* is used as a **term of art** in several docstrings and in the README
("the detached shape", "a detached loop"). If the new name is not a synonym for that idea,
those sentences need rewriting rather than substituting — a mechanical `sed` would leave
sentences like "the yourname shape", which reads as a typo.

Files where it appears as prose rather than an identifier:

- `README.md` — the opening contrast, the "cost argument" paragraph
- `docs/ARCHITECTURE.md` — "The one decision everything follows from"
- `docs/FAILURE-MODES.md` — the preamble
- `detached/__init__.py`, `loop.py`, `models.py`, `cursor.py` — module docstrings
- `tests/test_multi_approver.py::test_the_second_approver_can_arrive_much_later`

Keeping the concept name and changing only the package name is also fine — plenty of libraries
do that — but it should be a decision, not a leftover.

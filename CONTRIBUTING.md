# Contributing

## Branch rules

`main` is protected – **no direct pushes, no force pushes, no deletion**.
Every change goes through a Pull Request that needs:

- at least **1 approval** from another team member (stale approvals are dismissed on new pushes)
- all review conversations **resolved**
- CI check **`test`** passing (lint + pytest), branch up to date with `main`
- merged with **Squash merge** (linear history); the branch is deleted automatically

## Branch naming

```
<type>/<issue-number>-<short-description>
```

| type | use for |
|---|---|
| `feat/` | new feature (e.g. `feat/12-vote-counting`) |
| `fix/` | bug fix |
| `test/` | adding/changing tests |
| `docs/` | README, report, diagrams |
| `refactor/` | code restructuring without behaviour change |
| `chore/` | tooling, CI, config |

## Commit messages – Conventional Commits

```
feat(consensus): lock block on prevote quorum
fix(network): ignore duplicate headers
test(e2e): add T6 silent proposer scenario
```

Scopes: `crypto`, `network`, `consensus`, `execution`, `ledger`, `sim`, `e2e`, `ci`, `docs`.

## Daily flow

```bash
git switch main && git pull
git switch -c feat/12-vote-counting
# ... code ...
make lint test
git push -u origin feat/12-vote-counting
gh pr create --fill
```

Link the PR to its issue with `Closes #12` in the description.

## Definition of done

- [ ] Unit tests for new logic, all tests pass locally
- [ ] Determinism preserved (no wall-clock time, unseeded randomness, or unordered dict/set iteration in hashed/logged output)
- [ ] Logs include any new network/consensus event
- [ ] README/docs updated if behaviour or commands changed

## Submission packaging

The repository provides `scripts/package.py` and the Bash wrapper `scripts/package.sh`.
When preparing a submission, complete the member roster in README and provide the final report:

```bash
python scripts/package.py --report REPORT.pdf
```

The archive name uses student IDs in roster order. Its enclosing `Lab01_...` directory
contains exactly `src/`, `tests/`, `logs/`, `config/`, `README.md`, and `REPORT.pdf`.
Packaging excludes repository tools, caches, and temporary test output. An existing archive
is never overwritten. The team must check that the report meets the 10-page limit.

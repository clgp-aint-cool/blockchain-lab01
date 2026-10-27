# Lab01 – Minimal Layer-1 Blockchain

Blockchain course 2026 – Knowledge Engineering Department.

A minimal L1 blockchain that reaches **BFT finality** (Prevote/Precommit, `n = 3f + 1`).
Each node is a separate process (one terminal, its own port) that keeps TCP connections to a
few neighbors, finds peers with **signed UDP multicast beacons**, and spreads messages by
**gossip**. Unreliable network conditions (delay, duplication, reordering, drops) come from a
**deterministic fault injection layer**. Runs on one machine, or on several machines over
ZeroTier.

## Team

| Student ID | Name | GitHub | Main area |
|---|---|---|---|
| ID1 | | | |
| ID2 | | | |
| ID3 | | | |
| ID4 | | | |

## Documentation

- [docs/CONCEPT_GUIDE.md](docs/CONCEPT_GUIDE.md) – concepts behind the project (Vietnamese)
- [docs/TEAM_PLAN.md](docs/TEAM_PLAN.md) – design, roles, interfaces, timeline

## Project structure

```
src/l1sim/      node source (crypto, network, discovery, consensus, execution)
tests/unit/     unit tests: signatures/domains, state rules, vote counting, block validation
tests/e2e/      end-to-end scenarios T1–T8
config/         scenario configs (seed, validators, network params)
logs/           run output (git-ignored)
scripts/        dev helpers: multicast check, determinism check, packaging (not submitted)
docs/           design notes, diagrams, report sources
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
make install
```

## Run

One node per terminal (WIP):

```bash
python -m l1sim.run_node --id V0 --port 9000 --scenario config/t1_normal.json
python -m l1sim.run_node --id V1 --port 9001 --scenario config/t1_normal.json
# ... up to V9 on port 9009
```

On several machines over ZeroTier, add:

```bash
--listen 0.0.0.0:<port> --advertise <zerotier-ip>:<port> --mcast-iface <zerotier-ip>
```

## Network check

On one machine:

```bash
python scripts/mcast_test.py --self-test 4
```

On two machines over ZeroTier, run on both:

```bash
python scripts/mcast_test.py --id <name> --iface <own zerotier-ip> --duration 60
```

It prints the beacons heard and round-trip times.

## Tests

```bash
make test          # all tests (single entry point)
make unit          # unit tests only
make e2e           # T1–T8
make determinism   # T8: same seed twice -> byte-identical logs + state hash
```

| Test | Scenario | Required outcome |
|---|---|---|
| T1 | Normal run, no faults | All correct nodes finalize same chain & state hash |
| T2 | Duplicate/reordered messages | No double-counted votes, no conflicting finalization |
| T3 | Invalid signature / wrong domain | Message rejected, reason logged |
| T4 | Replayed/duplicate transaction | Applied at most once |
| T5 | Drop/delay before synchrony | Safety holds |
| T6 | Proposer silent/crashed | Round timeout, chain continues |
| T7 | Up to f Byzantine equivocate | No conflicting finalization |
| T8 | Same seed rerun twice | `logs/results/` is byte-identical; event logs may differ only in timestamps and event order |
| D1 | Peer discovery: nodes start in random order, no known addresses | Every node gets enough neighbors, graph connected, T1 passes |
| D2 | A neighbor dies | Node replaces it, chain continues |
| D3 | A node restarts behind | Block sync catches up, same chain |

### For graders

```bash
pip install -r tests/requirements.txt
python tests/run_tests.py   # (WIP, added later)
```

## Workflow

See [CONTRIBUTING.md](CONTRIBUTING.md). `main` is protected: all changes go through a Pull Request
with 1 approval and green CI.

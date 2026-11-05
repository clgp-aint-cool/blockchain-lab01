"""T3: invalid signatures and wrong signing domains over real validator TCP links."""

import pytest

from tests.e2e.t3_cluster import run_t3
from tests.e2e.test_byzantine_scenarios import honest_results

pytestmark = pytest.mark.e2e


def test_t3_invalid_signature_wrong_domain(tmp_path):
    scenario, results, events, byzantine = run_t3(tmp_path)
    assert byzantine == {"V5", "V6"}
    honest = honest_results(results, byzantine)
    first = next(iter(honest.values()))
    assert first["finalized_height"] == scenario["max_height"]
    assert all(result == first for result in honest.values())
    assert sum(block["txs"] for block in first["blocks"]) == len(scenario["transactions"])

    expected = {
        ("V5", "PREVOTE", "bad_signature"),
        ("V6", "PREVOTE", "wrong_domain"),
    }
    for node in honest:
        rejected = {
            (event["voter"], event["phase"], event["reason"])
            for event in events[node]
            if event["event"] == "vote_rejected"
            and event["vote_height"] == 1
            and event["vote_round"] == 0
        }
        assert expected <= rejected, f"{node}: missing rejection reasons: {expected - rejected}"

    # Verify the configured faults actually ran, so a clean run cannot pass T3.
    for node, behavior, kind in (
        ("V5", "bad_signature", "prevote"),
        ("V6", "wrong_domain", "prevote"),
    ):
        assert any(
            event["event"] == "byzantine_action"
            and event["behavior"] == behavior
            and event["kind"] == kind
            and event["height"] == 1
            and event["round"] == 0
            for event in events[node]
        ), f"{node}: configured {behavior} fault was not exercised"

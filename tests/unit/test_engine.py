import json
from pathlib import Path

import pytest

from l1sim.consensus.engine import Step, TimeoutConfig, proposer_for

CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.json"
CONSENSUS = {
    "timeout_propose_ms": 1000,
    "timeout_prevote_ms": 800,
    "timeout_precommit_ms": 600,
    "timeout_delta_ms": 300,
}


def test_proposer_rotates_with_height_and_round_over_sorted_validators():
    validators = ["V3", "V1", "V0", "V2"]

    assert proposer_for(validators, 1, 0) == "V1"
    assert proposer_for(validators, 1, 1) == "V2"
    assert proposer_for(validators, 2, 0) == "V2"
    assert proposer_for(validators, 3, 1) == "V0"
    assert proposer_for({"V1": None, "V0": None}, 1, 0) == "V1"


def test_proposer_requires_validators():
    with pytest.raises(ValueError):
        proposer_for([], 1, 0)


def test_timeouts_grow_with_round():
    timeouts = TimeoutConfig.from_config(CONSENSUS)

    assert timeouts.duration(Step.PROPOSE, 0) == 1000
    assert timeouts.duration(Step.PREVOTE, 0) == 800
    assert timeouts.duration(Step.PRECOMMIT, 2) == 1200
    assert timeouts.duration(Step.PROPOSE, 1) > timeouts.duration(Step.PROPOSE, 0)


def test_timeout_config_has_no_defaults():
    missing = {key: value for key, value in CONSENSUS.items() if key != "timeout_delta_ms"}

    with pytest.raises(KeyError):
        TimeoutConfig.from_config(missing)


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_timeout_config_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        TimeoutConfig.from_config({**CONSENSUS, "timeout_delta_ms": bad})


def test_default_config_provides_every_timeout():
    consensus = json.loads(CONFIG.read_text())["consensus"]

    assert TimeoutConfig.from_config(consensus).delta_ms == 300

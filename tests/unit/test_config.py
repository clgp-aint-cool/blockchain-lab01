import json
from pathlib import Path

import pytest

from l1sim.config import ConfigError, load_config, load_genesis, load_scenario

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("n", [8, 9, 10, 11, 12, 13])
def test_load_config_accepts_any_validator_count_at_least_eight(tmp_path, n):
    from l1sim.crypto import keypair_from_seed

    validators = [f"V{i}" for i in range(n)]
    genesis = {
        "chain_id": "lab01",
        "seed": 42,
        "validators": [
            {"id": v, "public_key": keypair_from_seed(42, v).public_bytes.hex()} for v in validators
        ],
    }
    scenario = {
        "seed": 42,
        "num_validators": n,
        "validators": validators,
        "max_height": 1,
        "byzantine": [{"node": v, "behavior": "silent"} for v in validators[: (n - 1) // 3]],
    }
    genesis_path = tmp_path / "genesis.json"
    scenario_path = tmp_path / "scenario.json"
    genesis_path.write_text(json.dumps(genesis), encoding="utf-8")
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    loaded_genesis, loaded_scenario = load_config(scenario_path, genesis_path)
    assert loaded_genesis.validators == tuple(validators)
    assert loaded_scenario.num_validators == n
    assert len(loaded_scenario.byzantine) == (n - 1) // 3

    scenario["byzantine"].append({"node": validators[(n - 1) // 3], "behavior": "silent"})
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    with pytest.raises(ConfigError, match="at most f"):
        load_scenario(scenario_path)


@pytest.mark.parametrize("n", [1, 4, 7])
def test_config_rejects_fewer_than_eight_validators(tmp_path, n):
    genesis = json.loads((ROOT / "config/genesis.json").read_text())
    genesis["validators"] = genesis["validators"][:n]
    genesis_path = tmp_path / "genesis.json"
    genesis_path.write_text(json.dumps(genesis), encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps({"seed": 42, "num_validators": n}))
    with pytest.raises(ConfigError, match="at least 8"):
        load_genesis(genesis_path)
    with pytest.raises(ConfigError, match="at least 8"):
        load_scenario(scenario_path)


def test_load_genesis_and_t1_scenario():
    genesis = load_genesis(ROOT / "config/genesis.json")
    scenario = load_scenario(ROOT / "config/t1_normal.json")

    assert genesis.chain_id == "lab01"
    assert len(genesis.validators) == 10
    assert scenario.name == "t1_normal"
    assert len(scenario.transactions) == 20
    assert scenario.consensus.timeout_delta_ms == 300


def test_load_default_scenario():
    scenario = load_scenario(ROOT / "config/default.json")

    assert scenario.chain_id == "lab01"
    assert scenario.num_validators == 10
    assert scenario.consensus.timeout_delta_ms == 300
    assert scenario.network.peers == {f"V{i}": f"127.0.0.1:{9000 + i}" for i in range(10)}


def test_config_rejects_wrong_chain_id(tmp_path):
    path = tmp_path / "genesis.json"
    path.write_text('{"chain_id":"wrong","seed":42,"validators":["V0"]}', encoding="utf-8")
    with pytest.raises(ConfigError, match="chain_id"):
        load_genesis(path)


def test_genesis_requires_public_keys(tmp_path):
    path = tmp_path / "genesis.json"
    path.write_text(
        '{"chain_id":"lab01","seed":42,"validators":[{"id":"V0"},{"id":"V1"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="public_key"):
        load_genesis(path)


def test_genesis_rejects_empty_validator_id(tmp_path):
    path = tmp_path / "genesis.json"
    path.write_text(
        '{"chain_id":"lab01","seed":42,"validators":[{"id":"","public_key":"'
        + ("00" * 32)
        + '"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="validator"):
        load_genesis(path)


def test_scenario_rejects_wrong_chain_id(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(
        '{"name":"bad","chain_id":"wrong","seed":42,"num_validators":10,'
        '"validators":["V0","V1","V2","V3","V4","V5","V6","V7","V8","V9"],'
        '"max_height":1}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="chain_id"):
        load_scenario(path)


def test_scenario_rejects_invalid_sender(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(
        '{"name":"bad","seed":42,"num_validators":10,'
        '"validators":["V0","V1","V2","V3","V4","V5","V6","V7","V8","V9"],'
        '"max_height":1,"transactions":[{"sender":"","key":"Alice/account","value":"x","nonce":0}]}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="sender"):
        load_scenario(path)


def test_load_config_rejects_mismatched_pair(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        '{"name":"bad","chain_id":"lab01","seed":43,"num_validators":10,'
        '"validators":["V0","V1","V2","V3","V4","V5","V6","V7","V8","V9"],'
        '"max_height":1}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="seed"):
        load_config(scenario, ROOT / "config/genesis.json")


def test_scenario_accepts_static_peer_addresses(tmp_path):
    import json

    raw = json.loads((ROOT / "config/t1_normal.json").read_text())
    peers = {f"V{i}": f"10.147.17.{10 + i}:9000" for i in range(10)}
    raw["network"]["peers"] = peers
    path = tmp_path / "peers.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_scenario(path).network.peers == peers


@pytest.mark.parametrize(
    "peers",
    [
        [],
        {"V0": "127.0.0.1:9000"},
        {f"V{i}": "127.0.0.1:9000" for i in range(10)},
        {f"V{i}": f"127.0.0.1:{70000 + i}" for i in range(10)},
        {f"V{i}": i for i in range(10)},
        {f"V{i}": f"localhost{i}" for i in range(10)},
    ],
)
def test_scenario_rejects_invalid_static_peers(tmp_path, peers):
    import json

    raw = json.loads((ROOT / "config/t1_normal.json").read_text())
    raw["network"]["peers"] = peers
    path = tmp_path / "peers.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="network.peers"):
        load_scenario(path)

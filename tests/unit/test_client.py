from pathlib import Path

import pytest

from l1sim.client import (
    TxClient,
    build_scenario_transactions,
    make_signed_tx,
    send_scenario,
)
from l1sim.config import load_scenario
from l1sim.crypto import keypair_from_seed, verify
from l1sim.encoding import encode
from l1sim.models import Domain

ROOT = Path(__file__).parents[2]


def test_t1_scenario_transactions_are_signed_deterministically():
    scenario = load_scenario(ROOT / "config/t1_normal.json")
    transactions = build_scenario_transactions(scenario)

    assert len(transactions) == 20
    first = make_signed_tx(42, "Alice", "Alice/test", "ok", 0)
    second = make_signed_tx(42, "Alice", "Alice/test", "ok", 0)
    assert first.signature == second.signature
    assert encode(transactions[0]) == encode(build_scenario_transactions(scenario)[0])

    public_key = keypair_from_seed(42, "Alice").public_key
    assert verify(public_key, Domain.TX, transactions[0], transactions[0].signature)


def test_tx_client_validates_private_keys():
    with pytest.raises(TypeError, match="Ed25519PrivateKey"):
        TxClient(42, lambda tx: None, {"Alice": object()})


def test_make_signed_tx_rejects_wrong_private_key():
    with pytest.raises(TypeError, match="Ed25519PrivateKey"):
        make_signed_tx(42, "Alice", "Alice/account", "ok", 0, private_key=object())


def test_tx_client_submit_validates_tx_type():
    client = TxClient(42, lambda tx: None)
    with pytest.raises(TypeError, match="Tx instance"):
        client.submit(object())


def test_tx_client_submit_calls_handler():
    received = []
    client = TxClient(42, received.append)
    tx = client.make_tx("Alice", "Alice/account", "ok", 0)

    client.submit(tx)

    assert received == [tx]


def test_send_scenario_supports_private_keys():
    scenario = load_scenario(ROOT / "config/t1_normal.json")
    private_key = keypair_from_seed(42, "Alice").private_key
    received = send_scenario(scenario, lambda tx: None, {"Alice": private_key})
    assert len(received) == 20


def test_send_scenario_custom_private_key_signature_verifies():
    scenario = load_scenario(ROOT / "config/t1_normal.json")
    private_key = keypair_from_seed(42, "Alice").private_key
    received = send_scenario(scenario, lambda tx: None, {"Alice": private_key})
    public_key = keypair_from_seed(42, "Alice").public_key

    alice_tx = next(tx for tx in received if tx.sender == "Alice")
    assert verify(public_key, Domain.TX, alice_tx, alice_tx.signature)


def test_client_cli_still_prints_without_connect(capsys):
    import json

    from l1sim.client import main

    assert main(["--scenario", str(ROOT / "config/t1_normal.json")]) == 0
    output = capsys.readouterr()
    assert len([json.loads(line) for line in output.out.splitlines()]) == 20
    assert not output.err


def test_client_cli_connection_failure_has_nonzero_exit(capsys):
    import socket

    from l1sim.client import main

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        status = main(
            [
                "--scenario",
                str(ROOT / "config/t1_normal.json"),
                "--genesis",
                str(ROOT / "config/genesis.json"),
                "--connect",
                f"127.0.0.1:{port}",
                "--timeout",
                "0.5",
            ]
        )
    assert status == 1
    assert "client error:" in capsys.readouterr().err

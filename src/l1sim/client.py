"""Scenario transaction construction and submission helpers."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from l1sim.config import ConfigError, GenesisConfig, ScenarioConfig, load_config, load_scenario
from l1sim.crypto import keypair_from_seed, sign
from l1sim.encoding import encode, hash_with_domain
from l1sim.models import Domain, Kind, Message, Tx
from l1sim.network.hello import HelloError, check_hello
from l1sim.network.tcp_transport import FrameTooLarge, parse_addr, read_frame, write_frame

TxHandler = Callable[[Tx], None]


class TxClient:
    """Build and submit signed transactions.

    The handler is injected by the node/network integration layer.  This keeps
    client signing deterministic and independently testable before networking is
    implemented.
    """

    def __init__(
        self,
        seed: int,
        handler: TxHandler,
        private_keys: Mapping[str, Ed25519PrivateKey] | None = None,
    ) -> None:
        if type(seed) is not int:
            raise TypeError("seed must be an integer")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.seed = seed
        self.handler = handler
        self.private_keys = dict(private_keys or {})
        for sender, private_key in self.private_keys.items():
            if not isinstance(sender, str) or not sender:
                raise TypeError("private key sender must be a non-empty string")
            if not isinstance(private_key, Ed25519PrivateKey):
                raise TypeError("private_keys values must be Ed25519PrivateKey instances")

    def make_tx(self, sender: str, key: str, value: str, nonce: int) -> Tx:
        private_key = self.private_keys.get(sender)
        return make_signed_tx(
            self.seed,
            sender,
            key,
            value,
            nonce,
            private_key=private_key,
        )

    def submit(self, tx: Tx) -> None:
        if not isinstance(tx, Tx):
            raise TypeError("tx must be a Tx instance")
        self.handler(tx)

    def submit_scenario(self, scenario: ScenarioConfig) -> tuple[Tx, ...]:
        if not isinstance(scenario, ScenarioConfig):
            raise TypeError("scenario must be a ScenarioConfig instance")
        submitted: list[Tx] = []
        for raw_tx in scenario.transactions:
            tx = self.make_tx(
                sender=raw_tx["sender"],
                key=raw_tx["key"],
                value=raw_tx["value"],
                nonce=raw_tx["nonce"],
            )
            self.submit(tx)
            submitted.append(tx)
        return tuple(submitted)


def make_signed_tx(
    seed: int,
    sender: str,
    key: str,
    value: str,
    nonce: int,
    private_key: Ed25519PrivateKey | None = None,
) -> Tx:
    """Convenience function for constructing one signed transaction."""
    if private_key is not None and not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("private_key must be an Ed25519PrivateKey instance")
    unsigned = Tx(sender=sender, key=key, value=value, nonce=nonce)
    signing_key = private_key or keypair_from_seed(seed, sender).private_key
    signature = sign(signing_key, Domain.TX, unsigned)
    return Tx(sender=sender, key=key, value=value, nonce=nonce, signature=signature)


def build_scenario_transactions(scenario: ScenarioConfig) -> tuple[Tx, ...]:
    if not isinstance(scenario, ScenarioConfig):
        raise TypeError("scenario must be a ScenarioConfig instance")
    return tuple(
        make_signed_tx(
            scenario.seed,
            raw_tx["sender"],
            raw_tx["key"],
            raw_tx["value"],
            raw_tx["nonce"],
        )
        for raw_tx in scenario.transactions
    )


def send_scenario(
    scenario: ScenarioConfig,
    handler: TxHandler,
    private_keys: Mapping[str, Ed25519PrivateKey] | None = None,
) -> tuple[Tx, ...]:
    if not isinstance(scenario, ScenarioConfig):
        raise TypeError("scenario must be a ScenarioConfig instance")
    client = TxClient(scenario.seed, handler, private_keys=private_keys)
    return client.submit_scenario(scenario)


def _print_transactions(scenario_path: str) -> int:
    scenario = load_scenario(scenario_path)
    for tx in build_scenario_transactions(scenario):
        encoded = encode(tx) + b"\n"
        output = getattr(sys.stdout, "buffer", None)
        if output is not None:
            output.write(encoded)
            output.flush()
        else:
            sys.stdout.write(encoded.decode("utf-8"))
            sys.stdout.flush()
    return 0


@dataclass(frozen=True)
class SubmissionResult:
    """A node's mempool acknowledgement, not a finalization certificate."""

    tx_id: str
    accepted: bool
    reason: str


async def send_transactions(
    address: str,
    transactions: Iterable[Tx],
    genesis: GenesisConfig,
    *,
    timeout_s: float = 10.0,
) -> tuple[SubmissionResult, ...]:
    """Verify the server's HELLO, submit signed TX frames and await per-TX acknowledgements."""
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout must be finite and positive")
    host, port = parse_addr(address)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
    try:
        validator_keys = {
            node: Ed25519PublicKey.from_public_bytes(bytes.fromhex(key))
            for node, key in genesis.public_keys.items()
        }
        # An empty self id cannot coincide with a valid validator identifier.
        check_hello(await asyncio.wait_for(read_frame(reader), timeout_s), validator_keys, me="")
        results = []
        for tx in transactions:
            if not isinstance(tx, Tx):
                raise TypeError("transactions must contain Tx instances")
            await asyncio.wait_for(
                write_frame(writer, encode(Message(Kind.TX, tx.sender, 0, tx))), timeout_s
            )
            data = await asyncio.wait_for(read_frame(reader), timeout_s)
            result = json.loads(data)
            expected_id = hash_with_domain("TX", tx)
            if (
                not isinstance(result, dict)
                or set(result) != {"tx_id", "accepted", "reason"}
                or result["tx_id"] != expected_id
                or type(result["accepted"]) is not bool
                or not isinstance(result["reason"], str)
                or encode(result) != data
            ):
                raise ValueError("invalid transaction acknowledgement")
            results.append(SubmissionResult(**result))
        return tuple(results)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout_s)
        except (OSError, asyncio.TimeoutError):
            writer.transport.abort()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build transactions from a Lab01 scenario")
    parser.add_argument("--scenario", required=True, help="scenario JSON file")
    parser.add_argument("--connect", help="send signed transactions to a node at host:port")
    parser.add_argument("--genesis", default="config/genesis.json", help="trusted validator keys")
    parser.add_argument(
        "--timeout", type=float, default=10, help="per-operation network timeout in seconds"
    )
    args = parser.parse_args(argv)
    try:
        if args.connect:
            genesis, scenario = load_config(args.scenario, args.genesis)
            results = asyncio.run(
                send_transactions(
                    args.connect,
                    build_scenario_transactions(scenario),
                    genesis,
                    timeout_s=args.timeout,
                )
            )
            for result in results:
                print(encode(result).decode("utf-8"))
            return 0 if all(result.accepted for result in results) else 1
        return _print_transactions(args.scenario)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    except (
        OSError,
        ValueError,
        HelloError,
        FrameTooLarge,
        asyncio.IncompleteReadError,
        asyncio.TimeoutError,
    ) as exc:
        print(f"client error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

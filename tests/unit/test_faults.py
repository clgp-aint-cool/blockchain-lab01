"""Unit tests for FaultInjector."""

import pytest

from l1sim.config import NetworkConfig
from l1sim.models import Kind, Message, Tx
from l1sim.network.faults import FaultInjector


def _make_msg(height: int = 1, sender: str = "Alice", nonce: int = 0) -> Message:
    tx = Tx(sender=sender, key=f"{sender}/key", value="val", nonce=nonce)
    return Message(kind=Kind.TX, origin=sender, height=height, payload=tx)


def test_fault_injector_same_message_same_fate():
    cfg = NetworkConfig(drop_rate=0.3, duplicate_rate=0.3, gst_height=5, delta_ms=500)
    injector = FaultInjector(seed=42, fault_cfg=cfg)
    msg = _make_msg(height=2, nonce=1)

    first = injector.decide("V0", "V1", msg)
    second = injector.decide("V0", "V1", msg)

    assert first == second


def test_fault_injector_different_call_order_same_fate():
    cfg = NetworkConfig(drop_rate=0.3, duplicate_rate=0.3, gst_height=5, delta_ms=500)
    injector1 = FaultInjector(seed=42, fault_cfg=cfg)
    injector2 = FaultInjector(seed=42, fault_cfg=cfg)

    msg_a = _make_msg(height=1, nonce=1)
    msg_b = _make_msg(height=1, nonce=2)

    # Order 1: A then B
    res_a_1 = injector1.decide("V0", "V1", msg_a)
    res_b_1 = injector1.decide("V0", "V1", msg_b)

    # Order 2: B then A
    res_b_2 = injector2.decide("V0", "V1", msg_b)
    res_a_2 = injector2.decide("V0", "V1", msg_a)

    assert res_a_1 == res_a_2
    assert res_b_1 == res_b_2


def test_fault_injector_drop_rate_zero_never_drops():
    cfg = NetworkConfig(drop_rate=0.0, duplicate_rate=0.0, gst_height=0)
    injector = FaultInjector(seed=42, fault_cfg=cfg)

    for i in range(50):
        msg = _make_msg(height=1, nonce=i)
        result = injector.decide("V0", "V1", msg)
        assert len(result) >= 1


def test_fault_injector_drop_rate_one_always_drops_pre_gst():
    cfg = NetworkConfig(drop_rate=1.0, duplicate_rate=0.0, gst_height=10)
    injector = FaultInjector(seed=42, fault_cfg=cfg)

    for i in range(50):
        msg = _make_msg(height=1, nonce=i)
        assert injector.decide("V0", "V1", msg) == []
        outcome = injector.decide_outcome("V0", "V1", msg)
        assert outcome.fate == "drop"
        assert outcome.delays_ms == ()


def test_fault_injector_post_gst_never_drops():
    cfg = NetworkConfig(drop_rate=1.0, duplicate_rate=0.0, gst_height=3)
    injector = FaultInjector(seed=42, fault_cfg=cfg)

    # Pre-GST: drops 100%
    for i in range(20):
        msg = _make_msg(height=1, nonce=i)
        assert injector.decide("V0", "V1", msg) == []

    # Post-GST: never drops even with drop_rate=1.0
    for i in range(50):
        msg = _make_msg(height=3 + (i % 3), nonce=i)
        result = injector.decide("V0", "V1", msg)
        assert len(result) == 1
        assert injector.decide_outcome("V0", "V1", msg).fate == "send"


def test_fault_injector_duplicate_rate_one_always_duplicates():
    cfg = NetworkConfig(drop_rate=0.0, duplicate_rate=1.0)
    injector = FaultInjector(seed=42, fault_cfg=cfg)

    for i in range(50):
        msg = _make_msg(height=1, nonce=i)
        result = injector.decide("V0", "V1", msg)
        assert len(result) == 2
        outcome = injector.decide_outcome("V0", "V1", msg)
        assert outcome.fate == "duplicate"


def test_fault_injector_no_delay_in_forbidden_zone():
    cfg = NetworkConfig(drop_rate=0.1, duplicate_rate=0.3, gst_height=5)
    injector = FaultInjector(seed=42, fault_cfg=cfg, timeout_base_ms=1000)

    # Forbidden zone: (100 ms, 2000 ms)
    for i in range(200):
        msg = _make_msg(height=i % 10, nonce=i)
        delays = injector.decide("V0", "V1", msg)
        for d in delays:
            assert d <= 100 or d >= 2000, f"Delay {d} fell in forbidden zone (100, 2000)"


def test_fault_injector_pre_gst_allows_long_delay():
    cfg = NetworkConfig(drop_rate=0.0, duplicate_rate=0.0, gst_height=10)
    injector = FaultInjector(seed=42, fault_cfg=cfg)

    # Over 100 messages with height < gst_height, some must get long delay
    long_count = 0
    short_count = 0
    for i in range(100):
        msg = _make_msg(height=1, nonce=i)
        delays = injector.decide("V0", "V1", msg)
        assert len(delays) == 1
        if delays[0] >= 2000:
            long_count += 1
        elif delays[0] <= 100:
            short_count += 1

    assert long_count > 0, "Pre-GST should have some long delays"
    assert short_count > 0, "Pre-GST should have some short delays"


def test_fault_injector_long_delay_exceeds_two_times_timeout_at_all_rounds():
    cfg = NetworkConfig(drop_rate=0.0, duplicate_rate=0.0, gst_height=5)
    base = 1000
    delta = 300
    max_round = 10
    inj = FaultInjector(
        seed=42,
        fault_cfg=cfg,
        timeout_base_ms=base,
        timeout_delta_ms=delta,
        max_round=max_round,
    )

    for r in range(max_round + 1):
        round_timeout = base + r * delta
        assert inj.long_delay_ms >= 2 * round_timeout, (
            f"Failed at round {r}: {inj.long_delay_ms} < {2 * round_timeout}"
        )


def test_fault_injector_post_gst_always_short_delay():
    cfg = NetworkConfig(drop_rate=0.0, duplicate_rate=0.5, gst_height=3)
    injector = FaultInjector(seed=42, fault_cfg=cfg)

    for i in range(50):
        # height >= gst_height (height 3, 4, 5)
        msg = _make_msg(height=3 + (i % 3), nonce=i)
        delays = injector.decide("V0", "V1", msg)
        for d in delays:
            assert d <= 100, f"Post-GST delay must be <= 100ms, got {d}"


def test_fault_injector_post_gst_delays_vary_for_reordering():
    cfg = NetworkConfig(drop_rate=0.0, duplicate_rate=0.0, gst_height=0)
    injector = FaultInjector(seed=42, fault_cfg=cfg, timeout_base_ms=1000)

    delays = set()
    for i in range(50):
        msg = _make_msg(height=1, nonce=i)
        d = injector.decide("V0", "V1", msg)[0]
        assert 1 <= d <= 100
        delays.add(d)

    # Delays should vary across messages rather than a single fixed number
    assert len(delays) > 5, f"Expected varied delays for reordering, got {delays}"


def test_fault_injector_logger_events():
    events = []
    msg = _make_msg(height=1, nonce=0)

    # 1. Drop logging
    cfg_drop = NetworkConfig(drop_rate=1.0, duplicate_rate=0.0, gst_height=5)
    inj_drop = FaultInjector(
        seed=42,
        fault_cfg=cfg_drop,
        logger=lambda ev, **kw: events.append((ev, kw)),
    )
    outcome_drop = inj_drop.decide_outcome("V0", "V1", msg)
    assert outcome_drop.fate == "drop"
    assert len(events) == 1
    assert events[0][0] == "fault_drop"
    assert events[0][1] == {"src": "V0", "dst": "V1", "kind": "TX", "height": 1}

    # 2. Duplicate logging
    events.clear()
    cfg_dup = NetworkConfig(drop_rate=0.0, duplicate_rate=1.0, gst_height=5)
    inj_dup = FaultInjector(
        seed=42,
        fault_cfg=cfg_dup,
        logger=lambda ev, **kw: events.append((ev, kw)),
    )
    outcome_dup = inj_dup.decide_outcome("V0", "V1", msg)
    assert outcome_dup.fate == "duplicate"
    assert len(events) == 1
    assert events[0][0] == "fault_duplicate"
    assert events[0][1] == {"src": "V0", "dst": "V1", "kind": "TX", "height": 1}

    # 3. Normal send does not emit fault events
    events.clear()
    cfg_send = NetworkConfig(drop_rate=0.0, duplicate_rate=0.0, gst_height=0)
    inj_send = FaultInjector(
        seed=42,
        fault_cfg=cfg_send,
        logger=lambda ev, **kw: events.append((ev, kw)),
    )
    outcome_send = inj_send.decide_outcome("V0", "V1", msg)
    assert outcome_send.fate == "send"
    assert len(events) == 0


def test_fault_injector_seed_changes_outcome():
    cfg = NetworkConfig(drop_rate=0.5, duplicate_rate=0.2, gst_height=5)
    inj1 = FaultInjector(seed=1, fault_cfg=cfg)
    inj2 = FaultInjector(seed=99999, fault_cfg=cfg)

    diff = False
    for i in range(20):
        msg = _make_msg(height=1, nonce=i)
        if inj1.decide("V0", "V1", msg) != inj2.decide("V0", "V1", msg):
            diff = True
            break
    assert diff, "Different seeds should produce different decisions across messages"


def test_fault_injector_src_dst_changes_outcome():
    cfg = NetworkConfig(drop_rate=0.5, duplicate_rate=0.2, gst_height=5)
    inj = FaultInjector(seed=42, fault_cfg=cfg)

    diff = False
    for i in range(20):
        msg = _make_msg(height=1, nonce=i)
        if inj.decide("V0", "V1", msg) != inj.decide("V0", "V2", msg):
            diff = True
            break
    assert diff, "Different destinations should produce different decisions"


def test_fault_injector_invalid_arguments():
    cfg = NetworkConfig()

    with pytest.raises(TypeError, match="seed"):
        FaultInjector(seed="42", fault_cfg=cfg)  # type: ignore

    with pytest.raises(TypeError, match="fault_cfg"):
        FaultInjector(seed=42, fault_cfg={})  # type: ignore

    with pytest.raises(ValueError, match="timeout_base_ms"):
        FaultInjector(seed=42, fault_cfg=cfg, timeout_base_ms=0)

    with pytest.raises(ValueError, match="timeout_delta_ms"):
        FaultInjector(seed=42, fault_cfg=cfg, timeout_delta_ms=-1)

    with pytest.raises(ValueError, match="max_round"):
        FaultInjector(seed=42, fault_cfg=cfg, max_round=-1)

    with pytest.raises(TypeError, match="logger"):
        FaultInjector(seed=42, fault_cfg=cfg, logger="not-callable")  # type: ignore

    inj = FaultInjector(seed=42, fault_cfg=cfg)
    msg = _make_msg()

    with pytest.raises(TypeError, match="src"):
        inj.decide("", "V1", msg)

    with pytest.raises(TypeError, match="dst"):
        inj.decide("V0", "", msg)

    with pytest.raises(TypeError, match="msg"):
        inj.decide("V0", "V1", "not-a-message")  # type: ignore


def test_fault_injector_properties():
    cfg = NetworkConfig()
    logged = []
    inj = FaultInjector(
        seed=123,
        fault_cfg=cfg,
        logger=logged.append,
        timeout_base_ms=800,
        timeout_delta_ms=250,
        max_round=6,
    )

    assert inj.seed == 123
    assert inj.config == cfg
    assert inj.timeout_base_ms == 800
    assert inj.timeout_delta_ms == 250
    assert inj.max_round == 6
    assert inj.long_delay_ms == 2 * (800 + 6 * 250) + 400
    assert inj._logger is not None

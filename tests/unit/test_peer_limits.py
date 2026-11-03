"""Unit tests for PeerLimits."""

import pytest

from l1sim.network.peer_limits import PeerLimits


class FakeClock:
    """Controllable clock for deterministic rate-limit testing."""

    def __init__(self, start: float = 1000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_peer_limits_first_message_allowed():
    clock = FakeClock()
    limits = PeerLimits(rate_limit=5, block_duration=2.0, time_provider=clock)
    assert limits.on_receive("peer1") is True


def test_peer_limits_within_rate_limit_allowed():
    clock = FakeClock()
    limits = PeerLimits(rate_limit=5, block_duration=2.0, time_provider=clock)

    for _ in range(5):
        assert limits.on_receive("peer1") is True


def test_peer_limits_exceed_rate_limit_blocks_peer():
    clock = FakeClock()
    events = []
    limits = PeerLimits(
        rate_limit=5,
        block_duration=2.0,
        logger=lambda ev, **kw: events.append((ev, kw)),
        time_provider=clock,
    )

    # First 5 are allowed
    for _ in range(5):
        assert limits.on_receive("peer1") is True

    # 6th in the same 1s window exceeds limit -> returns False and blocks
    assert limits.on_receive("peer1") is False
    assert limits.is_blocked("peer1") is True
    assert ("block", {"peer": "peer1"}) in events


def test_peer_limits_blocked_peer_drops_future_messages():
    clock = FakeClock()
    limits = PeerLimits(rate_limit=3, block_duration=5.0, time_provider=clock)

    for _ in range(3):
        assert limits.on_receive("peer1") is True
    assert limits.on_receive("peer1") is False  # 4th blocks

    # Next messages during block period return False
    clock.advance(1.0)
    assert limits.on_receive("peer1") is False
    clock.advance(2.0)
    assert limits.on_receive("peer1") is False


def test_peer_limits_unblock_after_block_duration_expires():
    clock = FakeClock()
    events = []
    limits = PeerLimits(
        rate_limit=3,
        block_duration=5.0,
        logger=lambda ev, **kw: events.append((ev, kw)),
        time_provider=clock,
    )

    for _ in range(4):
        limits.on_receive("peer1")  # blocks on 4th

    assert limits.is_blocked("peer1") is True

    # Advance past block duration (5s)
    clock.advance(5.1)

    # on_receive should unblock and allow message
    assert limits.on_receive("peer1") is True
    assert limits.is_blocked("peer1") is False
    assert ("unblock", {"peer": "peer1"}) in events


def test_peer_limits_is_blocked_pure_query_no_mutation_or_log():
    clock = FakeClock()
    events = []
    limits = PeerLimits(
        rate_limit=2,
        block_duration=3.0,
        logger=lambda ev, **kw: events.append((ev, kw)),
        time_provider=clock,
    )

    limits.on_receive("peer1")
    limits.on_receive("peer1")
    limits.on_receive("peer1")  # blocked
    assert limits.is_blocked("peer1") is True

    clock.advance(3.1)
    # is_blocked should return False without mutating or emitting log
    assert limits.is_blocked("peer1") is False
    assert ("unblock", {"peer": "peer1"}) not in events

    # on_receive cleans up expired block and emits unblock log
    assert limits.on_receive("peer1") is True
    assert ("unblock", {"peer": "peer1"}) in events


def test_peer_limits_manual_unblock():
    clock = FakeClock()
    events = []
    limits = PeerLimits(
        rate_limit=2,
        block_duration=10.0,
        logger=lambda ev, **kw: events.append((ev, kw)),
        time_provider=clock,
    )

    limits.on_receive("peer1")
    limits.on_receive("peer1")
    limits.on_receive("peer1")  # blocked
    assert limits.is_blocked("peer1") is True

    limits.unblock("peer1")
    assert limits.is_blocked("peer1") is False
    assert ("unblock", {"peer": "peer1"}) in events

    # Unblocking non-blocked peer does nothing
    limits.unblock("peer1")


def test_peer_limits_sliding_window_rolls_over():
    clock = FakeClock()
    limits = PeerLimits(rate_limit=3, block_duration=5.0, time_provider=clock)

    # 3 messages at t=1000.0
    for _ in range(3):
        assert limits.on_receive("peer1") is True

    # Advance 1.1 seconds -> old window cleared
    clock.advance(1.1)

    # Can send 3 more without being blocked
    for _ in range(3):
        assert limits.on_receive("peer1") is True

    assert limits.is_blocked("peer1") is False


def test_peer_limits_allow_send_outbound():
    clock = FakeClock()
    limits = PeerLimits(out_rate_limit=3, time_provider=clock)

    assert limits.allow_send("peer1") is True
    assert limits.allow_send("peer1") is True
    assert limits.allow_send("peer1") is True

    # 4th message exceeds outbound limit
    assert limits.allow_send("peer1") is False

    # Advance 1.1 seconds -> window clears
    clock.advance(1.1)
    assert limits.allow_send("peer1") is True


def test_peer_limits_different_peers_independent():
    clock = FakeClock()
    limits = PeerLimits(rate_limit=2, block_duration=5.0, time_provider=clock)

    # Block peer1
    limits.on_receive("peer1")
    limits.on_receive("peer1")
    limits.on_receive("peer1")
    assert limits.is_blocked("peer1") is True

    # peer2 is completely unaffected
    assert limits.on_receive("peer2") is True
    assert limits.is_blocked("peer2") is False
    assert limits.allow_send("peer2") is True


def test_peer_limits_invalid_arguments():
    with pytest.raises(ValueError, match="rate_limit"):
        PeerLimits(rate_limit=0)

    with pytest.raises(ValueError, match="rate_limit"):
        PeerLimits(rate_limit=-1)

    with pytest.raises(ValueError, match="block_duration"):
        PeerLimits(block_duration=0)

    with pytest.raises(ValueError, match="block_duration"):
        PeerLimits(block_duration=-5.0)

    with pytest.raises(ValueError, match="block_duration"):
        PeerLimits(block_duration=True)  # type: ignore

    with pytest.raises(ValueError, match="out_rate_limit"):
        PeerLimits(out_rate_limit=0)

    with pytest.raises(TypeError, match="logger"):
        PeerLimits(logger="not-callable")  # type: ignore

    with pytest.raises(TypeError, match="time_provider"):
        PeerLimits(time_provider="not-callable")  # type: ignore

    limits = PeerLimits()

    with pytest.raises(TypeError, match="peer_id"):
        limits.on_receive("")

    with pytest.raises(TypeError, match="peer_id"):
        limits.on_receive(123)  # type: ignore

    with pytest.raises(TypeError, match="peer_id"):
        limits.allow_send("")

    with pytest.raises(TypeError, match="peer_id"):
        limits.is_blocked("")

    with pytest.raises(TypeError, match="peer_id"):
        limits.unblock("")


def test_peer_limits_properties_and_defaults():
    limits = PeerLimits(rate_limit=50, block_duration=12.5, out_rate_limit=75)
    assert limits.rate_limit == 50
    assert limits.block_duration == 12.5
    assert limits.out_rate_limit == 75

    defaults = PeerLimits()
    assert defaults.rate_limit == 1000
    assert defaults.block_duration == 10.0
    assert defaults.out_rate_limit == 1000

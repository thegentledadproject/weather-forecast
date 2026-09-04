"""
tests/test_startup_live_mismatch.py

P1-6 · make the live-authorisation state legible AND durable.

THE FAILURE THIS GUARDS. The deployed mode lives in /etc/polyweather/mode.env,
written once by deploy_daemon.sh and never rewritten. If that file goes
missing, scheduler.py comes up without live authorisation, and
executor.close_position() then refuses to sell every live position. Safe in
isolation -- but the result is real money on the exchange with no working
stop-loss, and a daemon that otherwise runs completely normally. Nothing
crashes. Nothing looks wrong. The condition is silent by nature.

WHAT ALREADY EXISTED, and is pinned here rather than rebuilt:
executor.warn_about_unmanageable_live_positions() already prints an
[ACTION NEEDED] block at boot, before the first cycle, naming each position
and the dollar total. Four of P1-6's five bullets shipped with it.

WHAT IS NEW: --require-live, which turns that warning into a refusal to start.
The warning is the safe default and the flag is for the systemd unit once the
operator trusts it -- a daemon that will not boot is louder than one that
boots and prints, and both are safer than one that quietly cannot sell.

WHAT MUST NOT HAPPEN: auto-promotion. Discovering a live position while in a
non-live mode is not evidence that live mode is authorised; it is evidence
that something is inconsistent. The refusal is the safe direction.
"""
from datetime import date

import pytest

import config
import executor
import scheduler
import storage
from models import Position

STATION = "WSSS"


def _live_position(size_usd=17.63):
    return Position(
        position_id=f"{STATION}:2026-09-03:32:YES:live",
        station_icao=STATION,
        target_date=date(2026, 9, 3),
        bucket_c=32,
        side="YES",
        entry_price=0.30,
        size_usd=size_usd,
        entry_time="2026-09-03T02:00:00+00:00",
        status="open",
        high_water_mark=0.30,
        size_shares=58.0,
        execution_mode="live",
        order_id="0xabc",
    )


@pytest.fixture
def stranded(monkeypatch):
    """One open live position, and a process NOT authorised to close it."""
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_live_position()])
    monkeypatch.setitem(executor.EXECUTION_MODE, STATION, "paper")


@pytest.fixture
def nothing_stranded(monkeypatch):
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [])


# ---------------------------------------------------------------------------
# The block itself -- existing behaviour, pinned
# ---------------------------------------------------------------------------

def test_an_open_live_position_in_a_non_live_mode_produces_the_action_needed_block(
    stranded, capsys
):
    """The specified acceptance case: a live position plus a non-live mode."""
    code = scheduler.enforce_live_requirement(require_live=False)
    out = capsys.readouterr().out

    assert "[ACTION NEEDED]" in out
    assert code == 0


def test_the_block_names_the_position_and_the_dollars_at_stake(stranded, capsys):
    """
    A count alone is not actionable -- an operator has to know WHICH position
    and HOW MUCH before deciding whether to go and close it by hand.
    """
    scheduler.enforce_live_requirement(require_live=False)
    out = capsys.readouterr().out

    assert STATION in out
    assert "17.63" in out


def test_a_live_position_in_live_mode_is_not_stranded(monkeypatch, capsys):
    monkeypatch.setattr(storage, "load_open_positions", lambda **kw: [_live_position()])
    monkeypatch.setitem(executor.EXECUTION_MODE, STATION, "live")

    code = scheduler.enforce_live_requirement(require_live=True)

    assert code == 0
    assert "[ACTION NEEDED]" not in capsys.readouterr().out


def test_a_clean_book_says_nothing(nothing_stranded, capsys):
    code = scheduler.enforce_live_requirement(require_live=True)

    assert code == 0
    assert "[ACTION NEEDED]" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --require-live
# ---------------------------------------------------------------------------

def test_require_live_refuses_to_start_when_a_position_is_stranded(stranded, capsys):
    """The new half of P1-6: the warning becomes a refusal."""
    code = scheduler.enforce_live_requirement(require_live=True)

    assert code != 0
    assert "--require-live" in capsys.readouterr().out


def test_without_the_flag_the_daemon_still_starts(stranded):
    """
    The warning stays the default. --require-live is for the systemd unit once
    the operator trusts it; making refusal unconditional would mean a stranded
    position also stops the paper book, which trades nothing real and is the
    only thing still working.
    """
    assert scheduler.enforce_live_requirement(require_live=False) == 0


def test_the_refusal_never_auto_promotes_the_station(stranded):
    """
    Finding a live position while in paper mode is not authorisation to go
    live -- it is evidence that something is inconsistent. Refusing is the
    safe direction; promoting is the one that spends money on a guess.
    """
    scheduler.enforce_live_requirement(require_live=True)

    assert executor.EXECUTION_MODE[STATION] == "paper"


def test_an_unreadable_book_does_not_refuse_to_start(monkeypatch, capsys):
    """
    unmanageable_live_positions() already swallows a storage failure and
    returns [] rather than stopping the daemon. --require-live must not turn
    a transient database read error into a boot loop on a book that may be
    perfectly fine -- the entry path fails closed on its own.
    """
    def boom(**kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(storage, "load_open_positions", boom)

    assert scheduler.enforce_live_requirement(require_live=True) == 0


# ---------------------------------------------------------------------------
# The flag reaches the CLI
# ---------------------------------------------------------------------------

def test_the_parser_exposes_require_live():
    args = scheduler._build_parser().parse_args(["--mode", "paper", "--require-live"])
    assert args.require_live is True


def test_require_live_defaults_off():
    args = scheduler._build_parser().parse_args(["--mode", "paper"])
    assert args.require_live is False


def test_live_still_requires_its_own_acknowledgement():
    """
    --require-live is about STRANDED positions and must not become a second
    way to arm real money. The existing two-switch requirement is unchanged.
    """
    parser = scheduler._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--mode", "live", "--require-live"])
        scheduler._check_live_acknowledgement(parser, parser.parse_args(
            ["--mode", "live", "--require-live"]
        ))

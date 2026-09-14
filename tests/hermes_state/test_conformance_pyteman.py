"""Deterministic crash/resume conformance reproducers, pyteman-driven (#80921).

Prototype for the machine-checked conformance suite asked in #80921: instead
of racing a SIGKILL against the commit window and hoping to land inside it,
pyteman rules place the kill at an EXACT call position (entry or exit of a
named symbol, with a countdown), so every run of a test traverses the same
interleaving. Properties checked here against a real ``SessionDB``:

- kill BEFORE the transaction opens (append entry): nothing of that write
  survives, database reopens clean;
- kill AFTER commit (append exit): the write survives exactly once;
- determinism: the same armed position, run twice, yields the identical
  surviving state.

Dev-side only: these tests SKIP when pyteman is not importable (it is not a
Hermes runtime dependency). Set ``PYTEMAN_SRC`` to a checkout's ``src`` dir,
or have it installed in the test environment.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_MSGS_BEFORE_KILL = 5


def _pyteman_src() -> str:
    env = os.environ.get("PYTEMAN_SRC")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "pyteman" / "src")


if _pyteman_src() not in sys.path:
    sys.path.insert(0, _pyteman_src())
pytest.importorskip("pyteman", reason="pyteman not installed (dev-side fault injector, #80921 prototype)")

from pyteman.patcher import Patcher  # noqa: E402
from pyteman.rules import Rule  # noqa: E402


def _child_writer(db_path: str, session_id: str, symbol: str, event: str, q) -> None:
    """Write N+1 messages in a fresh process; the kill rule fires at the
    (N+1)-th call of ``symbol``/``event``. os._exit means no cleanup: this is
    the SIGKILL stand-in."""
    src = _pyteman_src()
    if src not in sys.path:
        sys.path.insert(0, src)
    rule = Rule(
        id="conf-kill",
        module="hermes_state",
        symbol=symbol,
        event=event,
        action={"kind": "kill", "exit_code": 70},
        fire={"mode": "countdown", "n": _MSGS_BEFORE_KILL},  # fires on call n+1
    )
    Patcher([rule], log=None).install_hook()
    import hermes_state  # imported AFTER the hook so the rule patches it

    db = hermes_state.SessionDB(db_path=Path(db_path))
    db.ensure_session(session_id, source="conformance")
    for i in range(_MSGS_BEFORE_KILL + 1):
        db.append_message(session_id, role="user", content=f"m{i}")
    q.put("finished-without-kill")  # unreachable when the rule fires


def _run_killed_writer(tmp_path: Path, tag: str, symbol: str, event: str) -> tuple[str, int]:
    db_path = tmp_path / f"state-{tag}.db"
    q = mp.get_context("spawn").Queue()
    p = mp.get_context("spawn").Process(
        target=_child_writer, args=(str(db_path), "s-conf", symbol, event, q)
    )
    p.start()
    p.join(timeout=60)
    outcome = q.get_nowait() if not q.empty() else f"exitcode={p.exitcode}"
    con = sqlite3.connect(db_path)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        count = con.execute(
            "SELECT count(*) FROM messages WHERE session_id='s-conf'"
        ).fetchone()[0]
    finally:
        con.close()
    return outcome, count if integrity == "ok" else -1


def test_kill_at_append_entry_leaves_write_out(tmp_path):
    """Kill at the 6th append_message ENTRY (before its BEGIN): the first five
    committed messages survive, the sixth never lands, database is clean."""
    outcome, count = _run_killed_writer(tmp_path, "entry", "SessionDB.append_message", "entry")
    assert outcome.startswith("exitcode=") and outcome != "exitcode=0"
    assert count == _MSGS_BEFORE_KILL


def test_kill_at_append_exit_keeps_write_once(tmp_path):
    """Kill at the 6th append_message EXIT (after commit): the sixth message
    survives, exactly once, database is clean."""
    outcome, count = _run_killed_writer(tmp_path, "exit", "SessionDB.append_message", "exit")
    assert outcome.startswith("exitcode=") and outcome != "exitcode=0"
    assert count == _MSGS_BEFORE_KILL + 1


def test_same_position_is_deterministic(tmp_path):
    """The same armed position run twice yields the identical surviving count:
    the interleaving is a function of the rule, not of scheduler luck."""
    a = _run_killed_writer(tmp_path, "det-a", "SessionDB.append_message", "entry")
    b = _run_killed_writer(tmp_path, "det-b", "SessionDB.append_message", "entry")
    assert a == b

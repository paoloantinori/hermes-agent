"""Per-home kanban dispatch claim allowlist.

Regression tests for #110995: on a shared kanban board (one kanban.db mounted
across several Hermes homes) every home's ``profile_exists("default")`` is
unconditionally True, so any home's dispatcher could claim cards assigned to
``default``. ``kanban.dispatch_profiles`` (or the
``HERMES_KANBAN_DISPATCH_PROFILES`` env bridge) declares which assignees this
home may claim; anything else lands in ``skipped_nonspawnable``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def every_home_has_default(monkeypatch):
    """Reproduce the incident premise: ``profile_exists("default")`` is True."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


# Env bridge for the claim allowlist (mirrors
# kanban_db_dispatch.KANBAN_DISPATCH_PROFILES_ENV; spelled out so the tests
# stay red-on-base against code that lacks the constant).
_DISPATCH_PROFILES_ENV = "HERMES_KANBAN_DISPATCH_PROFILES"


@pytest.fixture
def no_env_bridge(monkeypatch):
    monkeypatch.delenv(_DISPATCH_PROFILES_ENV, raising=False)


def test_allowlist_env_restricts_default_claim(monkeypatch, every_home_has_default):
    monkeypatch.setenv(_DISPATCH_PROFILES_ENV, "sage,researcher")
    claim = kbd._profile_exists_fn()
    assert claim is not None
    assert claim("sage") is True
    assert claim("researcher") is True
    # The incident: this home must NOT claim another home's "default".
    assert claim("default") is False


def test_allowlist_env_none_claims_nothing(monkeypatch, every_home_has_default):
    monkeypatch.setenv(_DISPATCH_PROFILES_ENV, "none")
    claim = kbd._profile_exists_fn()
    assert claim is not None
    assert claim("sage") is False
    assert claim("default") is False


def test_allowlist_config_key_end_to_end(every_home_has_default, no_env_bridge):
    """Real config.yaml -> real load_config() -> gated predicate."""
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text("kanban:\n  dispatch_profiles:\n    - sage\n")
    claim = kbd._profile_exists_fn()
    assert claim is not None
    assert claim("sage") is True
    assert claim("default") is False


def test_unset_allowlist_preserves_upstream(every_home_has_default, no_env_bridge):
    claim = kbd._profile_exists_fn()
    assert claim is not None
    assert claim("default") is True


# ---------------------------------------------------------------------------
# End-to-end coverage (ported from #111206): the allowlist must gate the
# dispatcher loop, not just the predicate. #110995's cost had two halves:
# the wrong-home spawn (claim gate) and three dispatchers polling awake on
# one default card (wake-up path).
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors test_kanban_host_cap)."""
    from hermes_cli import kanban_db as kb
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _spawn_via(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


def test_dispatch_once_buckets_foreign_default_card(
    kanban_home, every_home_has_default, monkeypatch,
):
    """The incident path: a default-assigned card must not spawn in the wrong home."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.setenv(_DISPATCH_PROFILES_ENV, "sage,researcher")
    spawns: list = []
    with kbc.connect() as conn:
        foreign = kb.create_task(conn, title="foreign card", assignee="default")
        local = kb.create_task(conn, title="local card", assignee="sage")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_via(spawns))
    assert foreign in res.skipped_nonspawnable
    assert foreign not in spawns
    assert local in spawns
    assert local not in res.skipped_nonspawnable


def test_dispatch_once_env_none_claims_nothing(
    kanban_home, every_home_has_default, monkeypatch,
):
    """'none' is fail-closed end to end: no card spawns, every card is bucketed."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.setenv(_DISPATCH_PROFILES_ENV, "none")
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="any card", assignee="sage")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_via(spawns))
    assert spawns == []
    assert tid in res.skipped_nonspawnable


def test_has_spawnable_ready_sleeps_on_foreign_default_board(
    kanban_home, every_home_has_default, monkeypatch,
):
    """Wake-up path (#110995 comment: three dispatchers polled awake on one
    default card): a board holding only foreign assignees must read empty."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.setenv(_DISPATCH_PROFILES_ENV, "sage")
    with kbc.connect() as conn:
        kb.create_task(conn, title="only card", assignee="default")
        assert kbd.has_spawnable_ready(conn) is False
        kb.create_task(conn, title="our card", assignee="sage")
        assert kbd.has_spawnable_ready(conn) is True


def test_unset_env_preserves_legacy_dispatch_loop(
    kanban_home, every_home_has_default, no_env_bridge,
):
    """Unset keeplist: the default-assigned card spawns exactly as before."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy card", assignee="default")
        assert kbd.has_spawnable_ready(conn) is True
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_via(spawns))
    assert tid in spawns
    assert res.skipped_nonspawnable == []

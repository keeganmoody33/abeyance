"""The shell surface. A scheduled runner branches on these exit codes, so they are contract."""
from __future__ import annotations

import json

import pytest

from conftest import approvers, items
from abeyance.cli import EXIT_BLOCKED, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, main


@pytest.fixture
def app(loop, monkeypatch):
    """Expose the fixture loop as an importable module attribute for `--app`."""
    import types
    mod = types.ModuleType("cli_fixture")
    mod.loop = loop
    mod.execute = lambda item: {"written": True}
    mod.not_a_loop = 42
    import sys
    monkeypatch.setitem(sys.modules, "cli_fixture", mod)
    return loop


def run(argv):
    return main(["--app", "cli_fixture:loop"] + argv)


def test_pending_lists_open_proposals(app, capsys):
    res = app.propose(items(2), approvers("a@x.test"), subject_key="acme")
    assert run(["pending"]) == EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == res.id
    assert rows[0]["waiting_on"] == ["a@x.test"]


def test_poll_is_the_cheap_gate(app, transport, capsys):
    res = app.propose(items(1), approvers("a@x.test"))
    assert run(["poll"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["actionable"] == []

    transport.receive(res.id, "a@x.test", "approve 1")
    run(["poll"])
    assert json.loads(capsys.readouterr().out)["actionable"] == [res.id]


def test_read_then_record_then_apply(app, transport, capsys):
    res = app.propose(items(3), approvers("a@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1 and 3")

    run(["read", "--id", res.id])
    out = json.loads(capsys.readouterr().out)
    assert out["replies"][0]["suggestion"]["approve"] == [1, 3]
    assert "hint, not the judge" in out["note"]

    assert run(["record", "--id", res.id, "--from", "a@x.test", "--approve", "1,3"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["all_replied"] is True

    assert run(["apply", "--id", res.id, "--executor", "cli_fixture:execute"]) == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert [o["n"] for o in report["outcomes"] if o["written"]] == [1, 3]


def test_apply_returns_a_distinct_code_while_blocked(app, transport, capsys):
    res = app.propose(items(1), approvers("a@x.test", "b@x.test"))
    transport.receive(res.id, "a@x.test", "approve 1")
    run(["record", "--id", res.id, "--from", "a@x.test", "--approve", "1"])
    capsys.readouterr()

    assert run(["apply", "--id", res.id]) == EXIT_BLOCKED, \
        "a runner must be able to tell 'not yet' from 'done' without parsing prose"


def test_unknown_proposal_is_its_own_exit_code(app, capsys):
    assert run(["show", "--id", "nope"]) == EXIT_NOT_FOUND


def test_bad_app_spec_is_a_usage_error(capsys):
    assert main(["--app", "cli_fixture", "pending"]) == EXIT_USAGE
    assert main(["--app", "no.such.module:loop", "pending"]) == EXIT_USAGE


def test_a_non_loop_attribute_is_rejected(app, capsys):
    assert main(["--app", "cli_fixture:not_a_loop", "pending"]) == EXIT_USAGE


def test_inject_rehearses_without_a_mailbox(app, capsys):
    res = app.propose(items(2), approvers("a@x.test"))
    assert run(["inject", "--id", res.id, "--from", "a@x.test",
                "--text", "approve 1, skip 2"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["replies"][0]["suggestion"]["approve"] == [1]


def test_number_parsing_accepts_the_shapes_people_type(app):
    res = app.propose(items(4), approvers("a@x.test"))
    for spec in ("1,3", "1 3", "1;3", " 1 , 3 "):
        assert run(["record", "--id", res.id, "--from", "a@x.test", "--approve", spec]) == EXIT_OK
        assert app.get(res.id).approver("a@x.test").approved == [1, 3]


def test_sweep_and_nudge_are_callable_with_no_arguments(app, capsys):
    app.propose(items(1), approvers("a@x.test"))
    assert run(["sweep"]) == EXIT_OK
    assert run(["nudge", "--dry-run"]) == EXIT_OK

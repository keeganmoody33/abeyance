"""The reach ceiling. New behaviour is free; new reach costs a human.

These tests pin the boundary between the two, because the boundary is the security model.
"""
from __future__ import annotations

import json

import pytest

from abeyance import Capability, CapabilityRegistry, ContributionKind
from abeyance.errors import CapabilityMissing, ConfigurationError


def cap(name="db-evidence", **kw):
    base = dict(image="postgres:16-alpine", produces=("campaign-performance",),
                emits=ContributionKind.EVIDENCE, reach=("db-read",), app="workers-readonly")
    base.update(kw)
    return Capability(name=name, **base)


# --------------------------------------------------------------- declaring


def test_a_capability_cannot_declare_itself_able_to_decide():
    """Authority is not a capability you can deploy. If it were, the whole guarantee in
    standing.py could be bypassed by editing a registry entry."""
    with pytest.raises(ConfigurationError, match="cannot be given authority by declaring it"):
        cap(emits=ContributionKind.DECISION)


def test_a_capability_that_produces_nothing_is_refused():
    """It could never be selected, so it is a misconfiguration that would look like a mystery."""
    with pytest.raises(ConfigurationError, match="produces nothing"):
        cap(produces=())


@pytest.mark.parametrize("bad", [{"name": ""}, {"image": ""}])
def test_a_capability_needs_a_name_and_an_image(bad):
    with pytest.raises(ConfigurationError):
        cap(**bad)


def test_a_recommendation_emitting_capability_is_fine():
    c = cap(name="fit-scorer", emits=ContributionKind.RECOMMENDATION, produces=("fit-score",))
    assert c.emits is ContributionKind.RECOMMENDATION


# --------------------------------------------------------------- matching


def test_the_registry_matches_a_need_to_exactly_one_worker():
    r = CapabilityRegistry([cap()])
    assert r.match("campaign-performance").name == "db-evidence"
    assert r.match("wire-money") is None


def test_an_ambiguous_need_is_a_configuration_error_not_an_arbitrary_pick():
    """Whichever one it picked would be the one holding credentials."""
    r = CapabilityRegistry([cap("a"), cap("b")])
    with pytest.raises(ConfigurationError, match="claimed by"):
        r.match("campaign-performance")


def test_require_names_what_is_reachable_and_who_has_to_decide():
    r = CapabilityRegistry([cap()])
    with pytest.raises(CapabilityMissing) as e:
        r.require("wire-money")

    msg = str(e.value)
    assert "wire-money" in msg
    assert "campaign-performance" in msg, "the error must say what IS reachable"
    assert "human decision" in msg, "and that minting a new one is not automatic"


def test_a_duplicate_name_is_refused():
    r = CapabilityRegistry([cap()])
    with pytest.raises(ConfigurationError, match="already registered"):
        r.add(cap())


# --------------------------------------------------------------- audit


def test_the_reach_report_answers_what_can_touch_production():
    r = CapabilityRegistry([
        cap("db-evidence"),
        cap("deep-check", produces=("integrity-deep-check",)),
        cap("clickup-writer", produces=("queue-campaign",), reach=("clickup-write",),
            app="workers-write"),
    ])
    assert r.reach_report() == {"clickup-write": ["clickup-writer"],
                                "db-read": ["db-evidence", "deep-check"]}


def test_a_registry_round_trips_through_json_so_it_reviews_as_a_diff():
    r = CapabilityRegistry([cap(), cap("fit-scorer", produces=("fit-score",),
                                       emits=ContributionKind.RECOMMENDATION)])
    again = CapabilityRegistry.from_docs(json.loads(json.dumps(r.to_doc()))["capabilities"])
    assert again.to_doc() == r.to_doc()
    assert again.names() == ["db-evidence", "fit-scorer"]


def test_a_registry_loads_from_a_file(tmp_path):
    p = tmp_path / "capabilities.json"
    p.write_text(json.dumps(CapabilityRegistry([cap()]).to_doc()))
    assert CapabilityRegistry.from_file(p).names() == ["db-evidence"]


def test_an_empty_registry_reaches_nothing():
    r = CapabilityRegistry()
    assert len(r) == 0 and r.reach_report() == {}
    with pytest.raises(CapabilityMissing):
        r.require("anything")


def test_the_declared_timeout_is_what_the_lease_derives_from():
    """Not enforcement — the honest source for how long a dispatch should be trusted."""
    assert cap(timeout_seconds=45).timeout_seconds == 45
    assert cap().timeout_seconds == 600, "a default that is generous rather than surprising"

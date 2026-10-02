"""
Grounding verifier tests.

The verifier is the last thing between the model and the user, so these tests
focus on the cases the old implementation got wrong: the blanket constant
allowlist, and numbers appearing in SOP prose the model was shown.
"""
from app.engine.verifier import (
    collect_allowed_numbers,
    extract_numbers_from_text,
    requires_policy_citation,
    verify_reply,
)
from app.schemas import SOPModel

WIND_SOP = SOPModel(
    id="EXE-WIND-CYCLING-01",
    category="exercise",
    title="High Wind",
    severity="high",
    applies_to=["cycling"],
    when={"all": [{"fact": "wind_kmh", "op": ">=", "value": 35.0}]},
    advice=(
        "HIGH WIND WARNING: sustained wind of 35 km/h and gusts to 45 km/h. "
        "Avoid the road between 10:00 and 16:00."
    ),
)

UV_SOP = SOPModel(
    id="EXE-UV-HIGH-01",
    category="exercise",
    title="Severe UV",
    severity="high",
    applies_to=["running"],
    when={"all": [{"fact": "uv_max", "op": ">=", "value": 8.0}]},
    advice="Peak UV reaches {uv_max}. Avoid 10:00 to 16:00. Use SPF 50+ sunscreen.",
)

FACTS = {"wind_kmh": 36.4, "max_gust_kmh": 45.0, "uv_max": 9.2}


# --- Number extraction ---

def test_sop_ids_are_not_read_as_negative_numbers():
    assert extract_numbers_from_text("Per EXE-WIND-CYCLING-01 the wind is high.") == []


def test_clock_times_are_not_read_as_data():
    assert extract_numbers_from_text("Avoid 10:00 to 16:00") == []
    assert extract_numbers_from_text("Use SPF 50+ sunscreen") == []


def test_real_numbers_survive_extraction():
    assert extract_numbers_from_text("Wind is 36.4 km/h, gusts 45") == [36.4, 45.0]


# --- Allowlist construction ---

def test_allowed_numbers_include_thresholds_and_prose():
    allowed = collect_allowed_numbers(FACTS, [WIND_SOP, UV_SOP])
    for expected in (36.4, 45.0, 35.0, 8.0, 9.2):
        assert expected in allowed


def test_unrelated_constants_are_no_longer_blanket_allowed():
    """
    The old implementation allowlisted 24, 7, 50, 10 and 16 unconditionally,
    which let a reply state a humidity of 16% without support.
    """
    allowed = collect_allowed_numbers({}, [])
    assert 24.0 not in allowed
    assert 50.0 not in allowed


def test_prose_constants_come_from_the_sop_actually_cited():
    """10:00 and 16:00 are allowed because the cited SOP mentions them."""
    reply = (
        "High wind. See EXE-WIND-CYCLING-01: avoid the road between 10:00 and 16:00."
    )
    is_valid, errors = verify_reply(reply, FACTS, [WIND_SOP], ["EXE-WIND-CYCLING-01"])
    assert is_valid, errors


def test_threshold_from_an_uncited_sop_is_rejected():
    """
    8.0 is the UV policy's trigger, not a wind fact. Citing the wind policy
    must not license quoting it.
    """
    assert 8.0 not in collect_allowed_numbers(FACTS, [WIND_SOP])

    reply = "UV will reach 8 per EXE-WIND-CYCLING-01."
    is_valid, errors = verify_reply(reply, FACTS, [WIND_SOP], ["EXE-WIND-CYCLING-01"])
    assert not is_valid
    assert any("8" in err for err in errors)


# --- Pass and fail ---

def test_grounded_reply_passes():
    reply = (
        "Wind is 36.4 km/h with gusts to 45 km/h. Per EXE-WIND-CYCLING-01, "
        "cycling is not advised."
    )
    is_valid, errors = verify_reply(reply, FACTS, [WIND_SOP], ["EXE-WIND-CYCLING-01"])
    assert is_valid, errors


def test_invented_number_is_rejected():
    reply = "Humidity is 82% per EXE-WIND-CYCLING-01."
    is_valid, errors = verify_reply(reply, FACTS, [WIND_SOP], ["EXE-WIND-CYCLING-01"])
    assert not is_valid
    assert any("82" in err for err in errors)


def test_uncited_sop_id_is_rejected():
    reply = "Wind is 36.4 km/h per EXE-UV-HIGH-01."
    is_valid, errors = verify_reply(reply, FACTS, [WIND_SOP], ["EXE-WIND-CYCLING-01"])
    assert not is_valid
    assert any("EXE-UV-HIGH-01" in err for err in errors)


def test_rendered_advice_numbers_are_allowed():
    """Values interpolated into the advice template the model was shown."""
    advice = "Peak UV reaches 9.2. Avoid exertion."
    reply = "UV peaks at 9.2 per EXE-UV-HIGH-01."
    is_valid, errors = verify_reply(
        reply,
        {"uv_max": 9.2},
        [UV_SOP],
        ["EXE-UV-HIGH-01"],
        rendered_advice=[advice],
    )
    assert is_valid, errors


def test_reply_with_no_numbers_is_valid():
    is_valid, errors = verify_reply(
        "Not advised: EXE-WIND-CYCLING-01.", FACTS, [WIND_SOP], ["EXE-WIND-CYCLING-01"]
    )
    assert is_valid, errors


# --- Citation requirement ---

def test_citation_required_only_when_policy_matched():
    assert requires_policy_citation("Wind is high.", ["EXE-WIND-CYCLING-01"])
    assert not requires_policy_citation("Per EXE-WIND-CYCLING-01, no.", ["EXE-WIND-CYCLING-01"])
    assert not requires_policy_citation("No policy applies here.", [])
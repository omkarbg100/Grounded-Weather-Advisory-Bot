import pytest
from app.engine.location import parse_coordinates, resolve_location_decision
from app.engine.facts import derive_facts
from app.engine.timewindow import resolve_window, WindowResolutionError
from app.engine.matcher import match_sops
from app.engine.ranking import resolve_conflicts
from app.engine.verifier import verify_reply
from app.tools.sop_loader import load_sops
from app.config import SOPS_DIR
from app.schemas import LocationModel, SOPModel


# --- Location Parser Tests ---

def test_location_parse_coords():
    # Standard decimal pair
    assert parse_coordinates("23.26, 77.41") == (23.26, 77.41)
    # Labeled lat/lon
    assert parse_coordinates("lat 23.26 lon 77.41") == (23.26, 77.41)
    # Directional S/W hemisphere negations
    assert parse_coordinates("23.26S 77.41W") == (-23.26, -77.41)
    # Invalid ranges
    assert parse_coordinates("95.0, 77.41") is None
    assert parse_coordinates("23.26, 200.0") is None
    # Garbage input
    assert parse_coordinates("hello world in Bhopal") is None


def test_location_precedence():
    loc, note = resolve_location_decision(
        coords_candidate=(23.26, 77.41),
        city_text="Bhopal",
        session_location=None
    )
    assert loc.source == "coordinates"
    assert "Ignoring city text" in note

    # City fallback when coords invalid/None
    geocoded = {"lat": 23.26, "lon": 77.41, "name": "Bhopal", "country": "India"}
    loc, note = resolve_location_decision(
        coords_candidate=None,
        city_text="Bhopal",
        session_location=None,
        geocoded_result=geocoded
    )
    assert loc.source == "geocoded"
    assert loc.label == "Bhopal, India"

    # Session fallback
    sess_loc = LocationModel(lat=10.0, lon=10.0, label="Session City", source="session")
    loc, note = resolve_location_decision(
        coords_candidate=None,
        city_text=None,
        session_location=sess_loc
    )
    assert loc.source == "session"
    assert loc.label == "Session City"


# --- Facts Builder Tests ---

def test_derive_facts_this_evening():
    mock_raw = {
        "current": {"temperature_2m": 25.0, "apparent_temperature": 26.0, "weather_code": 0},
        "hourly": {
            "time": [f"2026-10-01T{h:02d}:00" for h in range(24)],
            "temperature_2m": [20.0 + h for h in range(24)],
            "apparent_temperature": [21.0 + h for h in range(24)],
            "wind_speed_10m": [10.0] * 24,
            "precipitation_probability": [5.0] * 24,
            "precipitation": [0.0] * 24,
            "uv_index": [0.0] * 24,
            "weather_code": [0] * 24,
        }
    }
    facts = derive_facts(mock_raw, time_window="this_evening")
    assert facts["is_daytime_window"] == 0
    # Evening hours 18 to 22: max temp is 20 + 22 = 42
    assert facts["temp_c"] == 42.0


def test_resolve_window_by_timestamp_not_assumed_index():
    """A payload that does not start at local midnight must still resolve."""
    times = [f"2026-10-01T{h:02d}:00" for h in range(6, 24)]
    window = resolve_window(times, "this_evening")
    assert window.start_index == 12   # 18:00 is the 13th entry of a 06:00 start
    assert times[window.start_index].endswith("T18:00")
    assert window.is_daytime_window is False

    tomorrow = resolve_window([f"2026-10-0{d}T{h:02d}:00" for d in (1, 2) for h in range(24)], "tomorrow")
    assert tomorrow.start_index == 24
    assert tomorrow.end_index == 48


def test_resolve_window_custom_iso_bounds():
    times = [f"2026-10-01T{h:02d}:00" for h in range(24)]
    window = resolve_window(times, "custom", "2026-10-01T10:00", "2026-10-01T12:00")
    assert window.start_index == 10
    assert window.end_index == 13


def test_resolve_window_short_payload_degrades():
    """A 5-hour forecast must not raise; it falls back to what it has."""
    window = resolve_window([f"2026-10-01T{h:02d}:00" for h in range(5)], "this_evening")
    assert window.start_index == 0
    assert window.end_index == 5


def test_unknown_window_name_rejected():
    with pytest.raises(WindowResolutionError):
        resolve_window(["2026-10-01T00:00"], "next_decade")


# --- Matcher Boundary & Fuzzy Tests ---

def test_matcher_boundary():
    registry = load_sops(SOPS_DIR)
    
    # Test high wind cycling threshold (wind_kmh >= 35.0)
    facts_below = {"wind_kmh": 34.9, "max_gust_kmh": 30.0}
    matched_below = match_sops(registry, "cycling", facts_below)
    assert not any(sop.id == "EXE-WIND-CYCLING-01" for sop, _ in matched_below)

    facts_at_threshold = {"wind_kmh": 35.0, "max_gust_kmh": 30.0}
    matched_at = match_sops(registry, "cycling", facts_at_threshold)
    assert any(sop.id == "EXE-WIND-CYCLING-01" for sop, _ in matched_at)


def test_matcher_fuzzy_picnic():
    registry = load_sops(SOPS_DIR)

    # Ideal picnic conditions
    facts_good = {"temp_c": 22.0, "precip_prob_max": 5, "wind_kmh": 10.0, "uv_max": 4.0}
    matched = match_sops(registry, "picnic", facts_good)
    picnic_matches = [item for item in matched if item[0].id == "LEI-PICNIC-01"]
    assert len(picnic_matches) == 1
    assert "EXCELLENT PICNIC CONDITIONS" in picnic_matches[0][1]

    # Poor picnic conditions (high rain prob, strong wind)
    facts_poor = {"temp_c": 12.0, "precip_prob_max": 80, "wind_kmh": 40.0, "uv_max": 9.0}
    matched_poor = match_sops(registry, "picnic", facts_poor)
    picnic_poor_matches = [item for item in matched_poor if item[0].id == "LEI-PICNIC-01"]
    assert len(picnic_poor_matches) == 1
    assert "POOR PICNIC CONDITIONS" in picnic_poor_matches[0][1]


# --- Ranking & Conflict Resolution Tests ---

def test_ranking_precedence_and_severity():
    sop_override = SOPModel(
        id="SIT-RAIN-SYSTEM-01",
        category="situational",
        title="Severe Rain",
        severity="critical",
        applies_to=["*"],
        precedence="override",
        when={"all": [{"fact": "temp_c", "op": ">", "value": 0}]},
        advice="Override advice"
    )
    sop_high = SOPModel(
        id="EXE-WIND-CYCLING-01",
        category="exercise",
        title="High Wind",
        severity="high",
        applies_to=["cycling"],
        when={"all": [{"fact": "temp_c", "op": ">", "value": 0}]},
        advice="High wind advice"
    )
    sop_info = SOPModel(
        id="TRV-LIGHT-RAIN-01",
        category="travel",
        title="Light Rain",
        severity="info",
        applies_to=["commute"],
        when={"all": [{"fact": "temp_c", "op": ">", "value": 0}]},
        advice="Info advice"
    )

    items = [
        (sop_info, "Info advice"),
        (sop_override, "Override advice"),
        (sop_high, "High wind advice"),
    ]

    res = resolve_conflicts(items)
    assert res["primary"][0].id == "SIT-RAIN-SYSTEM-01"
    assert res["all_sop_ids"] == ["SIT-RAIN-SYSTEM-01", "EXE-WIND-CYCLING-01", "TRV-LIGHT-RAIN-01"]


# --- Verifier Tests ---

def test_verifier_pass_and_failures():
    sop = SOPModel(
        id="EXE-WIND-CYCLING-01",
        category="exercise",
        title="High Wind",
        severity="high",
        applies_to=["cycling"],
        when={"all": [{"fact": "wind_kmh", "op": ">=", "value": 35.0}]},
        advice="High wind warning: 35.0 km/h wind."
    )
    facts = {"wind_kmh": 35.0, "max_gust_kmh": 45.0}

    # Pass case
    valid_reply = "High wind warning: sustained wind of 35.0 km/h and gusts up to 45.0 km/h. Per EXE-WIND-CYCLING-01, take care."
    is_valid, errors = verify_reply(valid_reply, facts, [sop], ["EXE-WIND-CYCLING-01"])
    assert is_valid, f"Errors: {errors}"

    # Rejects unallowed invented number (e.g. 99.9)
    bad_num_reply = "Wind is 35.0 km/h but humidity is 99.9%. Per EXE-WIND-CYCLING-01."
    is_valid, errors = verify_reply(bad_num_reply, facts, [sop], ["EXE-WIND-CYCLING-01"])
    assert not is_valid
    assert any("99.9" in err for err in errors)

    # Rejects unknown/uncited SOP ID
    bad_sop_reply = "Wind is 35.0 km/h per SOP-UNKNOWN-99."
    is_valid, errors = verify_reply(bad_sop_reply, facts, [sop], ["EXE-WIND-CYCLING-01"])
    assert not is_valid
    assert any("SOP-UNKNOWN-99" in err for err in errors)

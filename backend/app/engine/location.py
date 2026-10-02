import re
from typing import Optional, Tuple, Dict, Any
from app.schemas import LocationModel


COORD_REGEXES = [
    # Pattern 1: Directional coordinates e.g. "23.26N, 77.41E" or "23.26 N 77.41 E" or "23.26S 77.41W"
    re.compile(r"(\d+(?:\.\d+)?)\s*([NSns])\s*,?\s*(\d+(?:\.\d+)?)\s*([EWew])"),
    # Pattern 2: Explicit lat/lon keywords e.g. "lat 23.26 lon 77.41" or "lat: 23.26, lon: -77.41"
    re.compile(r"lat(?:itude)?\s*:?\s*(-?\d+(?:\.\d+)?)\s*,?\s*lon(?:gitude)?\s*:?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
    # Pattern 3: Standard comma-separated numbers e.g. "23.26, 77.41" or "-23.26, -77.41"
    re.compile(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)"),
]


def validate_lat_lon(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """
    Extracts coordinates from text using regex. Supports:
    - Directional: 23.26N 77.41E
    - Keyword labeled: lat 23.26 lon 77.41
    - Standard decimal pair: 23.26, 77.41
    """
    if not text:
        return None

    for pattern in COORD_REGEXES:
        match = pattern.search(text)
        if match:
            groups = match.groups()
            if len(groups) == 4:  # Directional pattern
                lat_val = float(groups[0]) * (-1 if groups[1].upper() == "S" else 1)
                lon_val = float(groups[2]) * (-1 if groups[3].upper() == "W" else 1)
            else:
                lat_val = float(groups[0])
                lon_val = float(groups[1])

            if validate_lat_lon(lat_val, lon_val):
                return (lat_val, lon_val)

    return None


def resolve_location_decision(
    coords_candidate: Optional[Tuple[float, float]],
    city_text: Optional[str],
    session_location: Optional[LocationModel],
    geocoded_result: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[LocationModel], str]:
    """
    Resolves location based on precedence rules:
    valid coordinates > city text (geocoded) > session last location > ask for clarification.
    Returns (LocationModel or None, location_note_string).
    """
    # Case 1: Valid coordinates supplied
    if coords_candidate is not None:
        lat, lon = coords_candidate
        note = f"Using provided coordinates ({lat:.2f}, {lon:.2f})."
        if city_text:
            note += f" (Ignoring city text '{city_text}' in favor of exact coordinates)."
        label = f"Coordinates ({lat:.2f}, {lon:.2f})"
        loc = LocationModel(lat=lat, lon=lon, label=label, source="coordinates")
        return loc, note

    # Case 2: City text present
    if city_text:
        if geocoded_result:
            lat = geocoded_result["lat"]
            lon = geocoded_result["lon"]
            city_name = geocoded_result.get("name", city_text)
            admin1 = geocoded_result.get("admin1", "")
            country = geocoded_result.get("country", "")
            
            label_parts = [p for p in [city_name, admin1, country] if p]
            label = ", ".join(label_parts)
            loc = LocationModel(lat=lat, lon=lon, label=label, source="geocoded")
            note = f"Geocoded location: {label}."
            return loc, note
        # Geocode failed or pending
        return None, f"Could not geocode city '{city_text}'."

    # Case 3: Session location fallback
    if session_location is not None:
        loc = LocationModel(
            lat=session_location.lat,
            lon=session_location.lon,
            label=session_location.label,
            source="session"
        )
        note = f"Reusing session location: {session_location.label}."
        return loc, note

    # Case 4: No location available
    return None, "No location provided."

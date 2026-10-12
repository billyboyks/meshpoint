"""Coordinate sanity helpers shared by decoders and storage."""

from __future__ import annotations


def valid_coordinates(lat: float, lon: float) -> tuple[float | None, float | None]:
    """Return (lat, lon), or (None, None) when they are not a real position.

    A node with no GPS fix reports latitude_i = longitude_i = 0. Storing that
    as 0.0 / 0.0 puts the node on "Null Island" and stacks every no-fix node
    on one map marker. Out-of-range values are likewise rejected.
    """
    if lat == 0 and lon == 0:
        return None, None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, None
    return lat, lon

"""ZIP geocoding, distance and drive time (F7.5, F7.6).

Privacy rule 6: only a 5-digit ZIP ever leaves the app (to Nominatim). OSRM
gets the ZIP centroid and the trial site's public coordinates, never an
address.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from agent import http_cache
from agent.audit import audited
from agent.policy import get_policy
from agent.schemas import NearestSite, Origin, TrialSite, Zip
from pydantic import TypeAdapter

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a_lon},{a_lat};{b_lon},{b_lat}"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/{coords}"
OSRM_TABLE_CHUNK = 90  # destinations per table request (public server caps coordinates per request)
EARTH_RADIUS_MILES = 3958.8

_zip = TypeAdapter(Zip)


class GeoError(Exception):
    pass


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


@audited("geocode_zip")
def geocode_zip(zip_code: str, case_id: str | None = None) -> Origin:
    """ZIP centroid. Rejects anything that is not a bare 5-digit ZIP."""
    if "zip" not in get_policy().privacy.geocode_fields_allowed:
        raise GeoError("policy privacy.geocode_fields_allowed does not allow zip")
    zip_code = _zip.validate_python(zip_code)  # rule 6: never a full address

    if http_cache.source_mode() == "demo":
        table = http_cache.load_demo("nominatim/zip_lookup.json")
        if zip_code not in table:
            raise GeoError(f"ZIP {zip_code} is not in the demo cache; set CORTEXA_TRIAL_SOURCE=live")
        hit = table[zip_code]
        return Origin(zip=zip_code, lat=hit["lat"], lon=hit["lon"])

    got = http_cache.get_json(
        "nominatim",
        NOMINATIM_URL,
        {"postalcode": zip_code, "country": "us", "format": "jsonv2", "limit": 1},
        case_id=case_id,
    )
    if not got.data:
        raise GeoError(f"Nominatim found no centroid for ZIP {zip_code}")
    return Origin(zip=zip_code, lat=float(got.data[0]["lat"]), lon=float(got.data[0]["lon"]))


@audited("drive_time")
def drive_minutes(origin: Origin, site: TrialSite, case_id: str | None = None) -> int | None:
    """OSRM drive time in minutes, or None if no route or OSRM is down."""
    if site.lat is None or site.lon is None:
        return None
    if http_cache.source_mode() == "demo":
        return _demo_drive_minutes(origin, site)

    url = OSRM_URL.format(a_lon=origin.lon, a_lat=origin.lat, b_lon=site.lon, b_lat=site.lat)
    try:
        got = http_cache.get_json("osrm", url, {"overview": "false"}, case_id=case_id)
    except http_cache.FetchError:
        return None
    routes = got.data.get("routes") or []
    if got.data.get("code") != "Ok" or not routes:
        return None
    return round(routes[0]["duration"] / 60)


def _demo_drive_minutes(origin: Origin, site: TrialSite) -> int | None:
    table = http_cache.load_demo("osrm/drive_times.json")
    if table.get("origin_zip") != origin.zip:
        return None
    route = table["routes"].get(site.zip or "")
    return None if route is None else int(route["drive_minutes"])


@audited("drive_time_table")
def drive_minutes_many(origin: Origin, sites: list[TrialSite], case_id: str | None = None) -> list[int | None]:
    """Drive minutes from the ZIP centroid to each site, in order (None = unavailable).

    One OSRM `table` request per OSRM_TABLE_CHUNK sites instead of one `route`
    request per site, so a nationwide shortlist of hundreds of trials takes
    seconds and stays within the public server's fair use. Rule 6 still holds:
    only the ZIP centroid and the sites' public coordinates are sent.
    """
    out: list[int | None] = [None] * len(sites)
    have = [i for i, s in enumerate(sites) if s.lat is not None and s.lon is not None]
    if http_cache.source_mode() == "demo":
        for i in have:
            out[i] = _demo_drive_minutes(origin, sites[i])
        return out
    for k in range(0, len(have), OSRM_TABLE_CHUNK):
        chunk = have[k:k + OSRM_TABLE_CHUNK]
        coords = ";".join([f"{origin.lon},{origin.lat}"] + [f"{sites[i].lon},{sites[i].lat}" for i in chunk])
        try:
            got = http_cache.get_json("osrm", OSRM_TABLE_URL.format(coords=coords),
                                      {"sources": "0", "annotations": "duration"}, case_id=case_id)
        except http_cache.FetchError:
            continue
        rows = got.data.get("durations") or []
        if got.data.get("code") != "Ok" or not rows:
            continue
        for i, secs in zip(chunk, rows[0][1:]):
            out[i] = None if secs is None else round(secs / 60)
    return out


def closest(origin: Origin, sites: Iterable[TrialSite]) -> tuple[float, TrialSite] | None:
    """(straight-line miles, site) for the closest site with coordinates."""
    best: tuple[float, TrialSite] | None = None
    for s in sites:
        if s.lat is None or s.lon is None:
            continue
        d = haversine_miles(origin.lat, origin.lon, s.lat, s.lon)
        if best is None or d < best[0]:
            best = (d, s)
    return best


def as_nearest(best: tuple[float, TrialSite], drive: int | None = None) -> NearestSite:
    miles, site = best
    return NearestSite(facility=site.facility, city=site.city, state=site.state, lat=site.lat,
                       lon=site.lon, distance_miles=round(miles, 1), drive_minutes=drive)


def nearest_site(
    origin: Origin,
    sites: Iterable[TrialSite],
    *,
    with_drive_time: bool = True,
    case_id: str | None = None,
) -> NearestSite | None:
    """Closest site by straight line from the ZIP centroid; OSRM only for that one."""
    best = closest(origin, sites)
    if best is None:
        return None
    miles, site = best
    return NearestSite(
        facility=site.facility,
        city=site.city,
        state=site.state,
        lat=site.lat,
        lon=site.lon,
        distance_miles=round(miles, 1),
        drive_minutes=drive_minutes(origin, site, case_id=case_id) if with_drive_time else None,
    )

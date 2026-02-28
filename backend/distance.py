"""Geocode addresses and calculate driving distance/time using free APIs."""
import logging
import re
import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

# Patterns that confuse geocoders — strip them for a cleaner query
_UNIT_RE = re.compile(
    r'\b(?:unit|suite|ste|apt|#|dock|bay|floor)\s*[#]?\s*\w+\b',
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r'\([^)]*\)')


def _clean_address(address: str) -> str:
    """Strip unit/suite/dock info that confuses Nominatim."""
    cleaned = _PAREN_RE.sub('', address)
    cleaned = _UNIT_RE.sub('', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip().strip(',').strip()
    return cleaned


async def geocode(address: str) -> tuple[float, float] | None:
    """Geocode an address to (lat, lon) using Nominatim. Retries with cleaned address."""
    for attempt_addr in [address, _clean_address(address)]:
        if not attempt_addr:
            continue
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(NOMINATIM_URL, params={
                    "q": attempt_addr,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "ca",
                }, headers={"User-Agent": "HiBidSniper/1.0"})
            results = resp.json()
            if results:
                logger.info(f"Geocoded '{attempt_addr}' → {results[0]['lat']}, {results[0]['lon']}")
                return float(results[0]["lat"]), float(results[0]["lon"])
        except Exception as e:
            logger.warning(f"Geocoding failed for '{attempt_addr}': {e}")
    return None


async def get_driving_distance(origin: str, destination: str) -> dict | None:
    """Calculate driving distance and time between two addresses.

    Returns: {"distance_km": float, "drive_minutes": float} or None on failure.
    """
    origin_coords = await geocode(origin)
    if not origin_coords:
        logger.warning(f"Could not geocode origin: {origin}")
        return None

    dest_coords = await geocode(destination)
    if not dest_coords:
        logger.warning(f"Could not geocode destination: {destination}")
        return None

    # OSRM expects lon,lat (not lat,lon)
    origin_str = f"{origin_coords[1]},{origin_coords[0]}"
    dest_str = f"{dest_coords[1]},{dest_coords[0]}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{OSRM_URL}/{origin_str};{dest_str}",
                params={"overview": "false"},
                headers={"User-Agent": "HiBidSniper/1.0"},
            )
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            return {
                "distance_km": round(route["distance"] / 1000, 1),
                "drive_minutes": round(route["duration"] / 60, 0),
            }
    except Exception as e:
        logger.warning(f"OSRM routing failed: {e}")
    return None

from __future__ import annotations

from pathlib import Path

from garmin_client import get_garmin_client


GPX_OUT_DIR = Path("gpx")


def export_gpx_for_activity(activity_id: int) -> Path:
    """Download a Garmin activity GPX into the existing GPX cache layout."""
    from garminconnect import Garmin

    api = get_garmin_client()
    payload = api.download_activity(
        str(int(activity_id)),
        Garmin.ActivityDownloadFormat.GPX,
    )
    if not payload:
        raise RuntimeError(f"No GPX data returned for activity {activity_id}")

    GPX_OUT_DIR.mkdir(exist_ok=True)
    out_path = GPX_OUT_DIR / f"activity_{int(activity_id)}.gpx"
    out_path.write_bytes(payload)
    return out_path

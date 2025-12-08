from __future__ import annotations
import json
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List

import requests
import gpxpy
import gpxpy.gpx

"""
strava_tracks.py

Description:
    Utilities for fetching Strava activity data (streams) and converting them
    into geospatial and GPX formats. This module provides helpers to manage
    OAuth tokens, call the Strava API for activity details and streams, and
    export tracks as a GeoJSON FeatureCollection (LineStrings) or as individual
    GPX files suitable for import into mapping applications.

Usage summary:
    - Configuration:
        * Set CLIENT_ID and CLIENT_SECRET for your Strava application.
        * Provide an ACTIVITY_ID for ad-hoc GPX export (optional).
        * Ensure the following files/paths are configured as desired:
            - TOKEN_PATH: JSON file containing Strava OAuth tokens (access_token,
              refresh_token, expires_at).
            - ACTIVITIES_PATH: JSON array of activity summaries (from
              /athlete/activities) for building tracks.geojson.
            - TRACKS_GEOJSON_PATH: output path for combined GeoJSON.
            - GPX_OUT_DIR: directory for on-demand GPX exports.

    - Typical flows:
        1. Token management:
            * load_tokens() reads existing token JSON (raises if missing).
            * save_tokens(tokens) writes token JSON.
            * refresh_access_token(tokens) refreshes tokens if expired and saves them.
            * get_access_token() loads tokens and returns a valid access token.

        2. Fetching Strava data:
            * get_activity_detail(activity_id, access_token) -> activity dict
              (summary/detail information).
            * get_activity_streams(activity_id, access_token) -> streams dict
              (time, latlng, altitude, heartrate streams).

        3. Converting streams:
            * streams_to_geojson_feature(activity, streams) -> GeoJSON Feature
              (LineString) or None when no GPS points.
            * build_tracks_geojson(limit=None) iterates activities from
              ACTIVITIES_PATH, fetches streams, and writes a FeatureCollection to
              TRACKS_GEOJSON_PATH. Skips virtual/manual activities and those
              without GPS.
            * streams_to_gpx(activity, streams, out_path) -> Path writes a GPX
              file containing a single track/segment with points (lat, lon,
              elevation, timestamp) and returns the output path.
            * export_gpx_for_activity(activity_id) -> Path fetches activity
              detail/streams and writes a GPX file under GPX_OUT_DIR.

Important details and assumptions:
    - Token file format:
        {
            "access_token": "<token>",
            "refresh_token": "<refresh_token>",
            "expires_at": <unix_timestamp>
      refresh_access_token will update access_token, refresh_token, and expires_at
      when a refresh is performed.

    - Activities file format:
        Expect a JSON array of activity summary objects as returned by
        Strava's /athlete/activities endpoint. Each activity must contain an
        "id" and commonly includes "name", "start_date", "sport_type", "manual",
        etc.

    - Time handling:
        The code expects activity["start_date"] in ISO 8601 format (UTC with
        trailing "Z" or local). When present, timestamps from the Strava "time"
        stream are interpreted as seconds since the start_date and combined to
        create per-point datetime objects in generated GPX files.

    - Streams:
        The logic expects streams keyed by "latlng", "time", "altitude",
        "heartrate". Missing optional streams (altitude/heartrate/time) are
        handled by filling with None or zeros as appropriate; GPX points will
        omit values when not available.

    - Dependencies:
        * requests
        * gpxpy
        * Python 3.8+ (typing annotations use builtins like tuple[str, ...])

    - Error handling:
        Network/API errors raise HTTP exceptions (requests.raise_for_status).
        Missing token or activities files raise RuntimeError with guidance.

Security note:
    Do not commit CLIENT_SECRET, CLIENT_ID, or token files to public source
    control. Store credentials securely and ensure token storage has proper
    filesystem permissions.

Example invocations (conceptual):
    - Build a global tracks.geojson from activities.json:
        build_tracks_geojson(limit=None)

    - Export a single activity to GPX:
        export_gpx_for_activity(16673694374)

This docstring documents behavior and expectations for each helper function,
how to configure the script, and the main entry points for building GeoJSON
and exporting GPX.
"""



# ------------- CONFIG: EDIT THESE -------------
CLIENT_ID = "127989"
CLIENT_SECRET = "bdec5d496e731b5fd70526c0215d466b3fe70df7"
ACTIVITY_ID = 16673694374  # <-- put your Strava activity ID here

TOKEN_PATH = Path("strava_tokens.json")
ACTIVITIES_PATH = Path("strava_activities.json")  # list from /athlete/activities
TRACKS_GEOJSON_PATH = Path("tracks.geojson")
GPX_OUT_DIR = Path("gpx")  # directory for on-demand GPX exports
# ----------------------------------------------


# ============ TOKEN HELPERS ============

def load_tokens() -> Dict[str, Any]:
    if not TOKEN_PATH.exists():
        raise RuntimeError(
            f"{TOKEN_PATH} not found. Run your auth script to create it."
        )
    return json.loads(TOKEN_PATH.read_text())


def save_tokens(tokens: Dict[str, Any]) -> None:
    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))


def refresh_access_token(tokens: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    now = int(_time.time())
    if tokens["expires_at"] > now + 60:
        return tokens["access_token"], tokens

    url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }
    resp = requests.post(url, data=payload)
    if resp.status_code != 200:
        print("Token refresh failed:")
        print("Status:", resp.status_code)
        print("Response:", resp.text)
        resp.raise_for_status()

    data = resp.json()
    tokens["access_token"] = data["access_token"]
    tokens["refresh_token"] = data["refresh_token"]
    tokens["expires_at"] = data["expires_at"]
    save_tokens(tokens)
    return tokens["access_token"], tokens


def get_access_token() -> str:
    tokens = load_tokens()
    access_token, _ = refresh_access_token(tokens)
    return access_token


# ============ STRAVA API HELPERS ============

def get_activity_detail(activity_id: int, access_token: str) -> Dict[str, Any]:
    url = f"https://www.strava.com/api/v3/activities/{activity_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"include_all_efforts": "false"}
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def get_activity_streams(activity_id: int, access_token: str) -> Dict[str, Any]:
    """Get time, latlng, altitude, heartrate streams for an activity."""
    url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "keys": "time,latlng,altitude,heartrate",
        "key_by_type": "true",
    }
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        print(f"Streams failed for {activity_id}: {resp.status_code}")
        print(resp.text[:500])
        resp.raise_for_status()
    return resp.json()


# ============ STREAMS → GEOJSON ============

def streams_to_geojson_feature(
    activity: Dict[str, Any],
    streams: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Convert streams to a GeoJSON Feature (LineString)."""

    latlng: List[List[float]] = streams.get("latlng", {}).get("data", [])
    if not latlng:
        # no GPS, skip
        return None

    # basic props from activity summary/detail
    props = {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "sport_type": activity.get("sport_type") or activity.get("type"),
        "start_date": activity.get("start_date"),
        "start_date_local": activity.get("start_date_local"),
        "distance": activity.get("distance"),  # m
        "moving_time": activity.get("moving_time"),  # s
        "elapsed_time": activity.get("elapsed_time"),  # s
        "total_elevation_gain": activity.get("total_elevation_gain"),  # m
        "has_heartrate": activity.get("has_heartrate"),
    }

    feature = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            # GeoJSON expects [lon, lat]
            "coordinates": [[lon, lat] for lat, lon in latlng],
        },
        "properties": props,
    }
    return feature


def build_tracks_geojson(limit: int | None = None) -> None:
    """Loop over activities and build a tracks.geojson FeatureCollection."""
    if not ACTIVITIES_PATH.exists():
        raise RuntimeError(
            f"{ACTIVITIES_PATH} not found. Run your strava_activities fetch script first."
        )

    activities = json.loads(ACTIVITIES_PATH.read_text())
    # Most recent first in Strava response; reverse for nicer iteration if you want
    activities = list(activities)

    if limit is not None:
        activities = activities[:limit]

    access_token = get_access_token()

    features: List[Dict[str, Any]] = []
    total = len(activities)
    for i, act in enumerate(activities, start=1):
        act_id = act["id"]
        # Skip obvious non-GPS stuff if you want:
        if act.get("sport_type") in {"VirtualRide", "VirtualRun"}:
            continue
        if act.get("manual"):
            continue

        print(f"[{i}/{total}] Activity {act_id}: {act.get('name')}")

        try:
            streams = get_activity_streams(act_id, access_token)
        except Exception as e:
            print(f"  -> skipping (streams error: {e})")
            continue

        feat = streams_to_geojson_feature(act, streams)
        if feat is None:
            print("  -> no GPS, skipping")
            continue

        features.append(feat)

    fc = {
        "type": "FeatureCollection",
        "features": features,
    }
    TRACKS_GEOJSON_PATH.write_text(json.dumps(fc))
    print(f"Wrote {len(features)} tracks to {TRACKS_GEOJSON_PATH.resolve()}")


# ============ STREAMS → GPX (ON DEMAND) ============

def streams_to_gpx(
    activity: Dict[str, Any],
    streams: Dict[str, Any],
    out_path: Path,
) -> Path:
    """Convert Strava streams to a GPX file."""

    times: List[int] = streams.get("time", {}).get("data", [])
    latlng: List[List[float]] = streams.get("latlng", {}).get("data", [])
    altitude: List[float] = streams.get("altitude", {}).get("data", [])
    heartrate: List[int] = streams.get("heartrate", {}).get("data", [])

    if not latlng:
        raise RuntimeError("No lat/lon data in streams (no GPS?)")

    n = min(
        len(latlng),
        len(times) if times else len(latlng),
        len(altitude) if altitude else len(latlng),
        len(heartrate) if heartrate else len(latlng),
    )
    latlng = latlng[:n]
    times = times[:n] if times else [0] * n
    altitude = altitude[:n] if altitude else [None] * n
    heartrate = heartrate[:n] if heartrate else [None] * n

    start_str = activity.get("start_date")
    if start_str and start_str.endswith("Z"):
        start_str = start_str[:-1]
    start_time = datetime.fromisoformat(start_str) if start_str else None

    gpx = gpxpy.gpx.GPX()
    gpx_track = gpxpy.gpx.GPXTrack(
        name=activity.get("name", f"Activity {activity.get('id')}")
    )
    gpx.tracks.append(gpx_track)
    gpx_segment = gpxpy.gpx.GPXTrackSegment()
    gpx_track.segments.append(gpx_segment)

    for (lat, lon), dt_s, ele, hr in zip(latlng, times, altitude, heartrate):
        if start_time is not None:
            point_time = start_time + timedelta(seconds=dt_s)
        else:
            point_time = None

        point = gpxpy.gpx.GPXTrackPoint(
            latitude=lat,
            longitude=lon,
            elevation=ele,
            time=point_time,
        )

        # HR is available in `hr` if you want to use it later in Python.
        gpx_segment.points.append(point)

    out_path.write_text(gpx.to_xml())
    return out_path


def export_gpx_for_activity(activity_id: int) -> Path:
    """On-demand GPX for a single activity."""
    access_token = get_access_token()

    activity = get_activity_detail(activity_id, access_token)
    streams = get_activity_streams(activity_id, access_token)

    GPX_OUT_DIR.mkdir(exist_ok=True)
    out_path = GPX_OUT_DIR / f"activity_{activity_id}.gpx"
    path = streams_to_gpx(activity, streams, out_path)
    return path


# ============ MAIN ============

if __name__ == "__main__":
    # ---- MODE 1: build global tracks.geojson for mapping ----
    # Set a small limit first to test, e.g. limit=20
    # build_tracks_geojson(limit=None)

    # ---- MODE 2: on-demand GPX for a specific activity ----
    # Uncomment to test:
    some_id = ACTIVITY_ID
    gpx_file = export_gpx_for_activity(some_id)
    print("Wrote GPX:", gpx_file.resolve())
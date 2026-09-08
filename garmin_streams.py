from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from garmin_client import get_garmin_client


DETAILS_DIR = Path("garmin_activity_details")
FIT_DIR = Path("fit_files")
ORIGINAL_DIR = Path("garmin_originals")


def _descriptor_map(detail: dict[str, Any]) -> dict[str, int]:
    descriptors = detail.get("metricDescriptors")
    if not isinstance(descriptors, list):
        return {}
    mapping: dict[str, int] = {}
    for index, descriptor in enumerate(descriptors):
        if not isinstance(descriptor, dict):
            continue
        key = descriptor.get("key") or descriptor.get("metricsKey")
        metric_index = descriptor.get("metricsIndex", index)
        if key is not None:
            mapping[str(key)] = int(metric_index)
    return mapping


def _value_at(metrics: list[Any], index: int | None) -> Any:
    if index is None or index >= len(metrics):
        return None
    return metrics[index]


def _first_key(mapping: dict[str, int], candidates: tuple[str, ...]) -> int | None:
    for key in candidates:
        if key in mapping:
            return mapping[key]
    return None


def get_activity_details_cached(activity_id: int, use_cache: bool = True) -> dict[str, Any]:
    DETAILS_DIR.mkdir(exist_ok=True)
    path = DETAILS_DIR / f"activity_{int(activity_id)}_details.json"
    if use_cache and path.exists():
        return json.loads(path.read_text())

    api = get_garmin_client()
    detail = api.get_activity_details(str(int(activity_id)), 2000, 4000)
    path.write_text(json.dumps(detail, indent=2, default=str))
    return detail


def stream_from_activity_details(
    detail: dict[str, Any],
    value_keys: tuple[str, ...],
) -> tuple[list[float], list[float]]:
    mapping = _descriptor_map(detail)
    rows = detail.get("activityDetailMetrics")
    if not mapping or not isinstance(rows, list):
        return [], []

    time_idx = _first_key(
        mapping,
        (
            "directTimestamp",
            "sumElapsedDuration",
            "sumDuration",
            "sumMovingDuration",
        ),
    )
    value_idx = _first_key(mapping, value_keys)
    if time_idx is None or value_idx is None:
        return [], []

    raw_times: list[float] = []
    values: list[float] = []
    for row in rows:
        metrics = row.get("metrics") if isinstance(row, dict) else None
        if not isinstance(metrics, list):
            continue
        raw_time = _value_at(metrics, time_idx)
        value = _value_at(metrics, value_idx)
        if raw_time is None or value is None:
            continue
        try:
            raw_times.append(float(raw_time))
            values.append(float(value))
        except (TypeError, ValueError):
            continue

    if not raw_times:
        return [], []

    # directTimestamp is epoch milliseconds. Garmin duration metrics are seconds.
    if "directTimestamp" in mapping and time_idx == mapping["directTimestamp"]:
        first = raw_times[0]
        times = [(ts - first) / 1000.0 for ts in raw_times]
    else:
        times = raw_times
    return times, values


def _download_original(activity_id: int, use_cache: bool = True) -> Path:
    from garminconnect import Garmin

    ORIGINAL_DIR.mkdir(exist_ok=True)
    path = ORIGINAL_DIR / f"activity_{int(activity_id)}_original.zip"
    if use_cache and path.exists():
        return path

    api = get_garmin_client()
    payload = api.download_activity(
        str(int(activity_id)),
        Garmin.ActivityDownloadFormat.ORIGINAL,
    )
    if not payload:
        raise RuntimeError(f"No original/FIT data returned for activity {activity_id}")
    path.write_bytes(payload)
    return path


def _extract_fit(activity_id: int, use_cache: bool = True) -> Path:
    FIT_DIR.mkdir(exist_ok=True)
    fit_path = FIT_DIR / f"activity_{int(activity_id)}.fit"
    if use_cache and fit_path.exists():
        return fit_path

    original = _download_original(activity_id, use_cache=use_cache)
    if zipfile.is_zipfile(original):
        with zipfile.ZipFile(original) as archive:
            fit_names = [name for name in archive.namelist() if name.lower().endswith(".fit")]
            if not fit_names:
                raise RuntimeError(f"No FIT file inside original Garmin download for {activity_id}")
            fit_path.write_bytes(archive.read(fit_names[0]))
            return fit_path

    fit_path.write_bytes(original.read_bytes())
    return fit_path


def stream_from_fit(
    activity_id: int,
    field_name: str,
    use_cache: bool = True,
) -> tuple[list[float], list[float]]:
    try:
        from fitparse import FitFile
    except ImportError as exc:
        raise RuntimeError("Missing dependency: fitparse") from exc

    fit_path = _extract_fit(activity_id, use_cache=use_cache)
    fit = FitFile(str(fit_path))

    times: list[float] = []
    values: list[float] = []
    first_timestamp: datetime | None = None
    for record in fit.get_messages("record"):
        row = {field.name: field.value for field in record}
        timestamp = row.get("timestamp")
        value = row.get(field_name)
        if timestamp is None or value is None:
            continue
        if first_timestamp is None:
            first_timestamp = timestamp
        try:
            times.append(float((timestamp - first_timestamp).total_seconds()))
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return times, values


def fetch_hr_stream(activity_id: int) -> tuple[list[float], list[float]]:
    detail = get_activity_details_cached(activity_id, use_cache=True)
    times, values = stream_from_activity_details(
        detail,
        ("directHeartRate", "heartRate", "directHR"),
    )
    if times and values:
        return times, values
    return stream_from_fit(activity_id, "heart_rate", use_cache=True)


def fetch_power_stream(activity_id: int) -> tuple[list[float], list[float]]:
    detail = get_activity_details_cached(activity_id, use_cache=True)
    times, values = stream_from_activity_details(
        detail,
        ("directPower", "power", "directBikePower", "directRunPower"),
    )
    if times and values:
        return times, values
    return stream_from_fit(activity_id, "power", use_cache=True)

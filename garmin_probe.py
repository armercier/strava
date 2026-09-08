from __future__ import annotations

import argparse
import json
import os
import zipfile
from getpass import getpass
from pathlib import Path
from typing import Any


OUT_DIR = Path("garmin_probe_output")
DEFAULT_TOKENSTORE = "~/.garminconnect"


def _json_default(value: Any) -> str:
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=_json_default))


def safe_call(label: str, func, *args, **kwargs) -> tuple[bool, Any]:
    try:
        return True, func(*args, **kwargs)
    except Exception as exc:
        print(f"{label}: failed ({type(exc).__name__}: {exc})")
        return False, None


def safe_method_call(label: str, obj: Any, method_name: str, *args, **kwargs) -> tuple[bool, Any]:
    method = getattr(obj, method_name, None)
    if method is None:
        print(f"{label}: skipped ({method_name} is not available)")
        return False, None
    return safe_call(label, method, *args, **kwargs)


def import_garminconnect():
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: garminconnect. Install it with:\n"
            "  python -m pip install --upgrade garminconnect curl_cffi"
        ) from exc
    return Garmin


def login(tokenstore: str):
    Garmin = import_garminconnect()
    tokenstore_path = str(Path(tokenstore).expanduser())

    # Prefer saved tokens so normal probe runs do not require credentials/MFA.
    ok, api = safe_call("token login", lambda: Garmin())
    if ok:
        ok, _ = safe_call("saved-token login", api.login, tokenstore_path)
        if ok:
            print(f"Logged in using saved tokens from {tokenstore_path}")
            return api

    email = (
        os.getenv("GARMIN_EMAIL")
        or os.getenv("EMAIL")
        or input("Garmin email: ").strip()
    )
    password = (
        os.getenv("GARMIN_PASSWORD")
        or os.getenv("PASSWORD")
        or getpass("Garmin password: ")
    )

    api = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    api.login(tokenstore_path)
    print(f"Logged in with credentials. Tokens saved to {tokenstore_path}")
    return api


def get_activity_id(activity: dict[str, Any]) -> str | None:
    for key in ("activityId", "id"):
        value = activity.get(key)
        if value is not None:
            return str(value)
    return None


def summarize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "activityId",
        "id",
        "activityName",
        "name",
        "startTimeLocal",
        "startTimeGMT",
        "activityType",
        "distance",
        "duration",
        "elapsedDuration",
        "elevationGain",
        "averageHR",
        "maxHR",
        "avgPower",
        "maxPower",
    ]
    return {key: activity.get(key) for key in keys if key in activity}


def compact_type(value: Any) -> str:
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    return type(value).__name__


def summarize_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: compact_type(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "first_item": summarize_shape(value[0]) if value else None,
        }
    return compact_type(value)


def walk_json(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk_json(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value[:5]):
            yield from walk_json(item, f"{path}[{index}]")


def descriptor_names(detail: dict[str, Any]) -> list[str]:
    descriptors = detail.get("metricDescriptors")
    if not isinstance(descriptors, list):
        return []

    names: list[str] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            names.append(str(descriptor))
            continue
        name = (
            descriptor.get("metricsKey")
            or descriptor.get("key")
            or descriptor.get("name")
            or descriptor.get("unitKey")
            or descriptor.get("displayName")
            or str(descriptor)
        )
        names.append(str(name))
    return names


def inspect_metric_descriptor_streams(detail: dict[str, Any]) -> dict[str, Any]:
    descriptors = descriptor_names(detail)
    rows = detail.get("activityDetailMetrics")
    if not descriptors or not isinstance(rows, list):
        return {
            "available": False,
            "reason": "No metricDescriptors/activityDetailMetrics pair found.",
        }

    lower = [name.lower() for name in descriptors]
    hr_indices = [
        i
        for i, name in enumerate(lower)
        if "heart" in name or name in {"hr", "heartrate", "heart_rate"}
    ]
    power_indices = [
        i
        for i, name in enumerate(lower)
        if "power" in name or "watt" in name
    ]
    time_indices = [
        i
        for i, name in enumerate(lower)
        if "time" in name or "timestamp" in name or "duration" in name
    ]

    def count_non_null(indices: list[int]) -> int:
        count = 0
        for row in rows:
            metrics = row.get("metrics") if isinstance(row, dict) else None
            if not isinstance(metrics, list):
                continue
            for idx in indices:
                if idx < len(metrics) and metrics[idx] is not None:
                    count += 1
                    break
        return count

    return {
        "available": True,
        "row_count": len(rows),
        "descriptor_count": len(descriptors),
        "time_descriptors": [descriptors[i] for i in time_indices],
        "hr_descriptors": [descriptors[i] for i in hr_indices],
        "power_descriptors": [descriptors[i] for i in power_indices],
        "hr_non_null_rows": count_non_null(hr_indices),
        "power_non_null_rows": count_non_null(power_indices),
    }


def inspect_named_series(detail: Any) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    interesting = (
        "heart",
        "heartrate",
        "heart_rate",
        "hr",
        "power",
        "watt",
        "watts",
    )

    for path, value in walk_json(detail):
        path_l = path.lower()
        if not any(token in path_l for token in interesting):
            continue
        if isinstance(value, list):
            non_null = sum(item is not None for item in value)
            hits.append(
                {
                    "path": path,
                    "kind": "list",
                    "length": len(value),
                    "non_null": non_null,
                    "sample": value[:5],
                }
            )
        elif isinstance(value, (int, float, str)):
            hits.append({"path": path, "kind": type(value).__name__, "value": value})
    return hits


def write_download_probe(
    api,
    activity_id: str,
    fmt_name: str,
    fmt_value: Any,
    filename: str,
) -> dict[str, Any]:
    ok, payload = safe_call(f"download {fmt_name}", api.download_activity, activity_id, fmt_value)
    if not ok or payload is None:
        return {"ok": False}

    path = OUT_DIR / filename
    path.write_bytes(payload)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "bytes": len(payload),
    }

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            result["zip_entries"] = archive.namelist()
    else:
        result["first_bytes"] = payload[:24].hex()
    return result


def inspect_downloads(api, activity_id: str) -> dict[str, Any]:
    Garmin = import_garminconnect()
    if getattr(api, "download_activity", None) is None:
        return {"error": "download_activity is not available on this garminconnect version"}
    fmt = Garmin.ActivityDownloadFormat
    return {
        "gpx": write_download_probe(
            api,
            activity_id,
            "GPX",
            fmt.GPX,
            f"activity_{activity_id}.gpx",
        ),
        "tcx": write_download_probe(
            api,
            activity_id,
            "TCX",
            fmt.TCX,
            f"activity_{activity_id}.tcx",
        ),
        "original": write_download_probe(
            api,
            activity_id,
            "ORIGINAL",
            fmt.ORIGINAL,
            f"activity_{activity_id}_original.zip",
        ),
    }


def extract_fit_payload(original_path: Path, activity_id: str) -> tuple[Path | None, str | None]:
    fit_dir = OUT_DIR / "fit"
    fit_dir.mkdir(exist_ok=True)

    if zipfile.is_zipfile(original_path):
        with zipfile.ZipFile(original_path) as archive:
            fit_names = [
                name for name in archive.namelist() if name.lower().endswith(".fit")
            ]
            if not fit_names:
                return None, "Original download is a zip, but no .fit file was found."
            fit_name = fit_names[0]
            fit_path = fit_dir / f"activity_{activity_id}.fit"
            fit_path.write_bytes(archive.read(fit_name))
            return fit_path, None

    data = original_path.read_bytes()
    if len(data) > 12 and (b".FIT" in data[:32] or b".fit" in data[:32]):
        fit_path = fit_dir / f"activity_{activity_id}.fit"
        fit_path.write_bytes(data)
        return fit_path, None

    return None, "Original download was not a zip and did not look like a FIT file."


def inspect_fit_streams(original_download: dict[str, Any], activity_id: str) -> dict[str, Any]:
    if not original_download.get("ok"):
        return {"available": False, "reason": "Original/FIT download did not succeed."}

    original_path = Path(str(original_download.get("path", "")))
    if not original_path.exists():
        return {"available": False, "reason": f"Original path not found: {original_path}"}

    fit_path, error = extract_fit_payload(original_path, activity_id)
    if error or fit_path is None:
        return {"available": False, "reason": error}

    try:
        from fitparse import FitFile
    except ImportError as exc:
        return {"available": False, "reason": f"fitparse is not installed: {exc}"}

    record_count = 0
    timestamp_count = 0
    hr_count = 0
    power_count = 0
    cadence_count = 0
    distance_count = 0
    speed_count = 0
    first_timestamp = None
    last_timestamp = None
    samples: list[dict[str, Any]] = []

    fit = FitFile(str(fit_path))
    for record in fit.get_messages("record"):
        record_count += 1
        values = {field.name: field.value for field in record}

        timestamp = values.get("timestamp")
        heart_rate = values.get("heart_rate")
        power = values.get("power")

        if timestamp is not None:
            timestamp_count += 1
            if first_timestamp is None or timestamp < first_timestamp:
                first_timestamp = timestamp
            if last_timestamp is None or timestamp > last_timestamp:
                last_timestamp = timestamp
        if heart_rate is not None:
            hr_count += 1
        if power is not None:
            power_count += 1
        if values.get("cadence") is not None:
            cadence_count += 1
        if values.get("distance") is not None:
            distance_count += 1
        if values.get("speed") is not None or values.get("enhanced_speed") is not None:
            speed_count += 1

        if len(samples) < 5:
            samples.append(
                {
                    "timestamp": str(timestamp) if timestamp is not None else None,
                    "heart_rate": heart_rate,
                    "power": power,
                    "cadence": values.get("cadence"),
                    "distance": values.get("distance"),
                    "speed": values.get("speed") or values.get("enhanced_speed"),
                }
            )

    return {
        "available": True,
        "fit_path": str(fit_path),
        "record_count": record_count,
        "timestamp_records": timestamp_count,
        "hr_records": hr_count,
        "power_records": power_count,
        "cadence_records": cadence_count,
        "distance_records": distance_count,
        "speed_records": speed_count,
        "first_timestamp": str(first_timestamp) if first_timestamp is not None else None,
        "last_timestamp": str(last_timestamp) if last_timestamp is not None else None,
        "sample_records": samples,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Garmin Connect activity payloads and decide whether HR/power "
            "streams are available in activity details or require FIT/TCX parsing."
        )
    )
    parser.add_argument("--activity-id", help="Specific Garmin activity id to inspect.")
    parser.add_argument("--limit", type=int, default=10, help="Recent activities to fetch.")
    parser.add_argument(
        "--tokenstore",
        default=os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE),
        help="Garmin token directory. Defaults to GARMINTOKENS or ~/.garminconnect.",
    )
    parser.add_argument(
        "--maxchart",
        type=int,
        default=2000,
        help="maxChartSize passed to get_activity_details.",
    )
    parser.add_argument(
        "--maxpoly",
        type=int,
        default=4000,
        help="maxPolylineSize passed to get_activity_details.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    OUT_DIR.mkdir(exist_ok=True)

    api = login(args.tokenstore)

    activities: list[dict[str, Any]] = []
    if args.activity_id:
        activity_id = str(args.activity_id)
    else:
        ok, activities_payload = safe_method_call(
            "get recent activities", api, "get_activities", 0, args.limit
        )
        if not ok or not activities_payload:
            print("No activities returned. Pass --activity-id if you know one.")
            return 2
        activities = list(activities_payload)
        write_json(OUT_DIR / "recent_activities.json", activities)
        print("Recent activities:")
        for activity in activities[: args.limit]:
            print("  ", summarize_activity(activity))

        activity_id = get_activity_id(activities[0])
        if activity_id is None:
            print("Could not infer activity id from the first activity.")
            return 2

    print(f"\nInspecting activity {activity_id}")

    ok, summary = safe_method_call("get activity summary", api, "get_activity", activity_id)
    if ok:
        write_json(OUT_DIR / f"activity_{activity_id}_summary.json", summary)

    ok, detail = safe_method_call(
        "get activity details",
        api,
        "get_activity_details",
        activity_id,
        args.maxchart,
        args.maxpoly,
    )
    if not ok and getattr(api, "connectapi", None) is not None:
        detail_path = f"/activity-service/activity/{activity_id}/details"
        ok, detail = safe_call(
            "get activity details via connectapi",
            api.connectapi,
            detail_path,
            params={
                "maxChartSize": str(args.maxchart),
                "maxPolylineSize": str(args.maxpoly),
            },
        )
    if not ok or not isinstance(detail, dict):
        detail = {}
    else:
        write_json(OUT_DIR / f"activity_{activity_id}_details.json", detail)
        write_json(
            OUT_DIR / f"activity_{activity_id}_details_shape.json",
            summarize_shape(detail),
        )

    metric_result = inspect_metric_descriptor_streams(detail)
    named_series = inspect_named_series(detail)
    download_result = inspect_downloads(api, activity_id)
    fit_result = inspect_fit_streams(
        download_result.get("original", {}),
        activity_id,
    )

    report = {
        "activity_id": activity_id,
        "activity_detail_metric_streams": metric_result,
        "named_hr_power_paths": named_series,
        "downloads": download_result,
        "fit_streams": fit_result,
    }
    write_json(OUT_DIR / f"activity_{activity_id}_stream_report.json", report)

    print("\nDetail stream inspection:")
    print(json.dumps(metric_result, indent=2))
    if named_series:
        print("\nOther HR/power-looking values found:")
        for hit in named_series[:20]:
            print("  ", hit)
    else:
        print("\nNo additional HR/power-looking values found in detail payload.")

    print("\nDownload inspection:")
    print(json.dumps(download_result, indent=2))

    print("\nFIT inspection:")
    print(json.dumps(fit_result, indent=2))

    hr_rows = int(metric_result.get("hr_non_null_rows") or 0)
    power_rows = int(metric_result.get("power_non_null_rows") or 0)
    original_ok = bool(download_result.get("original", {}).get("ok"))
    fit_hr_rows = int(fit_result.get("hr_records") or 0)
    fit_power_rows = int(fit_result.get("power_records") or 0)

    print("\nConclusion:")
    if hr_rows or power_rows:
        print(
            "Garmin activity details appear to contain sample-level streams "
            f"(HR rows: {hr_rows}, power rows: {power_rows})."
        )
        if original_ok:
            print("FIT/original download is also available as a fallback.")
    elif fit_hr_rows or fit_power_rows:
        print(
            "No obvious sample-level HR/power streams were found in activity details. "
            "The original/FIT file contains parseable samples "
            f"(HR records: {fit_hr_rows}, power records: {fit_power_rows})."
        )
    elif original_ok:
        print(
            "No obvious sample-level HR/power streams were found in activity details. "
            "Original/FIT download succeeded, but this activity did not show HR/power "
            "records in the parsed FIT file. Try an activity known to have HR/power."
        )
    else:
        print(
            "No obvious detail streams and no original/FIT download succeeded for this "
            "activity. Try another activity id, preferably one with HR/power data."
        )

    print(f"\nProbe artifacts written to {OUT_DIR.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

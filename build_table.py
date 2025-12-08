"""
build_table.py

Description:
    Load a list of Strava activity objects from a JSON file, normalize selected
    fields into a pandas DataFrame, compute a few derived columns (human-friendly
    units and a unified sport column), and write the cleaned table to a CSV file.

Behavior:
    - Reads RAW_PATH (defaults to "strava_activities.json") which is expected to
      contain a JSON array of activity objects as exported by Strava.
    - Constructs a pandas.DataFrame from the raw list and filters it to a
      predefined set of columns (ignoring any that are missing).
    - Adds derived columns:
        - date: start_date_local converted to a date (no time)
        - distance_km: distance (m) -> kilometers
        - moving_time_h: moving_time (s) -> hours
        - elev_km: total_elevation_gain (m) -> kilometers
        - sport: unified sport label taken from sport_type or type
    - Writes the resulting table to OUT_CSV (defaults to "activities_clean.csv")
      and prints a short summary to stdout.

Public API:
    main() -> None
        Runs the extraction-transform-load flow described above. Has the
        side-effect of writing OUT_CSV and printing status information.

Constants:
    RAW_PATH: Path
        Path to input JSON file containing an array of Strava activity objects.
    OUT_CSV: Path
        Path to output CSV file.

Notes & Assumptions:
    - The input JSON should be an array of dictionaries where keys are Strava
      activity attributes (e.g., 'id', 'start_date_local', 'distance', etc.).
    - The script is defensive about missing fields: only columns present in the
      raw JSON are kept; missing numeric fields become NaN in the DataFrame.
    - Timezone handling: start_date_local is parsed with pandas.to_datetime and
      only the date portion is kept. No timezone normalization is performed.
    - Units are assumed to be the Strava defaults (meters for distance and
      elevation, seconds for times, meters/second for average_speed).
    - Requires pandas to be installed.

Example:
    Run the module as a script:
        python build_table.py

    This will read strava_activities.json from the current working directory,
    produce derived columns described above, save activities_clean.csv and
    print the output path and number of activities processed.
"""
from pathlib import Path
import json
import pandas as pd



RAW_PATH = Path("strava_activities.json")
OUT_CSV = Path("activities_clean.csv")


def main():
    # Load raw JSON list
    activities = json.loads(RAW_PATH.read_text())

    # Dump into a DataFrame
    df = pd.DataFrame(activities)

    # Pick the fields we care about (you can tweak this list)
    # Some fields might be missing for some sports → they'll just be NaN
    cols = [
        "id",
        "name",
        "sport_type",           # newer Strava field
        "type",                 # older field, still useful
        "start_date_local",
        "distance",             # meters
        "moving_time",          # seconds
        "elapsed_time",         # seconds
        "total_elevation_gain", # meters
        "average_heartrate",
        "max_heartrate",
        "average_speed",        # m/s
        "average_cadence",
        "average_watts",        # for rides
        "kilojoules",
    ]

    # Keep only columns that actually exist in your JSON
    cols = [c for c in cols if c in df.columns]
    df = df[cols]

    # ---- Derived columns ----
    # Date column (no time)
    df["date"] = pd.to_datetime(df["start_date_local"]).dt.date

    # Distances in km
    if "distance" in df.columns:
        df["distance_km"] = df["distance"] / 1000.0

    # Moving time in hours
    if "moving_time" in df.columns:
        df["moving_time_h"] = df["moving_time"] / 3600.0

    # Elevation in km (why not)
    if "total_elevation_gain" in df.columns:
        df["elev_km"] = df["total_elevation_gain"] / 1000.0

    # A simple "sport" column combining sport_type/type
    if "sport_type" in df.columns:
        df["sport"] = df["sport_type"]
    elif "type" in df.columns:
        df["sport"] = df["type"]

    # Save as CSV
    df.to_csv(OUT_CSV, index=False)
    print(f"Saved cleaned table to {OUT_CSV.resolve()}")
    print(f"{len(df)} activities")


if __name__ == "__main__":
    main()
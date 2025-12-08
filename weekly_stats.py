from pathlib import Path

import pandas as pd

"""
weekly_stats.py

Small utility to compute weekly totals from a cleaned Strava activities CSV.

This module reads a CSV (default: "activities_clean.csv") into a pandas DataFrame and
computes weekly aggregates (week starting on Monday) of activity metrics. Expected CSV
columns:
- date (required): activity date; parsed as a date
- start_date_local (optional): local start timestamp; parsed if present
- distance_km (required): activity distance in kilometers (numeric)
- moving_time_h (required): moving time in hours (numeric)
- sport (optional): activity type; when missing a default of "unknown" is used
- total_elevation_gain (optional): elevation gain (numeric)

Behavior:
- Adds a "week_start" column computed as the Monday of the week containing "date".
- Groups by "week_start" and sums distance_km, moving_time_h, and total_elevation_gain
    if present.
- Rounds numeric columns for nicer printing and prints the last 10 weekly rows.

Usage:
- Run as a script: python weekly_stats.py
- Adjust CSV_PATH at the top of the file to point to a different CSV.

Dependencies:
- pandas

Notes:
- The function main() performs the read/aggregate/print pipeline.
- The script assumes the CSV has been previously cleaned and contains the numeric
    columns used for aggregation.
"""

CSV_PATH = Path("activities_clean.csv")


def main():
    df = pd.read_csv(CSV_PATH, parse_dates=["start_date_local", "date"])

    # Make sure we have sport + distance/moving_time
    if "sport" not in df.columns:
        df["sport"] = "unknown"

    # Week start (Monday)
    df["week_start"] = df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")

    # Aggregate by week
    agg = {
        "distance_km": "sum",
        "moving_time_h": "sum",
    }
    if "total_elevation_gain" in df.columns:
        agg["total_elevation_gain"] = "sum"

    weekly = df.groupby("week_start").agg(agg).sort_index()

    # Optional: round for pretty printing
    weekly["distance_km"] = weekly["distance_km"].round(1)
    weekly["moving_time_h"] = weekly["moving_time_h"].round(2)
    if "total_elevation_gain" in weekly.columns:
        weekly["total_elevation_gain"] = weekly["total_elevation_gain"].round(0)

    print("Weekly totals (last 10 weeks):")
    print(weekly.tail(10))


if __name__ == "__main__":
    main()
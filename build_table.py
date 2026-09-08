from __future__ import annotations

from garmin_normalize import (
    GARMIN_CUTOVER_DATE,
    GARMIN_RAW_PATH,
    OUT_CSV,
    STRAVA_RAW_PATH,
    build_combined_activities_table,
)


RAW_PATH = STRAVA_RAW_PATH


def main() -> None:
    df = build_combined_activities_table(
        strava_raw_path=STRAVA_RAW_PATH,
        garmin_raw_path=GARMIN_RAW_PATH,
        out_csv=OUT_CSV,
        cutover_date=GARMIN_CUTOVER_DATE,
    )
    print(f"Saved cleaned table to {OUT_CSV.resolve()}")
    print(
        f"{len(df)} activities "
        f"(Strava before {GARMIN_CUTOVER_DATE.isoformat()}, "
        f"Garmin from {GARMIN_CUTOVER_DATE.isoformat()})"
    )


if __name__ == "__main__":
    main()

"""
main.py

PURPOSE
-------
CLI entry point. Thin by design -- argument parsing plus a call into
pipeline.run(). The --station flag is the whole multi-station UX:
pick any ICAO code registered in config.STATIONS.

USAGE
-----
    python main.py                        # WSSS, today
    python main.py --station WMKK          # WMKK, today
    python main.py --station WMKK --date 2026-07-21
    python main.py --list-stations         # show what's registered

DEPENDENCIES
------------
argparse (standard library)
config.py, pipeline.py (local)
"""

import argparse
from datetime import date

import config
from pipeline import run, print_summary


def main():
    parser = argparse.ArgumentParser(
        description="Multi-station Polymarket weather forecast MVP"
    )
    parser.add_argument(
        "--station",
        type=str,
        default="WSSS",
        help="Station ICAO code to run (default: WSSS). See --list-stations.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (defaults to today).",
    )
    parser.add_argument(
        "--list-stations",
        action="store_true",
        help="List all registered stations and exit.",
    )
    args = parser.parse_args()

    if args.list_stations:
        for icao, st in config.STATIONS.items():
            print(f"{icao}: {st.display_name} ({st.country}) -- source: {st.official_client_key}")
        return

    target_date = date.fromisoformat(args.date) if args.date else None
    result = run(station_icao=args.station, target_date=target_date)
    print_summary(result)


if __name__ == "__main__":
    main()

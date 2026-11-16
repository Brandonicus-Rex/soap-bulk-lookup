"""Command-line interface for bulk SOAP lookups.

Examples::

    # A range of 10 sequential IDs
    python -m soap_lookup.cli --start 5550100000 --count 10 --endpoint http://127.0.0.1:8000/soap

    # IDs from a CSV file with an "id" column
    python -m soap_lookup.cli --input sample_data/ids.csv --endpoint http://127.0.0.1:8000/soap

Exit codes: 0 = all lookups completed, 1 = at least one lookup errored,
2 = bad arguments or configuration.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from soap_lookup.lookup import LookupConfig, id_range, load_template, lookup_many, read_ids, write_results

# Default template lives in the project's templates/ folder.
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "search_request.xml"


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line options."""
    p = argparse.ArgumentParser(
        prog="soap-lookup",
        description="Look up a range or list of IDs against a SOAP service and save results to CSV.",
    )

    # Exactly one input source is required: a range or a file.
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--start", help="First ID of a sequential range (digits, leading zeros kept)")
    source.add_argument("--input", type=Path, help="CSV file with an 'id' column")

    p.add_argument("--count", type=int, default=1, help="How many sequential IDs to look up (with --start)")
    # The endpoint can come from an environment variable so it doesn't have
    # to be typed (or committed in scripts) every time.
    p.add_argument("--endpoint", default=os.getenv("SOAP_ENDPOINT"),
                   help="SOAP endpoint URL (default: $SOAP_ENDPOINT)")
    p.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                   help="Request template containing {lookup_id}")
    p.add_argument("--record-tag", default="Record", help="XML element that holds one result record")
    p.add_argument("--fields", default="status,plan,device_id",
                   help="Comma-separated fields to extract from each record")
    p.add_argument("--output", type=Path, default=Path("results.csv"), help="Output CSV path")
    p.add_argument("--workers", type=int, default=8, help="Concurrent requests")
    p.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the lookups, write the CSV and print a summary.

    Args:
        argv: Arguments to parse. None means use ``sys.argv`` (normal CLI use);
            tests pass a list instead.

    Returns:
        Process exit code (see module docstring).
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if not args.endpoint:
        print("error: no endpoint. Use --endpoint or set SOAP_ENDPOINT.", file=sys.stderr)
        return 2

    # Validate inputs up front so mistakes are reported before any requests go out.
    try:
        ids = id_range(args.start, args.count) if args.start else read_ids(args.input)
        template = load_template(args.template)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    fields = tuple(f.strip() for f in args.fields.split(",") if f.strip())
    config = LookupConfig(
        endpoint=args.endpoint,
        template=template,
        record_tag=args.record_tag,
        fields=fields,
        timeout=args.timeout,
        # Credentials only ever come from the environment, never from
        # command-line flags, which can end up in shell history.
        username=os.getenv("SOAP_USERNAME"),
        password=os.getenv("SOAP_PASSWORD"),
    )

    started = time.perf_counter()
    counts = write_results(lookup_many(config, ids, workers=args.workers), args.output, fields)
    elapsed = time.perf_counter() - started

    print(f"Looked up {len(ids)} IDs in {elapsed:.2f}s -> {args.output}")
    print(f"  found: {counts['found']}  not found: {counts['not_found']}  errors: {counts['error']}")

    # "Not found" is a valid answer, not a failure; only errors fail the run.
    # The last line states the verdict outright, so nobody has to infer it
    # from the counts or the exit code.
    if counts["error"]:
        print(f"Result: FAIL - {counts['error']} of {len(ids)} lookups errored (see the error column)")
        return 1
    print(f"Result: PASS - all {len(ids)} lookups completed with 0 errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())

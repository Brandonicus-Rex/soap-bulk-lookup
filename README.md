# SOAP Bulk Lookup

A command-line tool for looking up many records against a SOAP/XML web service at once. Give it a sequential ID range or a CSV of IDs, and it sends the requests concurrently, parses the XML responses, and writes a CSV report showing what was found, what wasn't, and what failed.

It includes a mock SOAP service with fictional data, so you can run it locally with no external systems.

![CI](https://github.com/Brandonicus-Rex/soap-bulk-lookup/actions/workflows/ci.yml/badge.svg)

**Requires Python 3.10–3.13.** Some dependencies don't yet publish prebuilt packages for newer versions.

## Why this exists

Many back-office and telecom systems still expose SOAP APIs. Checking hundreds of test records one by one, for example to confirm which test accounts exist before a regression run, is slow and error-prone. This tool turns that into a single command with a clean, auditable CSV output.

## Features

- **Range or list input:** a sequential range (leading zeros kept) or a CSV file of IDs
- **Template-driven requests:** any SOAP request works, as long as it contains a `{lookup_id}` placeholder (values are XML-escaped)
- **Namespace-agnostic parsing:** extracts the fields you name, regardless of namespace prefixes or letter case
- **Concurrent requests** with a configurable worker count, timeouts and retries
- **Clear outcomes:** every ID is reported as `found`, `not_found` or `error`, and SOAP faults are captured as errors
- **Config by flags or environment variables**, with optional basic auth from `SOAP_USERNAME` / `SOAP_PASSWORD`
- **Tested:** unit tests plus integration tests against the bundled mock service, run in GitHub Actions

## Project layout

```
soap-bulk-lookup/
├── soap_lookup/
│   ├── lookup.py            # Request building, parsing, concurrency, CSV output
│   └── cli.py               # Command-line interface
├── templates/
│   └── search_request.xml   # Example SOAP request with {lookup_id}
├── mock_server/
│   └── soap_server.py       # Mock SOAP service (FastAPI)
├── sample_data/             # Fictional records and an example ID list
└── tests/
```

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt

python -m pytest                 # runs unit and integration tests
```

### Try it against the mock service

```bash
# Terminal 1
python -m uvicorn mock_server.soap_server:app --port 8000

# Terminal 2: look up a range of 10 IDs
python -m soap_lookup.cli --start 5550100000 --count 10 \
    --endpoint http://127.0.0.1:8000/soap --output results.csv

# ...or a list of IDs from a CSV file
python -m soap_lookup.cli --input sample_data/ids.csv \
    --endpoint http://127.0.0.1:8000/soap
```

> **Windows tip:** commands are run as `python -m ...` so they work even when Windows Application Control blocks the `.exe` launchers that pip creates. Use `^` instead of `\` for line continuation in cmd, or a backtick in PowerShell.

Example output:

```
Looked up 4 IDs in 0.0s -> results.csv
  found: 2  not found: 2  errors: 0
```

```csv
lookup_id,outcome,status,plan,device_id,error
5550100001,found,active,prepaid,001010000000001,
5550100007,not_found,,,,
5550100012,found,suspended,unlimited-basic,001010000000012,
5550100099,not_found,,,,
```

## Options

| Option | Description |
|--------|-------------|
| `--start` / `--count` | Sequential range of IDs |
| `--input` | CSV file with an `id` column |
| `--endpoint` | SOAP endpoint URL (or set `SOAP_ENDPOINT`) |
| `--template` | Request template file (default: `templates/search_request.xml`) |
| `--record-tag` | XML element that wraps one result (default: `Record`) |
| `--fields` | Comma-separated fields to extract (default: `status,plan,device_id`) |
| `--workers` | Concurrent requests (default: 8) |
| `--timeout` | Per-request timeout in seconds (default: 10) |
| `--output` | Output CSV path (default: `results.csv`) |

The tool exits with code `1` if any lookup errored, so it can be used in scripts and CI.

## Using it with a real service

Write a request template that matches the service's WSDL, put `{lookup_id}` where the ID belongs, then set `--record-tag` and `--fields` to match the response. Only query systems you're authorized to use.

## Test data

All sample data is fictional. Phone-style IDs use the 555-01xx range reserved for fictional use, and device IDs use the 001-01 test network code.

## License

[MIT](LICENSE)

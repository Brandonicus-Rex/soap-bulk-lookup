"""Minimal mock SOAP service for local runs and tests.

Answers SearchRequest messages using fictional records from
sample_data/records.csv:
- known ID      -> a SearchResponse containing one Record
- unknown ID    -> an empty SearchResponse (the client reports "not_found")
- bad request   -> a SOAP Fault with HTTP 500 (the client reports "error")

Run it with::

    python -m uvicorn mock_server.soap_server:app --port 8000
"""

from __future__ import annotations

import csv
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

DATA_FILE = Path(__file__).resolve().parent.parent / "sample_data" / "records.csv"

# Namespace used by the example request template and responses.
NS = "urn:example:subscriber:v1"

app = FastAPI(title="Mock SOAP Subscriber Service")


def _load_records() -> dict[str, dict[str, str]]:
    """Load the sample CSV into a dict keyed by ID."""
    with DATA_FILE.open(newline="", encoding="utf-8") as fh:
        return {row["id"]: row for row in csv.DictReader(fh)}


# Loaded once at startup; the data never changes while the server runs.
RECORDS = _load_records()


def _envelope(body: str) -> str:
    """Wrap XML content in a SOAP 1.1 envelope."""
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:sub="{NS}"><soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>'
    )


def _fault(message: str) -> Response:
    """Build a SOAP Fault response. SOAP 1.1 returns faults with HTTP status 500."""
    body = f"<soapenv:Fault><faultcode>soapenv:Client</faultcode><faultstring>{escape(message)}</faultstring></soapenv:Fault>"
    return Response(_envelope(body), status_code=500, media_type="text/xml")


@app.get("/health")
def health() -> dict:
    """Liveness check, used by tests to know the server is up."""
    return {"status": "ok"}


@app.post("/soap")
async def soap(request: Request) -> Response:
    """Handle a SearchRequest and return the matching record, if any."""
    try:
        root = ET.fromstring(await request.body())
    except ET.ParseError:
        return _fault("Malformed XML")

    # Find <sub:LookupId> anywhere in the request ({NS} is ElementTree's
    # namespace syntax; the doubled braces escape them inside the f-string).
    id_element = root.find(f".//{{{NS}}}LookupId")
    if id_element is None or not (id_element.text or "").strip():
        return _fault("LookupId is required")

    record = RECORDS.get(id_element.text.strip())
    if record is None:
        return Response(_envelope("<sub:SearchResponse/>"), media_type="text/xml")

    # Turn each CSV column into an XML element, escaping values for safety.
    fields = "".join(f"<sub:{k}>{escape(v)}</sub:{k}>" for k, v in record.items())
    return Response(_envelope(f"<sub:SearchResponse><sub:Record>{fields}</sub:Record></sub:SearchResponse>"),
                    media_type="text/xml")

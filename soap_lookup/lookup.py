"""Core logic for bulk SOAP lookups.

The flow for each ID is:
    1. build_request()   insert the ID into an XML template
    2. lookup_one()      POST it to the SOAP endpoint
    3. parse_response()  pull the requested fields out of the XML reply
    4. LookupResult      record the outcome as found / not_found / error

lookup_many() runs steps 2-4 for many IDs in parallel, and write_results()
saves everything to CSV.
"""

from __future__ import annotations

import csv
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# SOAP 1.1 faults always use this namespace, regardless of the service.
SOAP_FAULT_TAG = "{http://schemas.xmlsoap.org/soap/envelope/}Fault"


# ---- Configuration and results -------------------------------------------

@dataclass(frozen=True)
class LookupConfig:
    """Settings shared by every lookup in a run.

    Attributes:
        endpoint: Full URL of the SOAP service.
        template: Request XML containing a ``{lookup_id}`` placeholder.
        record_tag: Name of the XML element that wraps one result record.
        fields: Child elements to extract from each record.
        timeout: Seconds to wait for each response.
        retries: How many times to retry transient server errors.
        username: Optional basic-auth username.
        password: Optional basic-auth password.
    """

    endpoint: str
    template: str
    record_tag: str = "Record"
    fields: tuple[str, ...] = ("status",)
    timeout: float = 10.0
    retries: int = 2
    username: str | None = None
    password: str | None = None


@dataclass
class LookupResult:
    """Outcome of looking up one ID.

    Attributes:
        lookup_id: The ID that was looked up.
        outcome: ``"found"``, ``"not_found"`` or ``"error"``.
        data: Extracted fields (only populated when found).
        error: Error message (only populated on error).
    """

    lookup_id: str
    outcome: str
    data: dict[str, str] = field(default_factory=dict)
    error: str = ""


# ---- Request building ----------------------------------------------------

def load_template(path: str | Path) -> str:
    """Read a request template and check it has a ``{lookup_id}`` placeholder.

    Raises:
        ValueError: If the placeholder is missing.
        OSError: If the file can't be read.
    """
    text = Path(path).read_text(encoding="utf-8")
    if "{lookup_id}" not in text:
        raise ValueError(f"Template {path} must contain a {{lookup_id}} placeholder")
    return text


def build_request(template: str, lookup_id: str) -> str:
    """Insert an ID into the template.

    The ID is XML-escaped first, so characters like ``<`` or ``&`` can't break
    the XML or inject extra elements into the request.
    """
    # str.replace (not str.format) because the template contains other braces
    # in XML namespaces that format() would misread.
    return template.replace("{lookup_id}", escape(lookup_id))


# ---- Response parsing ----------------------------------------------------

def _local(tag: str) -> str:
    """Strip an XML namespace from a tag name: ``'{urn:x}Status'`` -> ``'Status'``."""
    return tag.rsplit("}", 1)[-1]


def parse_response(xml_text: str | bytes, record_tag: str, fields: Iterable[str]) -> dict[str, str] | None:
    """Extract fields from the first matching record in a SOAP response.

    Matching ignores XML namespaces and letter case, so the same code works
    across services that use different namespace prefixes or naming styles.

    Args:
        xml_text: Raw response body.
        record_tag: Element name that wraps one record (e.g. ``"Record"``).
        fields: Child element names to extract.

    Returns:
        A dict of the fields that were present (keys use the caller's spelling),
        or None if no record element exists, meaning "not found".

    Raises:
        RuntimeError: If the response is a SOAP fault.
        ValueError: If the response isn't valid XML.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML response: {exc}") from exc

    # A SOAP fault means the service rejected the request; report it as an error.
    fault = root.find(f".//{SOAP_FAULT_TAG}")
    if fault is not None:
        message = " ".join(t.strip() for t in fault.itertext() if t.strip())
        raise RuntimeError(f"SOAP fault: {message}")

    # Map lowercase names back to the caller's spelling for the output keys.
    wanted = {f.lower(): f for f in fields}

    for element in root.iter():
        if _local(element.tag).lower() != record_tag.lower():
            continue
        found: dict[str, str] = {}
        for child in element.iter():
            name = _local(child.tag).lower()
            if name in wanted and child.text is not None:
                found[wanted[name]] = child.text.strip()
        # Only the first matching record is used.
        return found

    return None


# ---- Input helpers -------------------------------------------------------

def id_range(start: str, count: int) -> list[str]:
    """Generate sequential IDs, keeping leading zeros.

    Example: ``id_range("0099", 3)`` returns ``["0099", "0100", "0101"]``.

    Raises:
        ValueError: If ``start`` isn't all digits or ``count`` is less than 1.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if not start.isdigit():
        raise ValueError("start must contain digits only")
    # IDs are treated as strings so leading zeros survive the arithmetic.
    width = len(start)
    first = int(start)
    return [str(first + i).zfill(width) for i in range(count)]


def read_ids(path: str | Path, column: str = "id") -> list[str]:
    """Read IDs from a CSV file with a header row, skipping blank values.

    Raises:
        ValueError: If the CSV has no column with the given name.
    """
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or column not in reader.fieldnames:
            raise ValueError(f"{path} must have a '{column}' column")
        return [row[column].strip() for row in reader if row[column].strip()]


# ---- Sending requests ----------------------------------------------------

def make_session(config: LookupConfig) -> requests.Session:
    """Create an HTTP session with auth, retries and a connection pool.

    One session is shared by all worker threads so TCP connections are
    reused instead of opened for every lookup.
    """
    session = requests.Session()
    session.headers["Content-Type"] = "text/xml; charset=utf-8"
    if config.username and config.password:
        session.auth = (config.username, config.password)

    # SOAP sends everything as POST. Retrying POST is normally unsafe, but these
    # are read-only searches, so a retry can't create duplicate data.
    # allowed_methods=None means "retry any method".
    retry = Retry(total=config.retries, backoff_factor=0.5, status_forcelist=(502, 503, 504),
                  allowed_methods=None)

    # pool_maxsize must be at least the number of worker threads, otherwise
    # threads wait on each other for a connection.
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def lookup_one(session: requests.Session, config: LookupConfig, lookup_id: str) -> LookupResult:
    """Look up a single ID. Never raises; problems are returned as ``"error"`` results.

    This matters in a bulk run: one bad ID or a timeout shouldn't stop the
    other thousands of lookups.
    """
    try:
        response = session.post(config.endpoint, data=build_request(config.template, lookup_id),
                                timeout=config.timeout)
        # Parse even on HTTP 500, because SOAP services return faults with status 500.
        record = parse_response(response.content, config.record_tag, config.fields)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        log.warning("%s: %s", lookup_id, exc)
        return LookupResult(lookup_id, "error", error=str(exc))

    if record is None:
        return LookupResult(lookup_id, "not_found")
    return LookupResult(lookup_id, "found", data=record)


def lookup_many(config: LookupConfig, ids: Iterable[str], workers: int = 8) -> Iterator[LookupResult]:
    """Look up many IDs concurrently.

    Results are yielded in the same order as the input IDs (``pool.map``
    preserves order), so output files line up with input files.

    Args:
        config: Shared lookup settings.
        ids: IDs to look up.
        workers: Number of requests in flight at once.
    """
    with make_session(config) as session, ThreadPoolExecutor(max_workers=workers) as pool:
        yield from pool.map(lambda i: lookup_one(session, config, i), ids)


# ---- Output --------------------------------------------------------------

def write_results(results: Iterable[LookupResult], path: str | Path, fields: Iterable[str]) -> dict[str, int]:
    """Write results to CSV, one row per ID.

    Columns: lookup_id, outcome, one column per requested field, error.

    Returns:
        Counts per outcome, e.g. ``{"found": 9, "not_found": 1, "error": 0}``.
    """
    fields = list(fields)
    counts = {"found": 0, "not_found": 0, "error": 0}
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["lookup_id", "outcome", *fields, "error"])
        # `results` may be a generator, so rows are written as lookups finish
        # instead of holding everything in memory.
        for r in results:
            counts[r.outcome] += 1
            writer.writerow([r.lookup_id, r.outcome, *(r.data.get(f, "") for f in fields), r.error])
    return counts

"""Tests for soap_lookup.

Unit tests check the building blocks with hand-written XML; integration
tests run real lookups against the mock SOAP service from conftest.py.
"""

import csv
from pathlib import Path

import pytest

from soap_lookup import cli
from soap_lookup.lookup import (
    LookupConfig,
    build_request,
    id_range,
    load_template,
    lookup_many,
    parse_response,
    read_ids,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = load_template(ROOT / "templates" / "search_request.xml")
FIELDS = ("status", "plan", "device_id")

# Sample responses. FOUND_XML deliberately uses a different namespace prefix
# and capitalization than the code asks for, to prove matching ignores both.
FOUND_XML = """<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/" xmlns:x="urn:any">
<e:Body><x:SearchResponse><x:Record><x:Status>active</x:Status><x:Plan>prepaid</x:Plan></x:Record>
</x:SearchResponse></e:Body></e:Envelope>"""
EMPTY_XML = """<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/"><e:Body><SearchResponse/></e:Body></e:Envelope>"""
FAULT_XML = """<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/"><e:Body><e:Fault>
<faultcode>Client</faultcode><faultstring>Bad request</faultstring></e:Fault></e:Body></e:Envelope>"""


# ---- Unit tests ----------------------------------------------------------

def test_build_request_escapes_xml():
    # Special characters must be escaped so they can't inject XML.
    assert "&lt;script&gt;" in build_request(TEMPLATE, "<script>")


def test_parse_found_ignores_namespace_and_case():
    assert parse_response(FOUND_XML, "record", ["status", "plan"]) == {"status": "active", "plan": "prepaid"}


def test_parse_not_found():
    assert parse_response(EMPTY_XML, "Record", FIELDS) is None


def test_parse_fault_raises():
    with pytest.raises(RuntimeError, match="Bad request"):
        parse_response(FAULT_XML, "Record", FIELDS)


def test_parse_invalid_xml_raises():
    with pytest.raises(ValueError):
        parse_response("not xml", "Record", FIELDS)


def test_id_range_keeps_leading_zeros():
    # Crossing 0099 -> 0100 is the case most likely to drop a zero.
    assert id_range("0099", 3) == ["0099", "0100", "0101"]


@pytest.mark.parametrize("start,count", [("abc", 1), ("123", 0)])
def test_id_range_rejects_bad_input(start, count):
    with pytest.raises(ValueError):
        id_range(start, count)


def test_template_requires_placeholder(tmp_path):
    bad = tmp_path / "t.xml"
    bad.write_text("<x/>")
    with pytest.raises(ValueError):
        load_template(bad)


def test_read_ids(tmp_path):
    # Blank lines in the CSV should be skipped.
    f = tmp_path / "ids.csv"
    f.write_text("id\n111\n\n222\n")
    assert read_ids(f) == ["111", "222"]


# ---- Integration tests against the mock server ---------------------------

def test_lookup_many_mixed_results(endpoint):
    # 5550100001 exists, 5550100007 is a deliberate gap in the sample data,
    # and 5550100099 is outside the sample range.
    config = LookupConfig(endpoint=endpoint, template=TEMPLATE, fields=FIELDS, retries=0)
    results = list(lookup_many(config, ["5550100001", "5550100007", "5550100099"], workers=3))
    assert [r.outcome for r in results] == ["found", "not_found", "not_found"]
    assert results[0].data == {"status": "active", "plan": "prepaid", "device_id": "001010000000001"}


def test_lookup_reports_connection_error():
    # Port 9 (discard) has nothing listening, so the connection fails.
    # The lookup should report an error rather than crash.
    config = LookupConfig(endpoint="http://127.0.0.1:9/soap", template=TEMPLATE, retries=0, timeout=1)
    [result] = list(lookup_many(config, ["5550100001"]))
    assert result.outcome == "error"


def test_cli_end_to_end(endpoint, tmp_path, capsys):
    # Range 5550100005-08 includes the 07 gap, so expect one "not_found".
    out = tmp_path / "results.csv"
    code = cli.main(["--start", "5550100005", "--count", "4", "--endpoint", endpoint, "--output", str(out)])
    assert code == 0
    rows = list(csv.DictReader(out.open()))
    assert [r["outcome"] for r in rows] == ["found", "found", "not_found", "found"]
    assert "found: 3" in capsys.readouterr().out

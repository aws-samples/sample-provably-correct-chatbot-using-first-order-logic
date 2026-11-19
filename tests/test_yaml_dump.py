"""utils.yaml_dump / yaml_dump_all must write YAML that reads back exactly."""
import doctest
import io

import pytest
import yaml

import utils

THEORY = ("(declare-sort Event 0)\n"
          "; (days-after e1 e2 n): e1 happened n days after e2\n"
          "(assert true)")

TRICKY = [
    THEORY,
    "one line",
    "trailing newline\n",
    "  leading spaces\nsecond",
    "lone CR\r; (days-after e1 e2 n): after the CR\n(assert true)",
    "CRLF line\r\nnext line\r\n",
    "mac line\rendings\rhere",
    "NEL\x85and\u2028line\u2029separators\nx",
    "control \x07 bell\nand \x0b vt",
    "\ufeffBOM first\nline",
    "tab\tinside\nand trailing spaces   \nend",
    "",
]


@pytest.mark.parametrize("text", TRICKY)
def test_strings_round_trip(text):
    # Nested the way run files are: a list of result dicts in a mapping.
    doc = {"result": {"theory": text, "notes": [text, "x"]}, "n": 1}
    assert yaml.safe_load(utils.yaml_dump(doc)) == doc


def test_run_file_round_trips():
    docs = [{"p": {"stride": 2}}, {"theory": TRICKY[4], "sentences": ["a\rb", "c"]},
            {"theory": THEORY}]
    buf = io.StringIO()
    utils.yaml_dump_all(docs, buf)
    assert list(yaml.safe_load_all(buf.getvalue())) == docs


def test_plain_multiline_strings_stay_readable_literal_blocks():
    out = utils.yaml_dump({"theory": THEORY})
    assert out.startswith("theory: |")
    assert "; (days-after e1 e2 n): e1 happened" in out  # not escaped
    # Only the string a literal block cannot hold is escaped.
    out = utils.yaml_dump({"theory": "a\rb\nc"})
    assert "\\r" in out and "|" not in out


def test_literal_style_ok_doctests():
    assert doctest.run_docstring_examples(utils.literal_style_ok, {"literal_style_ok": utils.literal_style_ok}) is None
    result = doctest.DocTestRunner().run(
        doctest.DocTestFinder().find(utils.literal_style_ok)[0])
    assert result.failed == 0

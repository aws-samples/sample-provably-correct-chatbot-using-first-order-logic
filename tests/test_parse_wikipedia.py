# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for parse_wikipedia_page's marker stripping (no network needed)."""
import pytest

nltk = pytest.importorskip("nltk")
import parse_wikipedia_page as pwp  # noqa: E402

try:
    nltk.sent_tokenize("One. Two.")
except LookupError:  # tokenizer data not installed here
    pytestmark = pytest.mark.skip(reason="nltk punkt data not available")

# Shaped like Wikipedia's rendered article HTML: footnotes are
# <sup class="reference">, maintenance tags are <sup class="noprint
# Inline-Template ...">.
HTML = """
<html><head><title>New Zealand - Wikipedia</title></head><body>
<p>New Zealand is an island country.<sup id="cite_ref-1" class="reference"><a href="#cite_note-1">[1]</a></sup>
Its capital is Wellington.<sup class="reference"><a href="#cite_note-a">[note 1]</a></sup></p>
<p>It has about 5.3 million people<sup class="noprint Inline-Template Template-Fact"
 style="white-space:nowrap;">[<i><a href="/wiki/Wikipedia:Citation_needed"
 title="Wikipedia:Citation needed"><span title="This claim needs references">citation needed</span></a></i>]</sup>,
most of them in cities.<sup class="reference"><a href="#cite_note-b">[a]</a></sup></p>
<p>Auckland is the largest city<sup class="noprint Inline-Template"><i>[<a href="#">clarification needed</a>]</i></sup>.</p>
<p>The term [sic] is kept, as is [UK].</p>
</body></html>
"""


def test_sentences_from_html_drops_reference_and_maintenance_markers():
    assert pwp.sentences_from_html(HTML) == [
        "New Zealand is an island country.",
        "Its capital is Wellington.",
        "It has about 5.3 million people, most of them in cities.",
        "Auckland is the largest city.",
        "The term [sic] is kept, as is [UK].",
    ]


def test_text_markers_are_removed_even_without_markup():
    # e.g. a page whose markers are not in <sup> elements
    html = ("<p>Founded in 1840[when?] by settlers[by whom?].[2] "
            "It is the largest[dubious \u2013 discuss] city.[note 3]</p>")
    assert pwp.sentences_from_html(html) == [
        "Founded in 1840 by settlers.", "It is the largest city."]


def test_remove_citations_doctests():
    import doctest
    result = doctest.testmod(pwp)
    assert result.attempted >= 6 and result.failed == 0

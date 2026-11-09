# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Extract sentences from a Wikipedia page.
"""

import re
from typing import List
import argparse

import requests
from bs4 import BeautifulSoup
import nltk

from subjects import get_subject_by_id
from utils import flatten1
from globals import SENTENCES_DIR


# Footnote references: [1], [12], [a], [iv], [note 1], [Note 23], [nb 2],
# [n 3], [lower-alpha 1]. Letter footnotes are a single lowercase letter or a
# valid lowercase roman numeral, so bracketed words ("[UK]", "[civil]") are
# left alone; the HTML pass in sentences_from_html removes the rest.
_FOOTNOTE_RE = re.compile(
    r"\[\s*(?:\d+"
    r"|(?-i:[a-z])"
    r"|(?-i:x{0,3}(?:ix|iv|v?i{1,3}|v))"
    r"|(?:note|nb|n|fn|lower-alpha|upper-alpha|lower-roman|upper-roman)\s*\w{1,4}"
    r")\s*\]",
    re.IGNORECASE)

# Inline maintenance templates: [citation needed], [clarification needed],
# [dubious – discuss], [when?], [by whom?], [failed verification], [better
# source needed], [page needed], [original research?], [vague] ... Matched by
# their wording, so an ordinary bracketed aside (or "[sic]") is kept.
_MAINTENANCE_RE = re.compile(
    r"\[\s*[^\[\]\n]{0,60}?(?:needed|required|\?|dubious|discuss|verification|"
    r"verify|unreliable|disputed|original research|clarif\w*|vague|citation|"
    r"attribution|synthesis|neutrality|peacock|weasel|obsolete|outdated|"
    r"update|irrelevant|unbalanced|non sequitur|contradictory)[^\[\]\n]{0,30}\]",
    re.IGNORECASE)


def remove_citations(text: str) -> str:
    """
    Remove Wikipedia's inline reference and maintenance markers from text.

    Wikipedia renders footnote references ([1], [note 1], [a]) and inline
    cleanup templates ([citation needed], [clarification needed], [when?])
    inside the prose, so they end up in the extracted sentences; they are
    noise for formalization. The space a marker leaves before punctuation is
    closed up too.

    >>> remove_citations("Wellington is the capital.[1][note 2] It is windy.[a]")
    'Wellington is the capital. It is windy.'
    >>> remove_citations("It has 5.3 million people [citation needed], mostly urban.")
    'It has 5.3 million people, mostly urban.'
    >>> remove_citations("Founded in 1840[when?] by settlers[by whom?].")
    'Founded in 1840 by settlers.'
    >>> remove_citations("It is the largest[dubious \\u2013 discuss] city.[clarification needed]")
    'It is the largest city.'
    >>> remove_citations("Kept: [sic], [Wellington], [UK], [civil] and [is].")
    'Kept: [sic], [Wellington], [UK], [civil] and [is].'
    >>> remove_citations("A claim.[iv][xii][b][Note 3][lower-alpha 2]")
    'A claim.'
    """
    before = text
    text = _MAINTENANCE_RE.sub("", text)
    text = _FOOTNOTE_RE.sub("", text)
    if text != before:
        text = re.sub(r"[ \t]+([.,;:!?)])", r"\1", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text


# Elements Wikipedia uses for the markers above: footnote references
# (<sup class="reference">) and inline maintenance templates (<sup
# class="noprint Inline-Template ...">). Removed from the HTML before its text
# is taken, so they are dropped however they are worded.
_MARKER_SELECTORS = ("sup.reference", "sup.noprint", ".Inline-Template",
                     "span.mw-ref", ".mw-editsection")


def sentences_from_html(html) -> List[str]:
    """The prose sentences of a Wikipedia article's HTML, without markers."""
    soup = BeautifulSoup(html, 'html.parser')
    for selector in _MARKER_SELECTORS:
        for el in soup.select(selector):
            el.decompose()
    paragraphs = soup.find_all('p')
    print(f"paragraphs found: {len(paragraphs)}")
    # Strip text-level markers before splitting too, so a marker between two
    # sentences cannot change where the tokenizer splits them.
    sentences = flatten1([nltk.sent_tokenize(remove_citations(para.get_text()))
                          for para in paragraphs])
    print(f"sentences extracted: {len(sentences)}")
    # Collapse internal whitespace: line breaks in the HTML source would
    # otherwise split a sentence across lines of the one-per-line files.
    sentences = [" ".join(remove_citations(sent).split()) for sent in sentences]
    sentences = [sent for sent in sentences if sent]
    print(f"non-empty sentences: {len(sentences)}")
    return sentences


def parse_wikipedia_page(URL: str) -> List[str]:
    """
    Given a Wikipedia page URL, extract and return a list of sentences from the page.
    """

    # Download NLTK punkt tokenizer if not already downloaded
    nltk.download('punkt', quiet=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
    }
    response = requests.get(URL, headers=headers, timeout=30)
    print(f"response.status_code: {response.status_code}")
    return sentences_from_html(response.content)


def parse_and_persist(subject_id: str):
    """
    Parse the Wikipedia page for the given subject and persist the sentences.
    """
    subject = get_subject_by_id(subject_id)
    sentences = parse_wikipedia_page(subject.wikipedia_url)
    with open(SENTENCES_DIR / f"{subject.id}.txt", "w", 
              encoding="utf-8") as f:
        for sentence in sentences:
            f.write(sentence + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Parse a Wikipedia page and extract sentences."
    )
    parser.add_argument(
        "--subject-id", type=str, required=True,
        help="What subject to parse from Wikipedia."
    )
    
    args = parser.parse_args()
    subject_id: str = args.subject_id
    
    nltk.download('punkt_tab')
    parse_and_persist(subject_id)

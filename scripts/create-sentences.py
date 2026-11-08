# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Given a subject, download the corresponding article from Wikipedia, parse
it into sentences, and create standalone versions of each sentence.
"""
import argparse, os, sys
from typing import List

import nltk

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

import globals as g
from params import Params
from sentences import Document
from utils import register_signal_handlers, get_global_stats
from otel import otel_session_id, propagate_attributes
from parse_wikipedia_page import parse_and_persist


if __name__ == "__main__":
    nltk.download('punkt_tab')

    register_signal_handlers()

    parser = argparse.ArgumentParser(
        description="Create sentences for a subject."
    )
    parser.add_argument(
        "--subject-id", action="append", default=[],
        help="What subject to build theory for."
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Session ID (default: create a new one)"
    )
    defaults = Params()
    parser.add_argument(
        "--context-sentences", type=int,
        default=defaults.standalone_context_sentences,
        help="Previous raw sentences given as context to each rewrite "
             f"(default {defaults.standalone_context_sentences})"
    )
    parser.add_argument(
        "--pool-size", type=int, default=defaults.standalone_pool_size,
        help="Max concurrent LLM rewrites "
             f"(default {defaults.standalone_pool_size})"
    )

    args = parser.parse_args()
    subject_ids: List[str] = args.subject_id
    session_id: str = args.session_id
    g.p = Params(standalone_context_sentences=args.context_sentences,
                 standalone_pool_size=args.pool_size)

    session_id = session_id or otel_session_id()

    with propagate_attributes(session_id=session_id):
        for subject_id in subject_ids:
            raw_file = g.SENTENCES_DIR / f"{subject_id}.txt"
            if not raw_file.exists():
                parse_and_persist(subject_id)
            # A TextDocument computes sentences/{id}-standalone.txt if it is
            # missing, else loads it.
            document = Document.load_for_subject(subject_id, get_global_stats())
            sentences = document.sentences()
            print(f"Subject {subject_id}: {len(sentences):,} standalone sentences ready")

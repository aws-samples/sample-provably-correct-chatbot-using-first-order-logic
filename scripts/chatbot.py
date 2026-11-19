"""
Prompt the user for a question. Answer it. Repeat.
"""
import argparse, os, sys
from pathlib import Path
from typing import Optional

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

import globals as g
from utils import register_signal_handlers, get_global_stats
from auto_formalizer import answer_questions


if __name__ == "__main__":
    register_signal_handlers()
    parser = argparse.ArgumentParser(
        description="Given a set of questions, answer them."
    )
    parser.add_argument(
        "--run-file", "-r", type=str, default=None,
        help="Path to the run file of a build (bare names resolve under runs/)."
    )
    parser.add_argument(
        "--s3-uri", "-s", type=str, default=None,
        help="s3:// URI of a run file."
    )
    args = parser.parse_args()
    run_file: Optional[Path] = args.run_file
    s3_uri: Optional[str] = args.s3_uri

    assert (run_file is None) != (s3_uri is None), \
        "Either --run-file or --s3-uri (but not both) must be specified"

    if run_file is not None:
        if run_file.find("/") == -1:
            run_path = g.RUNS_DIR / run_file
        else:
            run_path = Path(run_file)
    else:
        run_path = None

    answer_questions(run_path or s3_uri,
                     stats=get_global_stats(),
                     temperature=None)

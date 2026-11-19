"""
Ask some questions and see how many times it says "I don't know".  The fewer
times the better.
"""

import argparse, re, time, sys, os
from typing import Generator
from pathlib import Path
import traceback

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

import globals as g
from auto_formalizer import compute_coverage, load_run_file
from utils import register_signal_handlers, get_global_stats
from otel import otel_session_id, propagate_attributes


if __name__ == "__main__":
    register_signal_handlers()
    parser = argparse.ArgumentParser(
        description="Given a run file, compute coverage (num qs answered)."
    )
    parser.add_argument("--num-questions", type=int, default=100,
        help="Number of questions to ask."
    )
    parser.add_argument("--run-file", type=str, required=True,
        help="Path to the run file of the build."
    )
    parser.add_argument("--pool-size", type=int, default=90,
        help="Number of cores to use."
    )
    parser.add_argument("--temperature", type=float, default=1.0,
        help="Temperature for the language model."
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Session ID (default: create a new one)"
    )

    args = parser.parse_args()
    num_questions: int = args.num_questions
    run_file = Path(args.run_file)
    pool_size: int = args.pool_size
    temperature: float = args.temperature
    session_id: str = args.session_id

    try:
        session_id = session_id or otel_session_id()
        with propagate_attributes(session_id=session_id):
            g.p, _result = load_run_file(run_file)  # the build's subjects
            compute_coverage(run_file, pool_size, temperature,
                             get_global_stats(), num_questions)
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
    except Exception as ex:
        print(f"Error: {ex}")
        traceback.print_exc()
        sys.exit(1)

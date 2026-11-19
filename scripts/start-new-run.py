"""
Build a theory from the command line: formalize every sentence of the given
subjects in one kiro-cli unit (auto_formalizer.formalize_document) and write
the result to a new run file under runs/.

The sentences must already exist under sentences/ (see
scripts/create-sentences.py). kiro-cli must be installed and logged in, or
KIRO_API_KEY set.
"""
import argparse, os, sys
from pathlib import Path
import pstats

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

from params import Params
import globals as g
from auto_formalizer import formalize_document
from utils import dump_dict, register_signal_handlers
from otel import otel_session_id, propagate_attributes


if __name__ == "__main__":
    register_signal_handlers()
    defaults = Params()

    parser = argparse.ArgumentParser(
        description="Build one theory of the given subjects."
    )
    parser.add_argument(
        "--subject-id", action="append", default=[], required=True,
        help="Subject to formalize (repeat for several)."
    )
    parser.add_argument(
        "--max-sentences", type=int, default=None,
        help="Formalize only the first N sentences (default: all)."
    )
    parser.add_argument(
        "--qas-per-sentence", type=float, default=defaults.qas_per_sentence,
        help="Test questions to generate per sentence."
    )
    parser.add_argument(
        "--ncar-ratio", type=float, default=defaults.ncar_ratio,
        help="Correct answers required, as a fraction of the sentence count."
    )
    parser.add_argument(
        "--kiro-model", type=str, default=defaults.kiro_model,
        help="kiro-cli --model (default: kiro-cli's own default)."
    )
    parser.add_argument(
        "--kiro-timeout-seconds", type=float, default=defaults.kiro_timeout_seconds,
        help="Wall-clock budget across all kiro-cli sessions."
    )
    parser.add_argument(
        "--kiro-max-attempts", type=int, default=defaults.kiro_max_attempts,
        help="kiro-cli sessions (the first build plus feedback resumes)."
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Tracing session ID (default: create a new one)"
    )
    parser.add_argument(
        "--do-profile", action=argparse.BooleanOptionalAction,
        default=False,
        help="Run the python profiler and dump .prof files to disk."
    )
    args = parser.parse_args()

    if args.do_profile:
        profs_dir = Path.cwd() / "profs"
        profs_dir.mkdir(parents=True, exist_ok=True)
        for file in profs_dir.iterdir():
            file.unlink()
        print(f"profs dir: {profs_dir}")

    g.p = Params(
        subject_ids=args.subject_id,
        max_sentences=args.max_sentences,
        qas_per_sentence=args.qas_per_sentence,
        ncar_ratio=args.ncar_ratio,
        kiro_model=args.kiro_model,
        kiro_timeout_seconds=args.kiro_timeout_seconds,
        kiro_max_attempts=args.kiro_max_attempts,
        do_profile=args.do_profile,
    )
    print(f"snr: g.p: {g.p}")

    with propagate_attributes(session_id=args.session_id or otel_session_id()):
        summary = formalize_document()
        print("Build summary:")
        dump_dict(summary)

    if args.do_profile:
        stats = None
        for file in profs_dir.iterdir():
            stats = pstats.Stats(file) if stats is None else stats.add(file)
        if stats:
            stats.dump_stats(profs_dir / "summary.prof")
    sys.exit(0 if summary["ok"] else 1)

import random, os
from typing import List, Optional
from operator import itemgetter
from pathlib import Path

from dotenv import load_dotenv

from auto_formalizer import (generate_q_and_a, convert_question_to_logical_form,
                          load_run_result, answer_q,
                          get_global_stats)
from utils import starmap_with_dupes, Statistics, join_with_and

load_dotenv()

PREV_QUESTIONS: List[str] = []
THEORY: Optional[str] = None
SENTENCES: List[str] = []
SUBJECT_IDS: Optional[List[str]] = None


def answer_q_(theory: str,
              question: str,
              job_num: int,
              retries: int):
    """
    A wrapper around WikiLogifier:answer_q() that makes it work
    with starmap_with_dupes.

    starmap_with_dupes (on the quit_when_get_first_result path) unpacks each
    worker result as (result, stats, duration, errors), so return that shape
    rather than a bare answer.
    """
    import time
    import traceback
    stats = get_global_stats()
    start_time = time.time()
    try:
        answer = answer_q(theory=theory,
                          question=question,
                          generate_actual_answer=True,
                          stats=stats)
        return answer, stats, time.time() - start_time, None
    except Exception as ex:  # pylint: disable=broad-except
        return (None, stats, time.time() - start_time,
                f"{ex}\n{traceback.format_exc()}")


def answer_q_from_chatbot(question: str, num_jobs: int = 10) -> str:
    """
    Given a question in natural language, return an answer using the chatbot.
    """
    global THEORY, PREV_QUESTIONS
    PREV_QUESTIONS.append(question)
    results = starmap_with_dupes(
        worker=answer_q_,
        arguments=[(THEORY, question,) for _ in range(num_jobs)],
        job_prefix="job",
        pool_size=num_jobs,
        do_spawn_dupes=False,
        quit_when_get_first_result=True,
        # Each worker result is a (answer, stats, duration, errors) tuple; a
        # provable answer has a non-None first element. starmap_with_dupes
        # requires this predicate whenever quit_when_get_first_result is True.
        job_successful_p=lambda result: result is not None and result[0] is not None)
    print(f"aqfc: chatbot answers: {results}")
    # results is a list of (answer, stats, duration, errors) tuples; keep the
    # answers that are non-None (i.e. the prover found a provable answer).
    actual_results = [
        res[0] for res in results
        if res is not None and res[0] is not None
    ]
    print(f"aqfc: actual results: {actual_results}")
    if len(actual_results) > 0:
        return random.choice(actual_results)
    else:
        return "I don't know"


def im_feeling_lucky():
    """
    Generate and return a random question.
    """
    global SENTENCES, PREV_QUESTIONS
    q_result = generate_q_and_a(SENTENCES, PREV_QUESTIONS,
                                temperature=None,
                                stats=get_global_stats())
    rv = q_result["question"]
    print(f"ifl: -> {rv}")
    return rv


def current_subject_id() -> str:
    global SUBJECT_IDS
    if SUBJECT_IDS is None:
        raise ValueError("Chatbot backend not set up yet.")
    return join_with_and(SUBJECT_IDS)


def setup():
    """
    Serve the theory in a local run file: CHATBOT_RUN_FILE (a run file written
    by formalize_document) for the subjects in CHATBOT_SUBJECT_IDS (comma
    separated). Used by the command-line chatbot; the web service loads
    theories from its store instead.
    """
    global THEORY, SENTENCES, SUBJECT_IDS
    print("cb:setup()")
    SUBJECT_IDS = os.environ["CHATBOT_SUBJECT_IDS"].split(",")
    result = load_run_result(Path(os.environ["CHATBOT_RUN_FILE"]))
    THEORY, _suggested_qs, SENTENCES = itemgetter("theory", "suggested_questions",
                                                  "sentences")(result)
    print(f"cb:setup(): loaded subjects {join_with_and(*SUBJECT_IDS)} with {len(SENTENCES)} sentences")
    print(f"cb:setup(): theory starts with: {THEORY[:100]}...")

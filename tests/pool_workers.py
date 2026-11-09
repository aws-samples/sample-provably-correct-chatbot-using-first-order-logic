# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Worker functions for test_pool_retries (module-level so spawned children can import them)."""
import time


def has_theory(job_result) -> bool:
    """starmap_with_dupes success predicate: the run produced a theory."""
    return job_result is not None and job_result["theory"] is not None


def flaky_worker(x, job_num, retries):
    """Job 1's first run fails; every other run succeeds after a short pause."""
    time.sleep(0.3 + 0.1 * job_num)
    if job_num == 1 and retries == 0:
        return dict(theory=None), None, 0.1, None
    return (dict(theory=f"(t{x})", n_correct_answers_reqd=1,
                 correctly_answered_questions=["q"]), None, 0.3, None)


def straggler_worker(pid_file, job_num, retries):
    """
    The first run returns after a moment; any duplicate starts a long
    session-leading child (like kiro-cli), records its own pid and the
    child's, and then never finishes on its own.
    """
    import os
    import subprocess
    if retries == 0:
        time.sleep(2.0)
        return (dict(theory="(t)", n_correct_answers_reqd=1,
                     correctly_answered_questions=["q"]), None, 2.0, None)
    child = subprocess.Popen(["sleep", "300"], start_new_session=True)
    with open(pid_file, "a", encoding="utf8") as f:
        f.write(f"{os.getpid()}\n{child.pid}\n")
    time.sleep(300)
    return None


def answer_worker(theory, question, job_num, retries):
    """
    A chatbot attempt (chatbot_backend.answer_q_): job 0 gives up quickly
    with "I don't know", job 2 proves an answer a little later, the rest find
    nothing. With question "none", every attempt gives up.
    """
    from utils import Statistics
    if question == "none" or job_num != 2:
        time.sleep(0.1 if job_num == 0 else 0.3)
        result = (dict(answer="I don't know", question_logic="(assert false)",
                       answer_logic=None) if job_num == 0 else None)
        return result, Statistics(), 0.1, None
    time.sleep(0.8)
    return (dict(answer="The Democratic Party.",
                 question_logic="(declare-const answer E)",
                 answer_logic="(define-fun answer () E dem-party)"),
            Statistics(), 0.8, None)

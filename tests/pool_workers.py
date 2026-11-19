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

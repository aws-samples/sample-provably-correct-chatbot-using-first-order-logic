# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# pylint: disable=use-dict-literal, too-many-locals, no-else-return, too-many-instance-attributes, too-many-lines, multiple-imports, broad-exception-raised, too-many-arguments, too-many-statements
# pylint: disable=wrong-import-position,import-error,multiple-imports,use-dict-literal,wrong-import-order
"""
Common utilites that aren't domain specific
"""
from __future__ import annotations
import io, re, os, glob, sys, itertools, time, random, signal, uuid, json, difflib, queue
from typing import (List, Any, Callable, Dict, Set, Optional, Union,
                    Tuple, TypeAlias)
from textwrap import dedent
from pathlib import Path
from enum import Enum
from multiprocessing import Process, Queue
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait as concurrent_wait
from time import sleep
from random import choice
from collections import defaultdict
from dataclasses import dataclass, field
import ctypes
import resource
import threading
import traceback
import statistics
from operator import itemgetter
import multiprocessing as mp
from urllib.parse import urlparse
import cProfile
import pstats
from contextlib import contextmanager

import globals as g
from otel import start_span

# --- Cooperative cancellation hook ----------------------------------------
# The inline job runner (cdk/docker/store.py) sets this to a zero-arg callable
# that returns True when the CURRENT job has been killed by the user.
#
# It is stored THREAD-LOCAL: each build runs in its own daemon thread, and
# starmap_with_dupes runs synchronously inside that same thread, so a
# thread-local predicate lets two concurrent builds each cancel only their own
# worker pool (a single process global would let one build's kill tear down the
# other's pool). It runs in the PARENT process and is never serialized to spawn
# children (only globals.py is). Default (unset) is a no-op, so every other
# caller/entry point is unaffected.
_cancel_tls = threading.local()


def set_cancel_check(fn: Optional[Callable[[], bool]]) -> None:
    """Install (or clear, with None) the cooperative-cancel predicate for THIS thread."""
    _cancel_tls.fn = fn


def _cancelled() -> bool:
    fn = getattr(_cancel_tls, "fn", None)
    try:
        return bool(fn and fn())
    except Exception:  # pylint: disable=broad-except
        return False


def cancel_requested() -> bool:
    """True when this thread's build has been killed (see set_cancel_check)."""
    return _cancelled()

import yaml, requests, boto3
from botocore.config import Config as BotocoreConfig
from strands.agent.agent import Agent, BedrockModel
from opentelemetry.context import attach, get_current
from opentelemetry.propagate import extract, inject


_primitiveJSON = Union[str, int, float, bool, None]
JSON: TypeAlias = Union[_primitiveJSON, List['JSON'], Dict[str, 'JSON']]
NEWLINE = "\n"
HOME = os.environ["HOME"]
s3_client = boto3.client('s3')

# Bedrock Converse client config for formalizer inference.
#
# The botocore DEFAULT read_timeout is 60s. Sonnet 4.5's layer-3 responses
# (large description-logic theories) routinely take longer than that to
# generate, and at high worker fan-out (pool size ~95) every call was read-
# timing-out on the `converse` endpoint, then sleeping the capped 120s and
# retrying forever -- wedging a whole build at layer 3 with zero completions
# (Hypertension, 2026-10-17), distinct from the earlier throttle/backoff wedge.
# A generous read_timeout lets a slow-but-healthy Sonnet response finish
# instead of being aborted client-side. connect_timeout stays short (a real
# connect failure should fail fast), and the adaptive retry mode adds a second
# line of defence for transient throttles on top of our own retry loop.
BEDROCK_RUNTIME_CONFIG = BotocoreConfig(
    region_name="us-east-1",
    connect_timeout=10,
    read_timeout=900,
    retries={"max_attempts": 3, "mode": "adaptive"},
)
_BOTO3_CLIENT_LOCK = threading.Lock()


def exactly_one_is_non_none(*values) -> bool:
    """
    Return True if exactly one of the inputs is not None
    """
    return sum(x is not None for x in values) == 1


def at_most_one_is_non_none(*values) -> bool:
    """
    Return True if exactly one of the inputs is not None
    """
    return sum(x is not None for x in values) <= 1


def get_global_stats() -> "Statistics":
    """
    For some reason can't use walrus operator here?!?
    """
    g.STATS = g.STATS or Statistics()
    return g.STATS

@contextmanager
def timing(name: str):
    """
    Context manager for timing a block of code.
    """
    start_time = time.time()
    yield
    print(f"{name} took {(time.time() - start_time)/60:.2f} mins")


# Characters a literal (|) block cannot hold as-is: a carriage return is a
# line break to the YAML reader but PyYAML's literal writer emits it raw
# without re-indenting, so the text after it falls out of the block (a lone
# \r corrupts or breaks the file; \r\n silently becomes \n). The other YAML
# line breaks are normalized on reading, and C0/C1 controls, BOM and
# non-characters are not allowed unescaped at all. Strings containing any of
# these are double-quoted (escaped) instead.
_LITERAL_UNSAFE_RE = re.compile(
    "[\r\x85\u2028\u2029\ufeff\ufffe\uffff\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")


def literal_style_ok(s: str) -> bool:
    """
    True if `s` is multi-line and round-trips exactly through a literal block.

    >>> literal_style_ok("a\\nb"), literal_style_ok("one line")
    (True, False)
    >>> literal_style_ok("a\\r\\nb"), literal_style_ok("a\\rb\\nc")
    (False, False)
    """
    return "\n" in s and not _LITERAL_UNSAFE_RE.search(s)


class ForcedLiteralDumper(yaml.Dumper):
    """
    Custom YAML dumper that writes multi-line strings in literal style, which
    keeps theories and logs readable. Strings a literal block cannot hold
    exactly (see literal_style_ok) keep PyYAML's own (escaped) style.
    """
    def write_line_break(self, data=None):
        super().write_line_break(data)

    def choose_scalar_style(self):
        if self.event.value and literal_style_ok(self.event.value):
            return "|"
        return super().choose_scalar_style()


def repr_str(dumper, data):
    """
    Custom representation for strings in YAML.
    """
    assert isinstance(data, str)
    if literal_style_ok(data):
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    else:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data)


def repr_list(dumper, data):
    """
    Custom representation for lists in YAML.
    """
    assert isinstance(data, list)
    if all(isinstance(x, (int, float)) for x in data):
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    else:
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)


ForcedLiteralDumper.add_representer(str, repr_str)
ForcedLiteralDumper.add_representer(list, repr_list)


def yaml_dump(obj) -> str:
    """
    Custom version of yaml.dump that uses ForcedLiteralDumper. The output ends
    with yaml.dump's single newline: stripping it would chop the trailing
    newline off a literal block that ends the document (e.g. a theory).
    """
    return yaml.dump(obj, Dumper=ForcedLiteralDumper,
                     default_flow_style=False, width=float('inf'))


def yaml_dump_all(objs: list, file) -> str:
    """ Wrapper around yaml_dump: documents separated by --- lines. """
    file.write("---\n".join(yaml_dump(obj) for obj in objs))


def test_yaml_representation():
    """
    Make sure it's printing out multi-line strings and lists of numbers
    correctly.
    """
    data_dir = Path.cwd() / "data"
    input_data = json.loads((data_dir / "yaml-representation-test.json").read_text())
    output = yaml_dump(input_data)
    # The fixture opens with a license header (comment lines), which yaml_dump
    # does not write; compare from the first line of YAML.
    expected_lines = (data_dir / "yaml-representation-test.yaml").read_text().splitlines()
    while expected_lines and (not expected_lines[0].strip()
                              or expected_lines[0].startswith("#")):
        expected_lines.pop(0)
    expected_output = "\n".join(expected_lines).strip()
    # print(output)
    diff = list(difflib.unified_diff([l.rstrip() for l in output.splitlines()],
                                     [l.rstrip() for l in expected_output.splitlines()],
                                     fromfile='out', tofile='expout', lineterm=''))
    for line in diff:
        print(line)
    assert len(diff) == 0


def shorten_dict(d: dict, *keys):
    """ 
    Simplify a dictionary by replacing long and less-useful keys like "theory".

    >>> shorten_dict({"a": 42, "b": 42}, "a")
    {'a': '$elided (len 2)$', 'b': 42}

    >>> shorten_dict({"a": 42, "b": 42}, "a", "b")
    {'a': '$elided (len 2)$', 'b': '$elided (len 2)$'}
    """
    rv = dict(d)
    for key in keys:
        if key in rv:
            rv[key] = f"$elided (len {len(str(rv[key]))})$"
    return rv


def remove_comments(script: str) -> str:
    """
    Remove to-end-of-line comments from a cvc5 script
    """
    lines = script.split("\n")
    new_lines = [m.group(1) if (m := re.match(r"^(.*?);.*$", line)) else line
                 for line in lines]
    rv = "\n".join(line for line in new_lines if strip(line) != "")
    return rv


def test_remove_comments():
    """ test remove_comments() """
    def normalize_ws(script: str) -> str:
        return "\n".join(line.lstrip() for line in script.split("\n"))

    script = normalize_ws("""\
        (set-logic UF)
        ; Declare sorts
        (declare-sort Disease 0)
        (declare-sort Organ 0)
        (declare-sort Hormone 0)
        (declare-sort Cell 0)
        (declare-sort Symptom 0)
        (declare-sort Biomarker 0)
        ; Declare constants
        (declare-const diabetes Disease)
        (declare-const pancreas Organ)
        (declare-const insulin Hormone)
        (declare-const body-cells Cell)
        (declare-const blood-sugar Biomarker)
        (declare-const polydipsia Symptom)
        (declare-const polyuria Symptom)
        (declare-const polyphagia Symptom)
        (declare-const weight-loss Symptom)
        (declare-const blurred-vision Symptom)
        ; Declare predicates
        (declare-fun is-endocrine-disease (Disease) Bool)
        (declare-fun is-common (Disease) Bool)
        (declare-fun is-group-of-diseases (Disease) Bool)
        (declare-fun characterized-by (Disease Biomarker) Bool)
        (declare-fun is-high (Biomarker) Bool)
        (declare-fun is-sustained (Biomarker) Bool)
        (declare-fun produces (Organ Hormone) Bool)
        (declare-fun produces-enough (Organ Hormone) Bool)
        (declare-fun responsive-to (Cell Hormone) Bool)
        (declare-fun has-symptom (Disease Symptom) Bool)
        (declare-fun is-excessive-thirst (Symptom) Bool)
        (declare-fun is-excessive-urination (Symptom) Bool)
        (declare-fun is-excessive-hunger (Symptom) Bool)
        (declare-fun is-weight-loss (Symptom) Bool)
        (declare-fun is-blurred-vision (Symptom) Bool)
        ; Assertions
        ; Diabetes is a group of common endocrine diseases
        (assert (is-group-of-diseases diabetes))
        (assert (is-common diabetes))
        (assert (is-endocrine-disease diabetes))
        ; Characterized by sustained high blood sugar
        (assert (characterized-by diabetes blood-sugar))
        (assert (is-high blood-sugar))
        (assert (is-sustained blood-sugar))
        ; Causation: insufficient insulin production OR unresponsive cells
        (assert (=> (or (not (produces-enough pancreas insulin))
                        (not (responsive-to body-cells insulin)))
                    (= diabetes diabetes)))
        ; Classic symptoms
        (assert (has-symptom diabetes polydipsia))
        (assert (has-symptom diabetes polyuria))
        (assert (has-symptom diabetes polyphagia))
        (assert (has-symptom diabetes weight-loss))
        (assert (has-symptom diabetes blurred-vision))
        ; Symptom descriptions
        (assert (is-excessive-thirst polydipsia))
        (assert (is-excessive-urination polyuria))
        (assert (is-excessive-hunger polyphagia))
        (assert (is-weight-loss weight-loss))
        (assert (is-blurred-vision blurred-vision))
        (check-sat)""")
    assert remove_comments(script) == normalize_ws("""\
        (set-logic UF)
        (declare-sort Disease 0)
        (declare-sort Organ 0)
        (declare-sort Hormone 0)
        (declare-sort Cell 0)
        (declare-sort Symptom 0)
        (declare-sort Biomarker 0)
        (declare-const diabetes Disease)
        (declare-const pancreas Organ)
        (declare-const insulin Hormone)
        (declare-const body-cells Cell)
        (declare-const blood-sugar Biomarker)
        (declare-const polydipsia Symptom)
        (declare-const polyuria Symptom)
        (declare-const polyphagia Symptom)
        (declare-const weight-loss Symptom)
        (declare-const blurred-vision Symptom)
        (declare-fun is-endocrine-disease (Disease) Bool)
        (declare-fun is-common (Disease) Bool)
        (declare-fun is-group-of-diseases (Disease) Bool)
        (declare-fun characterized-by (Disease Biomarker) Bool)
        (declare-fun is-high (Biomarker) Bool)
        (declare-fun is-sustained (Biomarker) Bool)
        (declare-fun produces (Organ Hormone) Bool)
        (declare-fun produces-enough (Organ Hormone) Bool)
        (declare-fun responsive-to (Cell Hormone) Bool)
        (declare-fun has-symptom (Disease Symptom) Bool)
        (declare-fun is-excessive-thirst (Symptom) Bool)
        (declare-fun is-excessive-urination (Symptom) Bool)
        (declare-fun is-excessive-hunger (Symptom) Bool)
        (declare-fun is-weight-loss (Symptom) Bool)
        (declare-fun is-blurred-vision (Symptom) Bool)
        (assert (is-group-of-diseases diabetes))
        (assert (is-common diabetes))
        (assert (is-endocrine-disease diabetes))
        (assert (characterized-by diabetes blood-sugar))
        (assert (is-high blood-sugar))
        (assert (is-sustained blood-sugar))
        (assert (=> (or (not (produces-enough pancreas insulin))
                        (not (responsive-to body-cells insulin)))
                    (= diabetes diabetes)))
        (assert (has-symptom diabetes polydipsia))
        (assert (has-symptom diabetes polyuria))
        (assert (has-symptom diabetes polyphagia))
        (assert (has-symptom diabetes weight-loss))
        (assert (has-symptom diabetes blurred-vision))
        (assert (is-excessive-thirst polydipsia))
        (assert (is-excessive-urination polyuria))
        (assert (is-excessive-hunger polyphagia))
        (assert (is-weight-loss weight-loss))
        (assert (is-blurred-vision blurred-vision))
        (check-sat)""")


@contextmanager
def profiling(do_profile: bool, prefix: str):
    """
    Context manager for profiling a block of code.
    """
    if do_profile:
        profiler = cProfile.Profile()
        print(f"profiling ({prefix}): profiler: {profiler}")
        profiler.enable()
        print(f"profiling ({prefix}): enabled profiler")
        yield
        print(f"profiling ({prefix}): about to disable profiler")
        profiler.disable()
        print(f"profiling ({prefix}): disabled profiler")
        try:
            file_name = f"profs/{prefix}_{str(uuid.uuid4())}.prof"
            assert (Path.cwd() / "profs").exists()
            print(f"profiling ({prefix}): before '{file_name}'")
            stats = pstats.Stats(profiler)
            print(f"profiling ({prefix}): created stats obj: {stats}")
            stats.dump_stats(file_name)
            print(f"profiling ({prefix}): after")
            sys.stdout.flush()
        except BaseException as ex:  # pylint: disable=broad-exception-caught
            print(f"profiling ({prefix}): caught {ex}")
            sys.stdout.flush()
        print(f"profiling ({prefix}): dumped stats")
    else:
        yield


def common_prefix_len(strs: List[str]) -> int:
    """
    >>> common_prefix_len(["a", "b", "c"])
    0
    >>> common_prefix_len(["aaa", "abb"])
    1
    >>> common_prefix_len(["aaa", "aaa", "aaa"])
    3
    """
    min_len = min(len(s) for s in strs)
    for idx in range(min_len):
        # print(f"idx {idx}: {[s[:idx+1] for s in strs]} {set(s[:idx+1] for s in strs)}")
        if len(set(s[:idx+1] for s in strs)) != 1:
            return idx
    return min_len


def save_to_s3(buf: bytes, bucket_name: str, key: str, content_type: str):
    """
    Save some bytes to s3.
    """
    print(f"save_to_s3 buf <<{buf}>> {bucket_name} {key} {content_type}")
    assert "{" not in key, key
    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=buf,
        ContentType=content_type # Set the content type for proper handling
    )
    return f"s3://{bucket_name}/{key}"


def save_yaml_to_s3(data: dict, bucket_name: str, key: str):
    """
    Save a dict as a YAML string to s3.
    """
    return save_to_s3(yaml_dump(data).encode(),
                      bucket_name,
                      key,
                      "text/yaml"
    )


def load_s3_object(s3_uri: str) -> str:
    """
    Load an object from S3 given a URI like 's3://bucket/key.yaml'
    
    Args:
        s3_uri: S3 URI in format 's3://bucket/key'
    
    Returns:
        The object content
    """
    parsed = urlparse(s3_uri)
    bucket = parsed.netloc
    s3_key = parsed.path.lstrip('/')
    response = s3_client.get_object(Bucket=bucket, Key=s3_key)
    content = response['Body'].read()
    if isinstance(content, bytes):
        content = content.decode('utf-8')
    return content


def load_yaml_from_s3(s3_uri: str) -> Any:
    """
    Load and parse a YAML object from an s3 URI.
    """
    print(f"load_yaml_from_s3 {s3_uri}")
    str_buf = load_s3_object(s3_uri)
    # print(f"str_buf <<{str_buf}>>")
    rv = yaml.safe_load(str_buf)
    # print(f"load_yaml_from_s3 -> {rv}")
    return rv


def extract_key_structure(x):
    """
    Return a simplified version of the dict that makes it easier to
    see what the keys are. Useful for debugging.
    """
    if isinstance(x, dict):
        return {k: extract_key_structure(v) for k, v in x.items()}
    elif isinstance(x, list):
        return []
    elif isinstance(x, str):
        return ""
    else:
        return x


def get_ecs_task_id() -> str:
    """
    Extract the ECS task ID from the container metadata.
    """
    try:
        metadata_uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4')
        if metadata_uri:
            response = requests.get(f"{metadata_uri}/task", timeout=31)
            task_metadata = response.json()
            task_arn = task_metadata['TaskARN']
            task_id = task_arn.split('/')[-1]
            return task_id
        else:
            print("ECS_CONTAINER_METADATA_URI_V4 not found")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        print(f"Caught in get_ecs_task_id: {ex}")
        traceback.print_exc()
    return None


def compute_ecs_task_cost(start_time: float) -> Optional[float]:
    """
    Use the container metadata to determine how much this ECS task
    has cost so far.
    """
    try:
        duration_seconds = time.time() - start_time

        metadata_uri = os.environ['ECS_CONTAINER_METADATA_URI_V4'] + '/task'
        task_metadata = requests.get(metadata_uri, timeout=31).json()
        print(f"cetc: task_metadata: {task_metadata}")

        cpu_vcpus = float(task_metadata["Limits"]["CPU"]) / 1024
        memory_gb = float(task_metadata["Limits"]["Memory"]) / 1024

        cpu_cost = cpu_vcpus * g.cpu_price_per_second * duration_seconds
        memory_cost = memory_gb * g.memory_price_per_second * duration_seconds
        total_cost = cpu_cost + memory_cost
        return total_cost
    except Exception as ex:  # pylint: disable=broad-exception-caught
        print(f"Caught in compute_ecs_task_cost: {ex}")
        traceback.print_exc()
        return None


def signal_handler(sig, _stack_frame):
    """ Handle signals like SIGINT and SIGTERM """
    print(f"Signal {sig} received, performing cleanup and exiting..."   )
    sys.exit(0)


def register_signal_handlers():
    """ Register signal handlers for SIGINT and SIGTERM """
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def join_with_and(*items: List[str]) -> str:
    """
    Join a list of strings with commas and "and" before the last item.

    >>> join_with_and("apple", "banana", "cherry")
    'apple, banana, and cherry'

    >>> join_with_and("apple", "banana")
    'apple and banana'

    >>> join_with_and("apple")
    'apple'

    >>> join_with_and()
    ''
    """
    if len(items) == 0:
        return ""
    elif len(items) == 1:
        return items[0]
    elif len(items) == 2:
        return f"{items[0]} and {items[1]}"
    else:
        return ", ".join(items[:-1]) + f", and {items[-1]}"


# def create_agent(model_id: str,
#                  system_prompt: Optional[str] = None,
#                  callback_handler=None,
#                  temperature: Optional[float] = None) -> Agent:
#     """
#     Create an agent for the specified model_id.
#     """
#     kwargs = {"temperature": temperature} if temperature is not None else {}
#     model = BedrockModel(
#                 model_id=model_id,
#                 **kwargs
#             )
#     return Agent(system_prompt=system_prompt,
#                  model=model,
#                  callback_handler=callback_handler)


class Sat(Enum):
    """ Type-safe way of representing result from cvc5. """
    SAT = 0
    UNSAT = 1
    UNKNOWN = 2
    ERROR = 3 # cvc5 failed due to something like parse error


def set_max_num_files(max_num_files: int):
    """ do ulimit -n max_num_files """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if max_num_files <= soft:
        if max_num_files < soft:
            print(f"Not setting max_files as request ({max_num_files})"
                  f" is less than {soft}")
    elif max_num_files > hard:
        print(f"Can't set max_files as request ({max_num_files})"
              f" is greater than the hard limit ({hard})")
    else:
        resource.setrlimit(resource.RLIMIT_NOFILE, (max_num_files, hard))
        print(f"Updated max_files limit to {max_num_files}")


def indent_but_1st(text: str, indent: str) -> str:
    """
    Indent all lines of text except the first one.

    >>> indent_but_1st("hello\\nworld", "  ")
    'hello\\n  world'

    >>> indent_but_1st("single line", ">> ")
    'single line'

    >>> indent_but_1st("", ">> ")
    ''
    """
    lines = text.split("\n")
    if len(lines) == 0:
        return text
    indented_lines = [lines[0]] + [indent + line for line in lines[1:]]
    return "\n".join(indented_lines)


def flatten1(nested_list: List[List[Any]]) -> List[Any]:
    """
    Flatten a list of lists by one level.

    >>> flatten1([[1, 2], [3, 4]])
    [1, 2, 3, 4]

    >>> flatten1([[1], [], [2, 3]])
    [1, 2, 3]

    >>> flatten1([])
    []

    """
    result = []
    for sublist in nested_list:
        for item in sublist:
            result.append(item)
    return result


def mp_worker(trace_context: dict,
              q: Queue,
              serialized_globals: dict,
              job_prefix: str,
              do_profile: bool,
              job_num: int,
              retries: int,
              func: Callable, *args):
    """
    Wrapper around caller's code that takes care of stdout, queueing,
    and OpenTelemetry context propagation.
    """
    with profiling(do_profile, "mp-worker"):
        g.deserialize(serialized_globals)
        retries_str = f"-{retries}" if retries > 0 else ""
        sys.stdout = PrefixStream(f"{job_prefix}-{job_num}{retries_str}> ",
                                sys.stdout)
        sys.stderr = PrefixStream(f"{job_prefix}-{job_num}{retries_str} stderr> ",
                                sys.stderr)
        start_time = time.time()
        attach(extract(trace_context))
        # make sure KeyboardInterrupt in parent kills children:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(1, signal.SIGTERM) # PR_SET_PDEATHSIG is 1
        result = func(*args, job_num=job_num, retries=retries)
        q.put((job_num, retries, time.time() - start_time, result))
        sys.stdout = sys.stdout.original # probably not necessary, but can't hurt
        sys.stderr = sys.stderr.original # ditto


def _ppe_worker(trace_context: dict,
                serialized_globals: dict,
                job_prefix: str,
                do_profile: bool,
                job_num: int,
                retries: int,
                func: Callable, *args):
    """
    ProcessPoolExecutor variant of :func:`mp_worker`.

    Does the same per-worker setup as mp_worker -- restores globals, OTel
    context, PR_SET_PDEATHSIG, and prefixed stdout/stderr -- but RETURNS the
    ``(job_num, retries, duration, result)`` tuple instead of putting it on a
    shared queue. starmap_with_dupes now drives a ProcessPoolExecutor (stable
    worker set, no manual terminate()/kill()), so the executor collects the
    return value and there is no shared ctx.Queue() whose POSIX semaphore the
    old hand-rolled churn could unlink mid-spawn (the SemLock._rebuild
    FileNotFoundError the Fargate builder hit).
    """
    with profiling(do_profile, "mp-worker"):
        g.deserialize(serialized_globals)
        retries_str = f"-{retries}" if retries > 0 else ""
        sys.stdout = PrefixStream(f"{job_prefix}-{job_num}{retries_str}> ",
                                sys.stdout)
        sys.stderr = PrefixStream(f"{job_prefix}-{job_num}{retries_str} stderr> ",
                                sys.stderr)
        start_time = time.time()
        attach(extract(trace_context))
        # make sure the parent dying kills children (Linux only; best-effort so
        # a non-Linux local run doesn't hard-fail the whole pool).
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG is 1
        except OSError:
            pass
        try:
            result = func(*args, job_num=job_num, retries=retries)
            return (job_num, retries, time.time() - start_time, result)
        finally:
            sys.stdout = sys.stdout.original
            sys.stderr = sys.stderr.original


def _proc_table() -> Dict[int, Tuple[int, int]]:
    """pid -> (ppid, session id) for every live process, from /proc (Linux)."""
    table: Dict[int, Tuple[int, int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf8") as f:
                stat = f.read()
        except OSError:
            continue
        # The command name (field 2) may contain spaces; fields after the
        # closing ')' are: state ppid pgrp session ...
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            table[int(entry)] = (int(fields[1]), int(fields[3]))
        except (IndexError, ValueError):
            continue
    return table


def kill_process_tree(root_pid: int, include_root: bool = True,
                      stop_only: bool = False) -> List[int]:
    """
    SIGKILL `root_pid`, every descendant of it, and every other member of any
    session a descendant started. The session sweep catches processes that
    escaped the tree: kiro-cli runs kiro-cli-chat, which runs a node server in
    its own process group, and those survive a kill of kiro-cli alone (then
    get re-parented, so a parent-pid walk would miss them).

    Never touches our own process or session. Returns the pids signalled.
    Linux only (reads /proc); a no-op elsewhere.
    """
    if not os.path.isdir("/proc"):
        return []
    me = os.getpid()
    my_sid = os.getsid(0)

    def find_targets() -> Set[int]:
        table = _proc_table()
        children: Dict[int, List[int]] = defaultdict(list)
        for pid, (ppid, _sid) in table.items():
            children[ppid].append(pid)
        tree, stack = set(), [root_pid]
        while stack:
            pid = stack.pop()
            if pid in tree:
                continue
            tree.add(pid)
            stack.extend(children.get(pid, []))
        sessions = {table[p][1] for p in tree if p in table}
        # A root started with start_new_session=True leads a session whose id
        # is its pid; sweep that session even if the root has already exited
        # (its orphaned children are then re-parented and not in `tree`).
        sessions.add(root_pid)
        sessions.discard(my_sid)
        found = tree | {pid for pid, (_pp, sid) in table.items() if sid in sessions}
        if not include_root:
            found.discard(root_pid)
        found.discard(me)
        return found

    stopped = _stop_tree(find_targets)
    if stop_only:  # the caller kills them (kill_process_trees)
        return sorted(stopped)
    return _sigkill(stopped)


def _stop_tree(find_targets: Callable[[], Set[int]]) -> Set[int]:
    """
    SIGSTOP every process `find_targets` returns, rescanning until nothing
    new appears; returns the pids stopped. A process that forks between a
    /proc snapshot and its SIGKILL would leave a child the snapshot missed,
    re-parented and out of reach once its parent is gone. A stopped process
    cannot fork, and a child it already started is still its child.
    """
    stopped: Set[int] = set()
    for _ in range(10):
        new = find_targets() - stopped
        if not new:
            break
        for pid in new:
            try:
                os.kill(pid, signal.SIGSTOP)
            except (ProcessLookupError, PermissionError):
                pass
            stopped.add(pid)
    return stopped


def _sigkill(pids: Set[int]) -> List[int]:
    killed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return killed


def kill_process_trees(root_pids: List[int]) -> List[int]:
    """
    kill_process_tree for several roots at once: stop every tree before
    killing any. Killing one pool worker first makes ProcessPoolExecutor
    treat the pool as broken and terminate the others itself, which orphans
    their session-leading children before they could be found (seen as a
    leftover `sleep` in test_pool_kills_leftover_runs_on_return).
    """
    stopped: Set[int] = set()
    for root in root_pids:
        stopped |= set(kill_process_tree(root, stop_only=True))
    return _sigkill(stopped)


def starmap_with_dupes(
        worker: Callable,
        arguments: List[list],
        job_prefix: str,
        pool_size: int,
        do_spawn_dupes: bool = True,
        choose_between_results_fn: Callable[[dict, dict], int] = None,
        quit_when_get_first_result: bool = False,
        job_successful_p: Optional[Callable[[dict], bool]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        retry_failed_runs: bool = False
        ) -> List[Any]:
    """
    Like Pool.starmap except that a job may be run multiple times.

    `retry_failed_runs` (for use with do_spawn_dupes=False) re-runs a job only
    after a run of it FAILED -- no speculative copies of jobs that are still
    running. Without it, do_spawn_dupes=False never re-runs a failed job (the
    chatbot's quit-on-first-result pools rely on that). The kiro theory engine
    uses this: each kiro-cli copy is a full, credit-spending session, and a
    unit almost always succeeds first time, so speculative duplicates are
    mostly waste.

    `on_event`, if given, is called in THIS (parent) process for every run of
    a job: {"event": "start", "job": n, "attempt": r} when a run is submitted
    and {"event": "end", "job": n, "attempt": r, "ok": bool, "result": dict |
    None, "duration": secs, "stats": Statistics} when it returns (result
    None, and no "stats", if the worker crashed). It is for progress reporting only; its exceptions are logged and
    swallowed so it can never disturb the pool.
    This is useful if there is one job that both takes much longer than the
    others and is unpredictable. Rather than waiting for this one process to
    finish, we can restart it incase the newly-created duplicate job
    finishes first.

    Since cores are effectively free, we aggressively create dupes of 
    existing jobs to fill up the pool. Since job durations are unpredictable,
    the later-created job may finish first.

    When quit_when_get_first_result is True, we return as soon as we get
    one result (this is useful when we only care about one successful result
    as all the jobs are the same and we just want the fastest one).
    """
    print(f"starmap_with_dupes {len(arguments):,} jobs, pool size {pool_size:,}")
    if quit_when_get_first_result:
        assert job_successful_p is not None
    # On the general (collect-all) path a missing predicate means "every result
    # is acceptable" -- the previous Process/Queue implementation would have
    # crashed calling None(result_dict), so a None here only ever reached the
    # quit path (guarded above). Default it so the collect-all path is safe.
    if job_successful_p is None:
        job_successful_p = lambda _result_dict: True  # noqa: E731
    job_num_2_args = dict(enumerate(arguments))
    all_job_nums = job_num_2_args.keys()
    jobs_not_yet_started = list(all_job_nums)
    random.shuffle(jobs_not_yet_started)

    trace_context = {}
    inject(trace_context, get_current())
    serialized_globals = g.serialize()
    do_profile = g.p.do_profile

    results: Dict[int, Any] = {} # job num => result
    durations: Dict[int, float] = {} # job_num => duration in secs
    job_nums_started: Dict[int, int] = defaultdict(int) # figure out when a new job is a dupe
    rerun_jobs: Set[int] = set() # jobs that have been rerun
    failed_results: List[dict] = [] # use these to roll up Stats from failed runs

    # A ProcessPoolExecutor over the spawn context replaces the old hand-rolled
    # ctx.Process + shared ctx.Queue() + terminate()/kill() churn. That churn
    # raced the queue's POSIX semaphore: a freshly spawned child unpickling the
    # shared Queue called SemLock._rebuild() while the parent was unlinking a
    # just-killed worker's semaphore, so children died with
    # "FileNotFoundError: SemLock._rebuild" (seen on the Fargate builder, which
    # has a healthy 16 GB /dev/shm -- so it was never a shared-memory shortage).
    # The executor owns a STABLE set of workers and its own result plumbing, so
    # no semaphore is racily unlinked mid-spawn. We keep the dupe semantics by
    # submitting extra runs of unfinished jobs to fill idle slots, and keep the
    # same worker contract (func(*args, job_num=, retries=) -> 4-tuple) via the
    # module-level _ppe_worker wrapper.
    ctx = mp.get_context('spawn')
    executor = ProcessPoolExecutor(max_workers=pool_size, mp_context=ctx)
    # future => (job_num, retries)
    pending: Dict[Any, Tuple[int, int]] = {}

    def emit(event: dict) -> None:
        """ Report a run start/end to on_event; never let it break the pool. """
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception as ex:  # pylint: disable=broad-except
            print(f"swd: on_event raised {ex!r}; ignoring")

    def submit_worker(job_num: int) -> None:
        """ Submit one run of a specific job to the executor. """
        retries = job_nums_started[job_num]
        fut = executor.submit(_ppe_worker, trace_context, serialized_globals,
                              job_prefix, do_profile, job_num, retries, worker,
                              *job_num_2_args[job_num])
        pending[fut] = (job_num, retries)
        job_nums_started[job_num] += 1
        emit(dict(event="start", job=job_num, attempt=retries))

    def maybe_submit_a_worker() -> bool:
        """ Pick the next job (fresh, else a dupe) and submit it. """
        if len(jobs_not_yet_started) > 0:
            new_job_num = jobs_not_yet_started.pop()
            submit_worker(new_job_num)
            return True
        elif do_spawn_dupes:
            # No fresh jobs left: dupe a non-finished one (prefer one not yet
            # rerun) so a slow straggler can be beaten by a later-started copy.
            unfinished_jobs = all_job_nums - results.keys()
            if not unfinished_jobs:
                return False
            not_yet_rerun_jobs = unfinished_jobs - rerun_jobs
            rerun_job_num = choice(list(not_yet_rerun_jobs if len(not_yet_rerun_jobs) > 0
                                                           else unfinished_jobs))
            submit_worker(rerun_job_num)
            rerun_jobs.add(rerun_job_num)
            return True
        return False

    def after_failed_run(job_num: int) -> None:
        """
        Refill the slot a failed run vacated: with retry_failed_runs, re-run
        that job (unless it already has a result or another run in flight);
        otherwise behave as before.
        """
        if retry_failed_runs and job_num not in results \
                and all(j != job_num for j, _r in pending.values()):
            print(f"swd: retrying job {job_num} after a failed run")
            submit_worker(job_num)
        else:
            maybe_submit_a_worker()

    def return_fn(results: List[Any]) -> List[Any]:
        """ Central place for returning results: shut the pool down cleanly. """
        # cancel_futures drops not-yet-started submissions; running children are
        # terminated by shutdown(). No manual terminate()/kill() loop, so no
        # semaphore-unlink race.
        print("swd: shutting down worker pool")
        # shutdown(wait=False) only stops NEW work: runs already executing
        # (duplicates of finished jobs, a cancelled build's workers) keep going
        # -- a layer-1 duplicate was seen still running a kiro-cli session five
        # hours after layer 1 ended, holding a session slot. Kill every still-
        # running worker and everything it started.
        workers = list((getattr(executor, "_processes", None) or {}).values())
        if pending:
            # Stop every worker's tree before killing any (kill_process_trees),
            # and before shutdown, so the pool cannot terminate a worker first
            # and orphan what it started.
            alive = [proc.pid for proc in workers if proc.is_alive()]
            if alive:
                killed = kill_process_trees(alive)
                print(f"swd: killed {len(alive)} leftover worker(s) "
                      f"({len(killed)} process(es))")
        executor.shutdown(wait=False, cancel_futures=True)
        return results

    # Fill the pool to pool_size (fresh jobs, then dupes if enabled).
    while len(pending) < pool_size:
        if not maybe_submit_a_worker():
            break

    while True:
        # Poll with a short timeout rather than blocking forever, so a user kill
        # (cooperative cancel) is noticed within ~1s even while all workers are
        # mid-LLM-call, not only when one happens to finish.
        if _cancelled():
            print("swd: cancel requested; tearing down worker pool")
            return return_fn(list(results.values()))
        if not pending:
            # Nothing in flight and nothing above decided to break: done.
            break
        done, _not_done = concurrent_wait(
            pending.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
        if not done:
            continue
        for fut in done:
            job_num, retries = pending.pop(fut)
            try:
                _jn, _retries, duration, result = fut.result()
            except Exception as ex:  # pylint: disable=broad-except
                # A worker that raised instead of returning a 4-tuple: treat it
                # like the old "result is None" (crashed worker) case -- skip it
                # and let a dupe/retry cover the job.
                print(f"swd: worker for job {job_num} raised {ex!r}; skipping")
                traceback.print_exc()
                emit(dict(event="end", job=job_num, attempt=retries, ok=False,
                          result=None, duration=None))
                after_failed_run(job_num)
                continue
            print(f"swd: Got result for job {job_num} (retry #{retries:,})")
            print(f"swd: result: {result}")
            if result is None:
                # This is likely caused by an exception in the worker
                print("swd: result is None, skipping")
                emit(dict(event="end", job=job_num, attempt=retries, ok=False,
                          result=None, duration=None))
                after_failed_run(job_num)
                continue
            result_dict, stats, duration, _errors = result
            run_ok = bool(job_successful_p(result_dict))
            emit(dict(event="end", job=job_num, attempt=retries, ok=run_ok,
                      result=result_dict, duration=duration, stats=stats))
            if not run_ok:
                print("swd: not a valid result, keep going")
                failed_results.append(result)
                if retry_failed_runs:
                    after_failed_run(job_num)
                    continue
            elif quit_when_get_first_result:
                print("swd: quit_when_get_first_result is True; got a valid result")
                # include the failed results in the stats
                print(f"swd: combining stats from one successful and {len(failed_results):,} failed runs")
                overall_stats = Statistics.combine([stats] + [fr[1] for fr in failed_results])
                overall_errors = ""
                try:
                    overall_errors = "\n".join(r[3] for r in [result] + failed_results if r[3] is not None)
                except Exception as ex:  # pylint: disable=broad-except
                    print(ex)
                    traceback.print_exc()
                    for i, fr in enumerate(failed_results):
                        print(f"swd: fr #{i}: {fr}")
                result2 = (result_dict, overall_stats, duration, overall_errors)
                return return_fn([result2])
            else:
                if job_num not in results:
                    results[job_num] = result
                else:
                    if choose_between_results_fn is None:
                        # default to always using most recent result
                        results[job_num] = result
                    else:
                        try:
                            which = choose_between_results_fn(results[job_num], result)
                            results[job_num] = result if which == 1 else results[job_num]
                        except Exception as ex:  # pylint: disable=broad-except
                            print(f"swd: results[{job_num}]: {results[job_num]}")
                            print(f"swd: result: {result}")
                            print(f"swd: choose_between_results_fn raised {ex}; "
                                f"keeping existing result for job {job_num}")
                    durations[job_num] = duration
            # Keep the pool full: replace the slot the finished future vacated
            # with the next fresh job (or a dupe of an unfinished one).
            maybe_submit_a_worker()
        print(f"swd: Now have {len(results):,} results "
              f"for these jobs: {list(results.keys())}")
        if len(results) == len(arguments):
            print("swd: Finished all jobs")
            break

    durations_str = " ".join(f"{d/60.0:.2f}"
                             for d in sorted(list(durations.values())))
    print(f"swd: durations: {durations_str}")
    if quit_when_get_first_result:
        # Every run finished and none succeeded (a success returns above):
        # return the failed results so the caller sees them, rather than
        # raising KeyError for the jobs that have no successful result.
        return return_fn(failed_results)
    return return_fn([results[job_num] for job_num in job_num_2_args.keys()])


def test_worker():
    """ for test_starmap_with_dupes() """
    print("Hello from worker")
    if random.random() < 0.8:
        delay = random.random()
    else:
        delay = 30 + random.random() # occasional slow job
    print(f"Sleeping for {delay} secs")
    time.sleep(delay)
    return 42.0


def test_starmap_with_dupes():
    """
    Simple test that we are correctly spawning dupes
    (it'll run much more slowly if not).
    """
    level = 0
    result = starmap_with_dupes(worker=test_worker,
                                arguments=[[] for _ in range(10)],
                                job_prefix=f"l{level}",
                                pool_size=5,
                                do_spawn_dupes=True)
    print(f"result: {result}")


# def do_inference_with_prompt_caching(system_prompt: str,
#                                      prompt_prefix: str,
#                                      prompt_suffix: str) -> str:
#     print("do_inference_with_prompt_caching")
#     print(f"diwpc: prompt_prefix has {len(prompt_prefix):,} chars; cf 4096 token min")
#     print_multiline("diwpc: prompt_prefix> ", prompt_prefix)
#     print_multiline("diwpc: prompt_suffix> ", prompt_suffix)
#     client = boto3.client("bedrock-runtime", region_name="us-east-1")
#     messages = [
#         {
#             "role": "user",
#             "content": [
#                 {"text": prompt_prefix},
#                 {"cachePoint": {"type": "default"}}
#             ]
#         }
#     ]
#     if prompt_suffix is not None and prompt_suffix.strip() != "":
#         messages[0]["content"].append({"text": prompt_suffix})
#     response = client.converse(
#         modelId=g.MODEL_ID,
#         system=[
#             {
#                 "text": system_prompt
#             }
#         ],
#         messages=messages
#     )
#     rv = response["output"]["message"]["content"][0]["text"]
#     usage = response.get("usage", {})
#     print(f"diwpc: usage: {json.dumps(usage,indent=2)}")
#     print("Cache read tokens:", usage.get("cacheReadInputTokens", 0))
#     print("Cache write tokens:", usage.get("cacheWriteInputTokens", 0))
#     return rv


def do_inference(prompt_prefix: str,
                 stats: "Statistics",
                 system_prompt: Optional[str] = None,
                 prompt_suffix: Optional[str] = None,
                 temperature: Optional[float] = None,
                 model_id: str = None # defaults to g.MODEL_ID
                 ) -> str:
    """
    Wrapper around Converse API that is more resilient.
    """
    start_time = time.time()
    with start_span(name="do_inference") as span:
        span.update(input=dict(
            system_prompt=system_prompt,
            prompt_prefix=prompt_prefix,
            prompt_suffix=prompt_suffix,
            temperature=temperature
        ))
        model_id = model_id or g.MODEL_ID
        delay: float = 30.0
        MAX_INFERENCE_RETRY_DELAY: float = 120.0  # ceiling on the exponential backoff
        retry: int = 0
        n_input_tokens: int = -1
        n_output_tokens: int = -1
        # boto3.client() on the shared default session is not thread-safe
        # (callers such as compute_standalones run do_inference on threads);
        # the client itself is safe to use once built.
        with _BOTO3_CLIENT_LOCK:
            client = boto3.client("bedrock-runtime", config=BEDROCK_RUNTIME_CONFIG)

        print_multiline("di: prompt_prefix> ", prompt_prefix)
        print_multiline("di: prompt_suffix> ", prompt_suffix)
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": prompt_prefix},
                    {"cachePoint": {"type": "default"}}
                ]
            }
        ]
        if prompt_suffix is not None and prompt_suffix.strip() != "":
            messages[0]["content"].append({"text": prompt_suffix})

        converse_kwargs = {
            "modelId": model_id,
            "messages": messages
        }
        if system_prompt is not None and system_prompt.strip() != "":
            converse_kwargs["system"] = [{"text": system_prompt}]

        def return_fn(result: str, usage: dict) -> str:
            duration = time.time() - start_time
            if stats is not None:
                stats.add_inference(usage, model_id=model_id)
                stats.inference_durations.append(duration)
            span.update(output=dict(
                result=result,
                retries=retry,
                n_input_tokens=n_input_tokens,
                n_output_tokens=n_output_tokens,
            ))
            return result

        for retry in itertools.count():
            try:
                response = client.converse(**converse_kwargs)
                rv = "".join(x["text"] for x in response["output"]["message"]["content"])
                usage = response.get("usage", {})
                if response.get("stop_reason", None) == "content_filtered":
                    # Claude Sonnet 4.5 is released under ASL-3 protections,
                    # and they appear to have serious false positives.
                    print("di: Output blocked by content filtering policy")
                    stats.n_inference_exceptions += 1
                    model_id="us.anthropic.claude-3-7-sonnet-20250219-v1:0"
                    continue
                return return_fn(rv, usage)
            except Exception as ex:  # pylint: disable=broad-except

                def sleep_():
                    nonlocal delay
                    # Jitter (0.5x-1.5x) so concurrent callers throttled by the
                    # same burst don't all retry at the same instant and get
                    # throttled again together.
                    jittered = delay * random.uniform(0.5, 1.5)
                    print(f"di: Sleeping for {jittered:.1f} secs before retrying")
                    sleep(jittered)
                    # Cap the exponential backoff. Previously `delay *= 2` with no
                    # ceiling, which ballooned to 17h (61440s) sleeps once Bedrock
                    # started timing out under batch fan-out, wedging a whole build
                    # (Hypertension-1, 2026-10-15). A capped backoff retries a
                    # transient timeout promptly instead of parking the node.
                    delay = min(delay * 2, MAX_INFERENCE_RETRY_DELAY)

                print(f"di: Caught exception during inference: {str(ex)}")
                if "ValidationException" in str(ex):
                    # The request itself is invalid (e.g. "Input is too long
                    # for requested model"): the same request fails the same
                    # way every time, so retrying only wedges the caller (a
                    # build sat retrying one for 10 hours, 2026-11-09). Raise.
                    print("di: Got ValidationException; not retrying")
                    if stats is not None:
                        stats.n_inference_exceptions += 1
                    raise
                if "ThrottlingException" in str(ex):
                    print("di: Throttled")
                    stats.n_throttled_inferences += 1
                    sleep_()
                elif "ResourceNotFoundException" in str(ex):
                    # Sometimes we see this:
                    # botocore.errorfactory.ResourceNotFoundException: An error occurred (ResourceNotFoundException) when calling the ConverseStream operation: Model not found.
                    # └ Bedrock region: us-west-2
                    # └ Model id: global.anthropic.claude-sonnet-4-5-20250929-v1:0
                    # No idea why this happens, but a retry is the best we can do
                    print("di: Got ResourceNotFoundException")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "modelStreamErrorException" in str(ex):
                    print("di: Got modelStreamErrorException")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "Bad Gateway" in str(ex):
                    print("di: Got Bad Gateway exception")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "ModelErrorException" in str(ex):
                    print("di: Got botocore.errorfactory.ModelErrorException")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "Unable to locate credentials" in str(ex):
                    print("di: Got NoCredentialsError; no idea why, try again")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "An error occurred (internalServerException) when calling the ConverseStream operation" in str(ex):
                    print("di: Got internalServerException; no idea why, try again")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "Read timed out" in str(ex):
                    print("di: Got AWSHTTPSConnectionPool(...): Read timed out.")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "Read timeout on endpoint URL" in str(ex):
                    print("di: Got exception: Read timeout on endpoint URL")
                    stats.n_inference_exceptions += 1
                    sleep_()
                elif "Output blocked by content filtering policy" in str(ex):
                    print("di: Got content filtering exception")
                    stats.n_inference_exceptions += 1
                    sleep_()
                else:
                    print(f"di: got unknown exception: {ex}")
                    traceback.print_exc()
                    stats.n_inference_exceptions += 1
                    sleep_()
        return return_fn(None, {}) # should never reach here, shuts pylint up


def dump_dict(a_dict: dict):
    """
    Print a dict as yaml, with special handling for Sat and Path types.
    """
    new_dict = {k: str(v) if isinstance(v, (Sat, Path)) else v
                for k, v in a_dict.items()}
    yaml_dump(new_dict)


class PrefixStream:
    """
    Add a prefix to the start of every line. This is useful
    when multiprocessing and each job's outputs are spliced together.
    """
    def __init__(self, prefix: str, original: io.TextIOWrapper):
        """ Initialize with prefix and original stream """
        self._prefix = prefix
        self.original = original
        self._curr_line = []

    def write(self, text):
        """ wrapper around parent that adds prefix when at start of line """
        for char in text:
            self._curr_line.append(char)
            if char in {"\n", "\r"}:
                self.original.write(self._prefix + "".join(self._curr_line))
                self._curr_line = []

    def flush(self):
        """ pass through to parent """
        self.original.flush()


def augment(base_dict: dict, **kwargs) -> dict:
    """
    Given a dictionary, add some key-value pairs to it and return.

    >>> augment({})
    {}

    >>> augment(dict(a=1))
    {'a': 1}

    >>> augment(dict(a=1), b=2)
    {'a': 1, 'b': 2}
    """

    for k, v in kwargs.items():
        base_dict[k] = v
    return base_dict


def union(*dicts: List[dict]) -> dict:
    """
    >>> union({})
    {}

    >>> union(dict(a=1))
    {'a': 1}

    >>> union(dict(a=1), dict(b=2))
    {'a': 1, 'b': 2}
    """
    result = {}
    for a_dict in dicts:
        for k, v in a_dict.items():
            result[k] = v
    return result


def extract_code_blocks(text: str) -> List[dict]:
    """
    Given the response from an LLM inference, extract
    and return the strings enclosed by ```...```
    """
    pattern = r'```([a-zA-Z0-9_-]*)\n(.*?)```'
    matches = re.findall(pattern, text, re.DOTALL)
    rv = [dict(type=strip(pair[0]), contents=strip(pair[1]))
          for pair in matches]
    return rv


def test_extract_multiple_code_blocks():
    """ Test multiple code blocks """
    result = extract_code_blocks(dedent("""\
        abc
        ```result
        def
        ```

        ```
        foo
        ```
        ghi"""))
    assert result == [dict(type="result", contents="def"),
                      dict(type="", contents="foo")], result


def test_extract_multiple_code_blocks_2():
    """ Test multiple code blocks """
    result = extract_code_blocks(dedent("""\
        abc
        ```theory
        def
        ```

        ```q_logical_form
        foo
        ```
        ghi"""))
    assert result == [dict(type="theory", contents="def"),
                      dict(type="q_logical_form", contents="foo")], result


def test_extract_code_block_1():
    """ Test a single code block """
    assert extract_code_blocks(dedent("""\
        abc
        ```
        def
        ```
        ghi""")) == [dict(contents="def", type="")]


def test_extract_code_block_2():
    """ Test a single typed code block """
    assert extract_code_blocks(dedent("""\
        abc
        ```smt
        def
        ```
        ghi""")) == [dict(type="smt", contents="def")]


def extract_tag(text: str, tag: str) -> str:
    """
    Extract the contents of a specific XML/HTML-like tag from text.
    """
    pattern = f'<{tag}>(.*?)</{tag}>'
    match = re.search(pattern, text, re.DOTALL)
    rv = match.group(1).strip("`").strip("\n") if match else None
    # print(f"rv {rv}")
    return rv


assert extract_tag(dedent("""\
    abc
    <foo>
    def
    </foo>
    ghi"""), "foo") == "def"


assert extract_tag(dedent("""\
    abc
    <foo>blah</foo>
    def
    ```
    ghi"""), "foo") == "blah"


def print_multiline(prefix: str, contents: str):
    """
    Print contents line by line, prefixing each line with prefix.
    """
    try:
        if contents is None:
            print(f"{prefix}None")
        else:
            for line in contents.split("\n"):
                print(f"{prefix}{line}")
    except Exception as ex:  # pylint: disable=broad-except
        print(ex)


def strip(s):
    """
    Safe version of s.strip()
    
    >>> strip(None) == None
    s: Caught 'NoneType' object has no attribute 'strip'
    True

    >>> strip(42)
    s: Caught 'int' object has no attribute 'strip'
    42

    >>> strip(" hello ")
    'hello'

    >>> strip("\thello")
    'hello'
    """
    try:
        return s.strip()
    except Exception as ex:  # pylint: disable=broad-except
        print(f"s: Caught {ex}")
        return s


def remove_empty_lines(s: Optional[str],
                       log_exceptions: bool=True) -> str:
    """
    >>> remove_empty_lines(None, log_exceptions=False) == None
    True

    >>> remove_empty_lines(None) == None
    True

    >>> remove_empty_lines("abc")
    'abc'

    >>> print(remove_empty_lines(f"abc{NEWLINE}{NEWLINE}def"))
    abc
    def

    >>> print(remove_empty_lines(f"abc{NEWLINE}  {NEWLINE}def"))
    abc
    def
    """
    assert isinstance(s, str | None), f"type: {type(s)}"
    try:
        # print(f"remove_empty_lines: s={s}")
        if s is None:
            return s
        try:
            lines = s.split("\n")
        except Exception as ex:  # pylint: disable=broad-except
            print(f"rel: Caught {ex} during split: s={s}bbgnigtvvuuinecujfilcvgc")
            traceback.print_exc()
            return s
        lines = [line for line in lines if line.strip() != '']
        return "\n".join(lines)
    except Exception as ex:  # pylint: disable=broad-except
        if log_exceptions:
            # supress during unit testing
            print(f"rel: Caught {ex}")
            traceback.print_exc()
        return s


def get_matching_files(directory_path: Path, pattern: str) -> List[Path]:
    """
    Returns a list of full file paths within a given directory
    that match a specified pattern.

    Args:
        directory_path (str): The path to the directory to search.
        pattern (str): The filename pattern to match (e.g., "*.txt", "image_*.png").

    Returns:
        list: A list of full file paths that match the pattern.
    """
    full_pattern = os.path.join(directory_path, pattern)
    return [Path(x) for x in glob.glob(full_pattern)]


def remove_comments_from_theory(theory: str) -> str:
    """
    cvc5 uses semicolons for to-end-of-line comments

    >>> print(remove_comments_from_theory(f"abc{NEWLINE} ; comment{NEWLINE}def"))
    abc
    def

    """
    result = []
    for line in theory.split("\n"):
        m = re.match(r"^\s*;.*$", line)
        if not m:
            result.append(line)
    return "\n".join(result)


def compute_statistics(nums: List[Union[float,int]],
                       num_decimal_places: int = 2) -> dict:
    """
    >>> compute_statistics([2, 1, 3])
    {'min': 1, 'max': 3, 'mean': 2, 'stdev': 1.0, 'total': 6}

    >>> compute_statistics([])
    {'min': None, 'max': None, 'mean': None, 'stdev': None, 'total': None}
    """
    if len(nums) == 0:
        return dict(
            min=None,
            max=None,
            mean=None,
            stdev=None,
            total=None
        )
    else:
        return dict(
            min=round(min(nums), num_decimal_places),
            max=round(max(nums), num_decimal_places),
            mean=round(statistics.mean(nums), num_decimal_places),
            stdev=round(statistics.stdev(nums) if len(nums) > 1 else 0, num_decimal_places),
            total=round(sum(nums), num_decimal_places)
        )


def statistics_2_str(stats: dict, format_wrapper: Optional[str] = None) -> str:
    """

    >>> statistics_2_str(compute_statistics([2, 1, 3]), "foo: {s}")
    'foo: [1..3] mean 2 +- 1.0, total 6'

    >>> statistics_2_str(compute_statistics([2]), "foo: {s}")
    'foo: [2..2] mean 2 +- 0, total 2'

    >>> statistics_2_str(compute_statistics([2]))
    '[2..2] mean 2 +- 0, total 2'
    
    >>> statistics_2_str(compute_statistics([]))
    'no data'
    
    """
    # print(f"statistics_2_str: stats={stats}, format_wrapper={format_wrapper}")
    min_, max_, mean, stdev, total = \
        itemgetter("min", "max", "mean", "stdev", "total")(stats)
    if min_ is None:
        s = "no data"
    else:
        s = f"[{min_}..{max_}] mean {mean} +- {stdev}, total {total}"
    s = format_wrapper.format(s=s) if format_wrapper else s
    return s


def load_lines(path: Path) -> List[str]:
    """
    Load lines from a text file and remove empty ones.
    """
    return [line for line in path.read_text().split("\n")
            if line is not None and line.strip() != ""]


def maximum_temperature() -> float:
    """
    The maximum temperature depends on the model
    """
    if "deepseek" in g.MODEL_ID:
        return 1.0 # it really should be 2.0, but converse API doesn't allow it
    elif "claude" in g.MODEL_ID or "nova" in g.MODEL_ID:
        return 1.0
    else:
        raise Exception(f"port me: {g.MODEL_ID}")


def inference_cost(model_id: str,
                   usage: dict) -> float:
    """
    Return the cost of an inference in USD.

    On-demand pricing as of Dec 2025.
    """
    n_input_tokens, n_output_tokens, \
    n_cache_read_input_tokens, n_cache_write_input_tokens =\
        itemgetter("inputTokens", "outputTokens",
                   "cacheReadInputTokens", "cacheWriteInputTokens")(usage)
    if "claude-sonnet-4-5" in model_id or "sonnet-4-6" in model_id:
        cost_per_1k_input_tokens = 0.003
        cost_per_1k_output_tokens = 0.015
    elif "claude-opus-4-5" in model_id or "opus-4-6-v1" in model_id:
        cost_per_1k_input_tokens = 0.005
        cost_per_1k_output_tokens = 0.025
    elif "claude-haiku-4-5-20251001-v1" in model_id:
        cost_per_1k_input_tokens = 0.001
        cost_per_1k_output_tokens = 0.005
    elif "us.deepseek.r1-v1" in model_id:
        cost_per_1k_input_tokens = 0.00135
        cost_per_1k_output_tokens = 0.054
    elif "deepseek.v3.2" in model_id:
        cost_per_1k_input_tokens = 0.00062
        cost_per_1k_output_tokens = 0.00185
    elif "us.amazon.nova-premier-v1:0" in model_id:
        cost_per_1k_input_tokens = 0.0025
        cost_per_1k_output_tokens = 0.0125
    elif "us.amazon.nova-2-pro-preview-20251202-v1" in model_id:
        cost_per_1k_input_tokens = 0.000625
        cost_per_1k_output_tokens = 0.005
    else:
        print(f"inference_cost: Unknown model_id {model_id}, returning 0.0 cost")
        return 0.0
    return (n_input_tokens * (cost_per_1k_input_tokens / 1000.0) +
            n_output_tokens * (cost_per_1k_output_tokens / 1000.0))


@dataclass
class Statistics:
    """
    Keep track of statistics around the LLM inferences and cvc5 calls.
    """
    n_inferences: int = 0
    n_throttled_inferences: int = 0
    n_inference_exceptions: int = 0 # except for throttling exceptions
    total_inference_cost : float = 0.0
    n_cvc5_calls: int = 0
    n_malformed_theories: int = 0
    n_input_tokens: List[int] = field(default_factory=list)
    n_output_tokens: List[int] = field(default_factory=list)
    n_cache_read_input_tokens: List[int] = field(default_factory=list)
    n_cache_write_input_tokens: List[int] = field(default_factory=list)
    inference_durations: List[float] = field(default_factory=list)
    cvc5_durations: List[float] = field(default_factory=list)
    cvc5_results: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # kiro theory engine: credits kiro-cli reported (stream-json meteringUsage)
    # over n_kiro_sessions sessions. A session killed at its timeout reports
    # nothing, so kiro_credits is a lower bound when unmetered sessions > 0.
    kiro_credits: float = 0.0
    n_kiro_sessions: int = 0
    n_kiro_unmetered_sessions: int = 0

    def to_dict(self) -> dict:
        """
        Return as a dict
        """
        result = self.__dict__.copy()
        for k, v in result.items():
            if isinstance(v, defaultdict):
                result[k] = dict(v)
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "Statistics":
        """
        Create from a dict
        """
        result = cls()
        for k, v in d.items():
            if hasattr(result, k):
                setattr(result, k, v)
        result.cvc5_results = defaultdict(int, result.cvc5_results) # convert back to defaultdict
        return result

    def add_inference(self, usage: dict, model_id: Optional[str] = None):
        """
        Augment statistics from an AgentResult
        """
        print(f"di: usage: {json.dumps(usage,indent=2)}")
        self.n_inferences += 1
        self.n_input_tokens.append(usage.get("inputTokens", 0))
        self.n_output_tokens.append(usage.get("outputTokens", 0))
        self.n_cache_read_input_tokens.append(usage.get("cacheReadInputTokens", 0))
        self.n_cache_write_input_tokens.append(usage.get("cacheWriteInputTokens", 0))
        if model_id is not None:
            self.total_inference_cost += inference_cost(model_id, usage)

    def add_kiro_session(self, credits: Optional[float]):
        """Record one kiro-cli session; credits None if it reported no usage."""
        self.n_kiro_sessions += 1
        if credits is None:
            self.n_kiro_unmetered_sessions += 1
        else:
            self.kiro_credits += credits

    def add_cvc5_call(self, duration: float, result: Sat):
        """
        Augment statistics from a cvc5 call
        """
        # print(f"add_cvc5_call: duration {duration}, result {result}")
        self.n_cvc5_calls += 1
        self.cvc5_durations.append(duration)
        self.cvc5_results[str(result)] += 1
        # print(f"cvc5_results now: {self.cvc5_results}")


    def add_another(self, other: "Statistics"):
        """
        Augment statistics from another Statistics object
        """
        self.n_inferences += other.n_inferences
        self.n_malformed_theories += other.n_malformed_theories
        self.n_throttled_inferences += other.n_throttled_inferences
        self.n_inference_exceptions += other.n_inference_exceptions
        self.n_cvc5_calls += other.n_cvc5_calls
        self.total_inference_cost += other.total_inference_cost
        self.kiro_credits += other.kiro_credits
        self.n_kiro_sessions += other.n_kiro_sessions
        self.n_kiro_unmetered_sessions += other.n_kiro_unmetered_sessions
        self.n_input_tokens.extend(other.n_input_tokens)
        self.n_output_tokens.extend(other.n_output_tokens)
        self.n_cache_read_input_tokens.extend(other.n_cache_read_input_tokens)
        self.n_cache_write_input_tokens.extend(other.n_cache_write_input_tokens)
        self.inference_durations.extend(other.inference_durations)
        self.cvc5_durations.extend(other.cvc5_durations)
        for sat, count in other.cvc5_results.items():
            self.cvc5_results[sat] += count

    @classmethod
    def combine(cls, stats_list: List["Statistics"]) -> "Statistics":
        """
        Combine multiple Statistics objects into one.
        """
        combined = cls()
        for stats in stats_list:
            assert isinstance(stats, cls), f"Expected Statistics, got {type(stats)}"
            combined.add_another(stats)
        return combined

    def summarize_to_dict(self) -> dict:
        """
        Return a dict summarizing the statistics
        """
        cvc5_results_str = ", ".join(f"{k}: {v}" for k, v in self.cvc5_results.items())
        return dict(
            n_inferences=self.n_inferences,
            n_throttled_inferences=self.n_throttled_inferences,
            n_inference_exceptions=self.n_inference_exceptions,
            total_inference_cost=self.total_inference_cost,
            kiro_credits=self.kiro_credits,
            n_kiro_sessions=self.n_kiro_sessions,
            n_kiro_unmetered_sessions=self.n_kiro_unmetered_sessions,
            n_cvc5_calls=self.n_cvc5_calls,
            n_malformed_theories=self.n_malformed_theories,
            input_tokens=statistics_2_str(compute_statistics(self.n_input_tokens)),
            output_tokens=statistics_2_str(compute_statistics(self.n_output_tokens)),
            cache_read_input_tokens=statistics_2_str(compute_statistics(self.n_cache_read_input_tokens)),
            cache_write_input_tokens=statistics_2_str(compute_statistics(self.n_cache_write_input_tokens)),
            inference_durations=statistics_2_str(compute_statistics(self.inference_durations)),
            cvc5_durations=statistics_2_str(compute_statistics(self.cvc5_durations,
                                                               num_decimal_places=3)),
            cvc5_results=cvc5_results_str
        )

    def summary(self) -> str:
        """
        Return a short summary of the statistics
        """
        stats = self.summarize_to_dict()
        return dedent(f"""\
            Summary(
              {stats["n_inferences"]} inferences
              {stats["n_throttled_inferences"]} throttled inferences, {stats["n_inference_exceptions"]} exceptions
              total inference cost: ${stats["total_inference_cost"]:.2f}
              kiro: {stats["kiro_credits"]:.2f} credits over {stats["n_kiro_sessions"]} sessions ({stats["n_kiro_unmetered_sessions"]} unmetered)
              Input tokens: {stats["input_tokens"]}
              Output tokens: {stats["output_tokens"]}
              Cache read tokens: {stats["cache_read_input_tokens"]}
              Cache write tokens: {stats["cache_write_input_tokens"]}
              Inference durations: {stats["inference_durations"]}
              CVC5 durations: {stats["cvc5_durations"]}
              {stats['n_cvc5_calls']:,} calls to cvc5, {stats['n_malformed_theories']:,} were malformed
              CVC5 results: {stats['cvc5_results']}""")


def test_statistics_add():
    """ Test Statistics.add() """
    try:
        stats = Statistics()
        prompt = "What is 2+2?"
        _ = do_inference(prompt_prefix=prompt, stats=stats)
        agent = Agent(callback_handler=None)
        ar = agent(prompt)
        stats.add_inference(ar)
        stats.add_cvc5_call(1.23, Sat.SAT)
        print(f"stats: {stats.summary()}")
        assert stats.n_inferences >= 1
        assert len(stats.n_input_tokens) > 0
        assert all(map(lambda x: x > 0, stats.n_input_tokens))
        assert len(stats.n_output_tokens) > 0
        assert all(map(lambda x: x > 0, stats.n_output_tokens))
        assert len(stats.inference_durations) == 1
        assert stats.n_cvc5_calls == 1
        assert stats.n_malformed_theories == 0
        assert stats.cvc5_results[str(Sat.SAT)] == 1
        assert stats.cvc5_durations == [1.23]
    except Exception as ex:  # pylint: disable=broad-except
        if "AccessDeniedException" in str(ex):
            print("tsa: Access to APIs not setup, this is likely being run from github actions")
        else:
            print("tsa: Caught {ex}")
            traceback.print_exc()


def test_statistics_combine():
    """ Test Statistics.combine() """
    try:
        stats1 = Statistics()
        prompt = "What is 2+2?"
        _ = do_inference(prompt_prefix=prompt, stats=stats1)
        stats1.add_cvc5_call(1.23, Sat.SAT)
        print(f"stats1: {stats1.summary()}")

        stats2 = Statistics()
        prompt = "What is 2+2?"
        _ = do_inference(prompt_prefix=prompt, stats=stats2)
        stats2.add_cvc5_call(1.23, Sat.SAT)
        print(f"stats2: {stats2.summary()}")

        combo = Statistics.combine([stats1, stats2])
        print(f"combined: {combo.summary()}")

        assert combo.n_inferences == stats1.n_inferences + stats2.n_inferences
        assert combo.n_throttled_inferences == (stats1.n_throttled_inferences +
                                                stats2.n_throttled_inferences)
        assert combo.n_inference_exceptions == (stats1.n_inference_exceptions +
                                                stats2.n_inference_exceptions)
        assert combo.n_input_tokens == stats1.n_input_tokens + stats2.n_input_tokens
        assert combo.n_output_tokens == stats1.n_output_tokens + stats2.n_output_tokens
        assert combo.total_inference_cost == stats1.total_inference_cost
        assert combo.inference_durations == stats1.inference_durations + stats2.inference_durations
        assert combo.n_cvc5_calls == stats1.n_cvc5_calls + stats2.n_cvc5_calls
        for k in set(stats1.cvc5_results.keys()).union(stats2.cvc5_results.keys()):
            assert combo.cvc5_results[k] == (stats1.cvc5_results.get(k, 0) +
                                             stats2.cvc5_results.get(k, 0))

    except Exception as ex:  # pylint: disable=broad-except
        if "AccessDeniedException" in str(ex):
            print("tsa: Access to APIs not setup, this is likely being run from github actions")
        else:
            print(f"tsa: Caught {ex}")
            traceback.print_exc()


if __name__ == "__main__":
    pass
    # test_starmap_with_dupes()
    # test_statistics_add()
    # test_statistics_combine()

    # for code_block in extract_code_blocks(Path("foo").read_text()):
    #     print(f"code block: type={code_block['type']}, contents={code_block['contents']}")

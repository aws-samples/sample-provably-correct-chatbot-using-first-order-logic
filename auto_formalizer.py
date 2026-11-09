# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# pylint: disable=use-dict-literal, too-many-locals, no-else-return, too-many-instance-attributes, too-many-lines, multiple-imports, broad-exception-raised
# pylint: disable=not-context-manager # doesn't get context from otel.py
# pylint: disable=too-many-positional-arguments, too-many-arguments
# pylint: disable=wrong-import-position,import-error,multiple-imports, use-dict-literal
"""
The AutoFormalizer turns a set of natural-language sentences (from a text
document such as a Wikipedia page, or the entries of a FHIR bundle) into ONE
satisfiable and useful first-order theory, in a single unit of work:

1. Load every sentence of every subject, in document order.
2. Generate a test set for the whole document: question / expected-answer
   pairs, window by window and in parallel (generate_test_set).
3. Run ONE kiro-cli agent session over all the sentences
   (kiro_formalizer.kiro_create_valid_useful_theory). The agent writes the
   theory and tests it against the test set; the harness re-verifies the
   result independently and resumes the session with its report when it
   falls short.
4. Write the result to a run file (params document + one result document),
   which the chatbot loads with load_run_result.

It also holds the cvc5 / question-answering functions the chatbot uses.

Note that in here, I use "valid" and "satisfiable" interchangeably.
"""

import re, subprocess, uuid, os, json, math, datetime, threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from time import time
from typing import (List, Optional, Union, Tuple, Set, Dict, Callable)
from pathlib import Path
from textwrap import dedent
import itertools
from operator import itemgetter
import traceback

import boto3
import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory

from params import Params
import globals as g # don't do "from globals import X, Y" ,don't want another pointer
from prompts import get_system_prompt, get_prompt
from otel import start_span
from utils import (extract_code_blocks, extract_tag, strip,
                   remove_empty_lines, NEWLINE, HOME, get_matching_files,
                   print_multiline,
                   Sat, dump_dict, do_inference, set_max_num_files,
                   starmap_with_dupes, Statistics, get_global_stats,
                   load_s3_object, remove_comments, yaml_dump_all,
                   maximum_temperature)
from subjects import subject_list_type
from sentences import Document, Sentence
from questions_similarity import QuestionSimilarity
# Script assembly / output classification, shared with the kiro-cli checker
# (kiro_tools/cvc5_checker.py) so both judge a theory identically.
from cvc5_script import (
    find_and_remove_set_logic_spans, find_and_remove_set_option_spans,
    build_cvc5_script, classify_cvc5_output)


set_max_num_files(16*1024)
bedrock_agent_runtime = boto3.client("bedrock-agent-runtime",
                                     region_name="us-east-1")


# --- Build progress hook -----------------------------------------------------
# A cooperative, in-process callback the build driver (cdk/docker/store.py)
# installs to follow a build: the job's progress pill and the Job
# Introspection tab (cdk/docker/job_introspection.py) are fed from it.
# Best-effort: a callback that raises must never fail the build. Events
# (dicts, in order):
#   {"type": "start", "sentences": [str, ...],        what the agent is given
#        "raw_sentences": [str, ...],                  each sentence as written
#        "standalone_sentences": [str | None, ...]}    its decontextualized
#                                                      rewrite (None for FHIR)
#   {"type": "phase", "phase": "test_set" | "theory", "spent": {...}}
#        spent = the build's running totals: {"bedrock_dollars": float
#        (estimated from token counts), "kiro_credits": float}
#   {"type": "test_set", "qs_and_as": [[q, a], ...], "n_required": int}
#   {"type": "session_start", "session": n, "kind": k}  k = build | feedback
#                                                         | stall_resume
#   {"type": "session_end", "session": n, "kind": k, "rc": int,
#        "stalled": bool, "credits": float | None, "duration": secs,
#        "valid": bool, "n_correct": int, "correct_questions": [q, ...],
#        "theory": str | None}                        after re-verification
#   {"type": "end", "ok": bool, "theory": str | None, "n_correct": int,
#        "n_required": int, "correct_questions": [q, ...], "duration": secs,
#        "spent": {...}}
_progress_cb: Optional[Callable[[dict], None]] = None


def set_progress_cb(cb: Optional[Callable[[dict], None]]) -> None:
    """Install (or clear, with None) the build progress callback."""
    global _progress_cb  # pylint: disable=global-statement
    _progress_cb = cb


def spent_so_far(stats: Statistics) -> dict:
    """ A build's running spend, for the progress events. """
    return dict(bedrock_dollars=round(stats.total_inference_cost, 6),
                kiro_credits=round(stats.kiro_credits, 6))


def report_progress(event: dict) -> None:
    """Invoke the installed progress callback, swallowing any error."""
    cb = _progress_cb
    if cb is None:
        return
    try:
        cb(event)
    except Exception as ex:  # pylint: disable=broad-except
        print(f"progress callback failed (ignored): {ex}")



def simplify_theory(theory: str) -> str:
    """
    remove empty lines and strip whitespace from each line. This
    makes it easier to compare theories.
    """
    return "\n".join(line.strip() for line in remove_empty_lines(theory).split("\n"))


def test_simplify_theory():
    """ test simplify_theory() """
    theory = """\
        (set-logic ALL)
           (declare-sort Disease 0)

        (declare-sort Organ 0)
        """
    simplified = simplify_theory(theory)
    assert simplified == "(set-logic ALL)\n(declare-sort Disease 0)\n(declare-sort Organ 0)"


def num_qs_and_as(num_sentences: int) -> int:
    """
    Size of the test set for `num_sentences` sentences (at least 3, so the
    "allow two wrong" clamp still leaves one required answer).
    """
    per_sentence = g.p.fhir_qas_per_sentence if g.subject_type == "fhir" \
        else g.p.qas_per_sentence
    rv = max(3, int(num_sentences * per_sentence))
    print(f"num_qs_and_as {num_sentences} -> {rv}")
    return rv


def num_correct_answers_reqd(num_sentences: int, num_qs: int) -> int:
    """
    Correct answers the theory needs: ncar_ratio of the sentence count, but
    at least 1 and allowing for two errors in a test set of `num_qs`.
    """
    rv = math.ceil(num_sentences * g.p.ncar_ratio)
    rv = max(1, min(rv, num_qs - 2))
    print(f"num_correct_answers_reqd {num_sentences} {num_qs} -> {rv}")
    return rv


def is_this_answer_i_dont_know(answer: str, stats: Statistics) -> bool:
    """
    Is this answer somthing like "I don't know"? If so, we want
    to filter it out.
    """
    prompt = dedent(f"""\
        I'm going to give you a answer and I want you to tell
        me if this answer is something like "I don't know" or
        "I can't answer this question because ...". I want to know
        if this answer is a definitive answer to some question. 
        
        If this answer is some variant of "I don't know" then your response
        must be just the word "true", otherwise your response must be just
        the word "false".

        If the answer is something like "I choose not to answer this question"
        then this is a definitive answer so you must respond "false".

        Here is the answer that I want you to consider:

        ```
        {answer}
        ```
        """)
    print_multiline("itaidk prompt>", prompt)
    response = do_inference(prompt, stats)
    print_multiline("itaidk response> ", response)
    if "true" in response.lower():
        rv = True
    elif "false" in response.lower():
        rv = False
    else:
        rv = True
    print(f"itaidn ({answer}) -> {rv}")
    return rv


def generate_q_and_a(nat_lang_sentences: List[Sentence],
                     qs_to_exclude: List[str],
                     temperature: Optional[float],
                     stats: Statistics,
                     use_prompt_caching: bool = True) -> dict:
    """
    Given a set of sentences, use the LLM to generate a reasonable question
    and answer that shows understanding of the sentences.
    """
    assert isinstance(nat_lang_sentences[0], Sentence)
    with start_span("generate_q_and_a",
                    nat_lang_sentences=nat_lang_sentences,
                    qs_to_exclude=qs_to_exclude,
                    temperature=temperature) as span:
        print(f"generate_q_and_a: {len(qs_to_exclude):,} excluded questions, "
              f"temp={temperature}")
        print(f"{len(nat_lang_sentences):,} nat_lang_sentences:")
        for s in nat_lang_sentences:
            print(f"  {s}")
        if qs_to_exclude:
            prev_q_str = dedent(f"""\

            Here are some previous questions. Don't repeat one of these:
             - {f"{NEWLINE}             - ".join(qs_to_exclude)}
            """)
        else:
            prev_q_str = ""
        document = "\n".join(Document.make_chunk_standalone(nat_lang_sentences))
        print_multiline("gqaa: document> ", document)
        print(f"gqaa: g.p: {g.p}")
        prompt_id = "generate_q_and_a" if len(g.p.subject_ids) == 1 else \
                    "generate_q_and_a_multiple_subjects"
        print(f"prompt_id: {prompt_id} g.p.subject_ids: {g.p.subject_ids}")
        if use_prompt_caching:
            prompt_prefix = get_prompt(prompt_id).format(
                document=document,
                prev_q_str=""
            )
            print_multiline("q&a prompt prefix> ", prompt_prefix)
            prompt_suffix = prev_q_str
            response = do_inference(
                system_prompt=get_system_prompt("generate_q_and_a"),
                prompt_prefix=prompt_prefix,
                prompt_suffix=prompt_suffix,
                stats=stats,
                temperature=temperature)
            prompt = prompt_prefix + prompt_suffix # for logging
        else:
            prompt = get_prompt(prompt_id).format(
                document=document,
                prev_q_str=prev_q_str
            )
            print_multiline("q&a prompt> ", prompt)
            response = do_inference(
                prompt, stats,
                temperature=temperature,
                system_prompt=get_system_prompt("generate_q_and_a"))
        print_multiline("q&a response> ", response)
        question = extract_tag(response, "question")
        answer = extract_tag(response, "answer")
        print(f"gqaa: q: {question} a: {answer}")
        span.update(output=dict(
            question=question,
            answer=answer
        ))
        rv = dict(
            nat_lang_sentences=nat_lang_sentences,
            question=question,
            temperature=temperature,
            answer=answer,
            prompt=prompt,
            response=response)
        # print("generate_q_and_a -> ")
        # dump_dict(rv)
        return rv


def convert_question_to_logical_form(nat_lang_question: str,
                                     theory: str,
                                     stats: Statistics,
                                     model_id: Optional[str] = None) -> dict:
    """
    Given a natural language question, use the LLM to turn it into a
    theory.
    """
    print(f"cq2lf: convert_question_to_logical_form {nat_lang_question}")
    assert isinstance(theory, str)
    model_id = model_id or g.FORMALIZATION_MODEL_ID
    # print_multiline("cq2lf: theory> ", theory)
    result_form: Optional[str] = None
    response: Optional[str] = None
    with start_span("convert_question_to_logical_form",
                    nat_lang_question=nat_lang_question,
                    theory=theory) as span:
        start_time = time()
        for iteration in itertools.count():
            # Bound the retry loop: the LLM may never emit a parseable
            # ```result``` block, in which case get_best_result_code_block()
            # keeps returning None and this loop would spin forever (the
            # ~8.5h di:/gbrcb: wedge). Give up cleanly on either cap so the
            # question fails as "no logical form" instead of hanging the pool.
            if iteration >= g.p.max_cq2lf_iterations:
                print(f"cq2lf: reached max_cq2lf_iterations "
                      f"({g.p.max_cq2lf_iterations}); giving up on this question")
                result_form = None
                break
            if (time() - start_time) >= g.p.max_cq2lf_seconds:
                print(f"cq2lf: reached max_cq2lf_seconds "
                      f"({g.p.max_cq2lf_seconds:.0f}s, iter {iteration:,}); "
                      f"giving up on this question")
                result_form = None
                break
            prompt = get_prompt("convert_question_to_logical_form",
                nat_lang_question=nat_lang_question,
                theory=theory
            )
            response = do_inference(
                prompt, stats,
                system_prompt="You are an expert in first-order logic, "
                              "SMT-LIBv2, and the biomedical literature",
                temperature=maximum_temperature() if iteration > 0 else None,
                model_id=model_id)
            # print_multiline("cq2lf:response> ", response)
            result_form = get_best_result_code_block(response)
            if result_form is not None:
                break
        rv = dict(
            nat_lang_question=nat_lang_question,
            theory=remove_empty_lines(theory),
            response=response,
            logical_form=result_form)
        # print("convert_question_to_logical_form -> ")
        # dump_dict(rv)
        span.update(output=dict(
            logical_form=result_form,
            response=response
        ))
        return rv


def generate_uniq_theory_path(theory_dir: Path) -> Path:
    """
    Generate a random Path for a theory. We need this as
    we are running cvc5 as a separate process.
    """
    theory_dir.mkdir(parents=True, exist_ok=True)
    while True:
        file_name = f"theory-{str(uuid.uuid4())[:6]}"
        path = theory_dir / file_name
        if not path.exists():
            return path


def compute_validity_from_cvc5_output(
        stdout: str,
        stderr: str) -> Tuple[Optional[Sat], Optional[str]]:
    """
    Given the stdout from cvc5, compute the validity. Also, return the (get-model) output, if any.
    """
    validity_name, get_model_output = classify_cvc5_output(stdout, stderr)
    validity: Sat = Sat[validity_name]
    stdout = stdout.strip()
    if validity == Sat.SAT and get_model_output is None:
        print("cvfco: warning: failed to parse get-model_output:")
        print_multiline("cvfco: unparseable> ", stdout)
    print(f"cvfco: returning {validity}")
    # print_multiline("cvfco: get_model_output> ", get_model_output)
    return validity, get_model_output


def test_compute_validity_from_cvc5_output():
    """ test validity check """
    stdout = dedent("""\
        sat
        (
        ; cardinality of Activity is 1
        ; rep: (as @Activity_0 Activity)
        ; cardinality of Disease is 1
        ; rep: (as @Disease_0 Disease)
        ; cardinality of Food is 7
        ; rep: (as @Food_0 Food)
        ; rep: (as @Food_1 Food)
        ; rep: (as @Food_2 Food)
        ; rep: (as @Food_3 Food)
        ; rep: (as @Food_4 Food)
        ; rep: (as @Food_5 Food)
        ; rep: (as @Food_6 Food)
        ; cardinality of Fat is 2
        ; rep: (as @Fat_0 Fat)
        ; rep: (as @Fat_1 Fat)
        ; cardinality of Diet is 1
        ; rep: (as @Diet_0 Diet)
        ; cardinality of PreventionMethod is 2
        ; rep: (as @PreventionMethod_0 PreventionMethod)
        ; rep: (as @PreventionMethod_1 PreventionMethod)
        (define-fun is-type2-diabetes ((_arg_1 Disease)) Bool true)
        (define-fun is-physical-activity ((_arg_1 Activity)) Bool true)
        (define-fun minutes-per-day ((_arg_1 Activity)) Int 91)
        (define-fun reduces-risk-of ((_arg_1 PreventionMethod) (_arg_2 Disease)) Bool true)
        (define-fun risk-reduction-percent ((_arg_1 PreventionMethod) (_arg_2 Disease)) Int 28)
        (define-fun is-dietary-change ((_arg_1 PreventionMethod)) Bool true)
        (define-fun is-rich-in ((_arg_1 Diet) (_arg_2 Food)) Bool true)
        (define-fun is-whole-grain ((_arg_1 Food)) Bool true)
        (define-fun is-fiber ((_arg_1 Food)) Bool true)
        (define-fun is-good-fat ((_arg_1 Fat)) Bool true)
        (define-fun is-polyunsaturated-fat ((_arg_1 Fat)) Bool true)
        (define-fun is-saturated-fat ((_arg_1 Fat)) Bool true)
        (define-fun found-in ((_arg_1 Fat) (_arg_2 Food)) Bool true)
        (define-fun is-sugary-beverage ((_arg_1 Food)) Bool true)
        (define-fun is-red-meat ((_arg_1 Food)) Bool true)
        (define-fun contains-fat-type ((_arg_1 Food) (_arg_2 Fat)) Bool true)
        (define-fun helps-prevent ((_arg_1 PreventionMethod) (_arg_2 Disease)) Bool true)
        (define-fun involves-limiting ((_arg_1 PreventionMethod) (_arg_2 Food)) Bool true)
        (define-fun involves-maintaining ((_arg_1 PreventionMethod) (_arg_2 Diet)) Bool true)
        (define-fun involves-choosing ((_arg_1 PreventionMethod) (_arg_2 Fat)) Bool true)
        (define-fun high-physical-activity () Activity (as @Activity_0 Activity))
        (define-fun type2-diabetes () Disease (as @Disease_0 Disease))
        (define-fun whole-grains () Food (as @Food_0 Food))
        (define-fun fiber () Food (as @Food_1 Food))
        (define-fun nuts () Food (as @Food_2 Food))
        (define-fun vegetable-oils () Food (as @Food_3 Food))
        (define-fun fish () Food (as @Food_4 Food))
        (define-fun sugary-beverages () Food (as @Food_5 Food))
        (define-fun red-meat () Food (as @Food_6 Food))
        (define-fun polyunsaturated-fat () Fat (as @Fat_0 Fat))
        (define-fun saturated-fat () Fat (as @Fat_1 Fat))
        (define-fun high-activity-prevention () PreventionMethod (as @PreventionMethod_0 PreventionMethod))
        (define-fun dietary-prevention () PreventionMethod (as @PreventionMethod_1 PreventionMethod))
        (define-fun whole-grain-fiber-diet () Diet (as @Diet_0 Diet))
        )
    """)
    stderr = """
    """
    satisfiability, get_model_output = compute_validity_from_cvc5_output(stdout, stderr)
    assert satisfiability == Sat.SAT
    assert get_model_output.strip().startswith("; cardinality of Activity is 1")
    assert get_model_output.strip()\
        .endswith("(define-fun whole-grain-fiber-diet () Diet (as @Diet_0 Diet))")


def cvc5_script_is_well_formed(script: str) -> Tuple[bool, List[str]]:
    """
    Make sure that (set-logic) is at the top, (check-sat)
    is at the bottom, etc.
    """
    script = remove_comments(script)

    def find_mentions(regex: str) -> List[int]:
        return [match.span()[0] for match in re.finditer(regex, script)]

    set_logics = find_mentions(r"\(set-logic\s+(\w+)\)")
    check_sats = find_mentions(r"\(check-sat\s*\)")
    get_models = find_mentions(r"\(get-model\s*\)")
    declare_sorts = find_mentions(r"\(declare-sort\s.+\)")
    declare_consts = find_mentions(r"\(declare-consts\s.+\)")
    asserts = find_mentions(r"\(assert\s.+\)")
    get_values = find_mentions(r"\(get-value\s+(.+)\)")
    regular_cmds = declare_sorts + declare_consts + asserts

    checks: List[bool] = []
    if set_logics and regular_cmds + check_sats + get_models:
        checks.append((max(set_logics) < min(regular_cmds + check_sats + get_models),
                      "set-logic must come first"))
    if declare_sorts + declare_consts + asserts and check_sats:
        checks.append((max(declare_sorts + declare_consts + asserts) < min(check_sats),
                      "check-sat must come after regular commands"))
    if check_sats and get_models:
        checks.append((max(check_sats) < min(get_models),
                      "check-sat must be before get-model"))
    if check_sats and get_models + get_values:
        checks.append((max(check_sats) < min(get_models + get_values),
                      "get-model and get-values must come after check-sat"))
    error_mesgs = [error_mesg for check, error_mesg in checks if not check]
    return len(error_mesgs) == 0, error_mesgs


def test_cvc5_script_is_well_formed():
    """ Test both positive and negative. """
    script = ""
    assert cvc5_script_is_well_formed(script)[0] is True
    script = "(set-logic ALL) (check-sat)"
    assert cvc5_script_is_well_formed(script)[0] is True
    script = """(set-logic ALL)
        (assert (=> (is-resolved gingivitis) (date-before date-2008-03-26 date-2008-04-09)))
        (check-sat)"""
    assert cvc5_script_is_well_formed(script)[0] is True

    script = "(check-sat)(set-logic ALL)"
    assert cvc5_script_is_well_formed(script)[0] is False

    script = """
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
        (check-sat)"""
    assert cvc5_script_is_well_formed(script)[0] is True


def run_cvc5(theory: str,
             question: Optional[str],
             stats: Statistics,
             print_out_theories: bool = True,
             timeout: Optional[float] = None) -> dict:
    """
    Run cvc5 as a seperate process, get the satisifiablity
    and (if sat) the get-model output.

    `timeout` (seconds) bounds the cvc5 process; on expiry the result has
    validity None (the generic-failure path). None means no limit, the
    historical behavior.
    """
    assert isinstance(theory, str), f"{type(theory)}: {theory}"
    with start_span("run_cvc5",
                    theory=theory,
                    question=question) as span:
        validity: Optional[Sat] = None
        # Build from the ORIGINAL question so its set-logic/set-option feed the
        # merged preamble; then keep the stripped form for the return dict, as
        # the previous inline version did.
        augmented_theory = build_cvc5_script(theory, question)
        if question is not None:
            question, _ = find_and_remove_set_logic_spans(question)
            question, _ = find_and_remove_set_option_spans(question)
        if print_out_theories:
            print_multiline("rc:aug_theory> ", augmented_theory)
        well_formed, error_mesgs = cvc5_script_is_well_formed(augmented_theory)
        if not well_formed:
            print(f"rc: WARNING: script is malformed: {error_mesgs}")
            stats.n_malformed_theories += 1
        theory_path = generate_uniq_theory_path(g.THEORIES_DIR)
        theory_path.write_text(augmented_theory)
        start_time = time()

        def return_fn(theory: str,
                      question: str,
                      augmented_theory: str,
                      validity: Optional[Sat],
                      get_model_output: Optional[str],
                      stdout: Optional[str],
                      stderr: Optional[str]) -> dict:
            span.update(output=dict(
                validity=str(validity),
                get_model_output=get_model_output,
                stdout=stdout,
                stderr=stderr,
                augmented_theory=augmented_theory,
            ))
            try:
                theory_path.unlink()
            except Exception as ex:  # pylint: disable=broad-except
                print(f"Caught {ex}")
                traceback.print_exc()
            duration = time() - start_time
            stats.add_cvc5_call(duration, validity)
            return dict(
                theory=theory,
                question=question,
                augmented_theory=augmented_theory,
                validity=validity,
                get_model_output=get_model_output,
                stdout=stdout,
                stderr=stderr)

        try:
            result = subprocess.run(
                [os.environ.get("CVC5_PATH", f"{HOME}/bin/cvc5"),
                 "--produce-models",
                 "--incremental",
                 str(theory_path)],
                capture_output=True,
                text=True,  # Decode stdout and stderr as text using default encoding
                check=True,   # Raise CalledProcessError if the command returns a non-zero exit code
                timeout=timeout)
            print(f"rc: command executed successfully; exit code: {result.returncode}")
            print_multiline("rc: result.stdout> ", result.stdout)
            print_multiline("rc: result.stderr> ", result.stderr)
            validity, get_model_output = compute_validity_from_cvc5_output(
                                            result.stdout,
                                            result.stderr)
            print(f"rc: validity: {validity}")
            # print(f"rc: get_model_output: {get_model_output}")
            return return_fn(
                theory=remove_empty_lines(theory),
                question=question,
                augmented_theory=remove_empty_lines(augmented_theory),
                validity=validity,
                get_model_output=remove_empty_lines(get_model_output),
                stdout=strip(result.stdout),
                stderr=strip(result.stderr)
            )
        except subprocess.CalledProcessError as e:
            print(f"rc: command failed with exit code {e.returncode}")
            print_multiline("rc:stdout> ", e.stdout)
            print_multiline("rc:stderr> ", e.stderr)
            return return_fn(
                theory=remove_empty_lines(theory),
                question=question,
                augmented_theory=remove_empty_lines(augmented_theory),
                validity=Sat.UNSAT,
                get_model_output=None,
                stdout=e.stdout,
                stderr=e.stderr
            )
        except FileNotFoundError as ex:
            print(f"rc: error: Command not found: {ex}")
            traceback.print_exc()
            return return_fn(
                theory=remove_empty_lines(theory),
                question=question,
                augmented_theory=remove_empty_lines(augmented_theory),
                validity=None,
                get_model_output=None,
                stdout=None,
                stderr=None
            )
        except Exception as e:  # pylint: disable=broad-except
            print(f"rc: an error occurred: {e}")
            traceback.print_exc()
            return return_fn(
                theory=remove_empty_lines(theory),
                question=question,
                augmented_theory=remove_empty_lines(augmented_theory),
                validity=None,
                get_model_output=None,
                stdout=None,
                stderr=None)


def test_cvc5():
    """ Simple test just to see if the plumbing works """
    theory = dedent("""\
        (declare-const |Hello World!| Bool)
        (assert (not |Hello World!|))
        """)
    result = run_cvc5(theory, None, get_global_stats())
    assert result["validity"] == Sat.SAT
    # Note that cvc5 adds the (get-model):
    assert result["get_model_output"] == "(define-fun |Hello World!| () Bool false)"


def get_best_result_code_block(response: str,
                               prev_theories: Set[str] = None,
                               ) -> Optional[str]:
    """
    Given a response from the LLM with multiple ```...```
    code blocks, extract and return the best one.

    In general we prefer (a) the longest one, and (b) ones that are tagged
    with ```result``` rather than ```smt``` or untagged.

    But, we don't want something super short, as that's likely a snippet, not 
    a complete theory.
    """
    print("gbrcb: get_best_result_code_block")
    # print_multiline("gbrcb: response> ", response)
    if prev_theories is None:
        prev_theories = set()
    theories = extract_code_blocks(response)
    for i, theory in enumerate(theories):
        print(f"gbrcb: theory[{i}]: {theory}")
    result_theories = [t["contents"] for t in theories if t["type"] == "result"]
    theory = None
    if result_theories:
        unseen_result_theories = list(set(result_theories) - prev_theories)
        if unseen_result_theories:
            print("gbrcb: found unseen result theories, using those")
            theory = sorted(unseen_result_theories, key=len)[-1]
        else:
            print("gbrcb: all result theories have been seen before, failing")
            theory = None
    if theory is None:
        print("gbrcb: failed to extract")
    else:
        assert isinstance(theory, str)
        theory = simplify_theory(theory)
        print(f"gbrcb: returning theory ({len(theory.split("\n")):,} lines): {theory[:100]} [...]")
    return theory


def get_model_output_2_nat_lang(valid_theory: str,
                                question: str,
                                q_logical_form: str,
                                get_model_output: str,
                                stats: Statistics,
                                model_id: Optional[str] = None) -> str:
    """
    Given the output from (get-model ...), use the LLM to convert
    it back into natural language (an answer to the question).
    """
    model_id = model_id or g.FORMALIZATION_MODEL_ID
    with start_span("get_model_output_2_nat_lang",
                    valid_theory=valid_theory,
                    question=question,
                    q_logical_form=q_logical_form,
                    get_model_output=get_model_output) as span:
        prompt = get_prompt("get_model_output_2_nat_lang",
            valid_theory=valid_theory,
            question=question,
            q_logical_form=q_logical_form,
            get_model_output=get_model_output)
        response = do_inference(
            prompt, stats,
            system_prompt="You are an expert in both first-order logic and cvc5",
            model_id=model_id)
        # print_multiline("response> ", response)
        answer = extract_tag(response, "answer")
        # print(f"gmo2nl: answer: {answer}")
        span.update(output=dict(
            answer=answer,
            response=response
        ))
        rv = dict(
            theory=valid_theory,
            question=question,
            q_logical_form=q_logical_form,
            get_model_output=get_model_output,
            response=response,
            answer=answer)
        # print("get_model_output_2_nat_lang -> ...")
        # dump_dict(rv)
        return rv


def llm_as_judge(q: str, answer1: str, answer2: str,
                 stats: Statistics) -> dict:
    """ Return True if the two answers are euivalent """
    with start_span("llm_as_judge",
                    q=q,
                    answer1=answer1,
                    answer2=answer2) as span:
        print(f"laj: q:       {q}")
        print(f"laj: answer1: {answer1}")
        print(f"laj: answer2: {answer2}")
        prompt = dedent(f"""\
            Given this question:

            ```question
            {q}
            ```

            and these two answers to that question:

            ```answer-1
            {answer1}
            ```

            and 

            ```answer-2
            {answer2}
            ```

            Are these two answers the same? Are they semantically equivalent?

            Your response should be either "true" or "false" and should be
            enclosed in <result>...</result> tags.""")
        # print_multiline("laj:prompt> ", prompt)
        response = do_inference(
            prompt, stats,
            system_prompt="You are an expert in the biomedical literature")
        # print_multiline("laj:response> ", response)
        result_str = extract_tag(response, "result")
        print(f"laj: result_str {result_str}")
        if result_str is None:
            result_str = "false"
        result_str = result_str.strip().lower()
        are_same = result_str != "false"
        span.update(output=dict(
            are_same=are_same,
            result_str=result_str,
            response=response
        ))
        rv = dict(
            are_same=are_same,
            question=q,
            answer1=answer1,
            answer2=answer2,
            response=response,
            result_str=result_str
        )
        print("llm_as_judge ->")
        dump_dict(rv)
        return rv


# --- The build: one unit over the whole document -----------------------------
def generate_uniq_run_file(subject_ids: List[str]) -> Path:
    """
    A new run file, numbered one more than the highest existing one, so the
    most recent run is easy to find: runs/run-<subjects>-<NNNN>.yaml.
    """
    g.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    existing_files = get_matching_files(g.RUNS_DIR, "run*.yaml")

    def extract_run_num(path: Path) -> int:
        m = re.match(r"^run(?:.*)-([0-9]+)(?:-.*)?\.yaml$", str(path.name))
        if m:
            return int(m.group(1))
        print(f"WARNING: ignoring {path}")
        return -1

    existing_run_nums = list(map(extract_run_num, existing_files))
    next_run_num = max(existing_run_nums) + 1 if len(existing_run_nums) > 0 else 0
    runs_path = g.RUNS_DIR / f"run-{'-'.join(subject_ids)}-{next_run_num:0>4}.yaml"
    assert not runs_path.exists(), runs_path
    return runs_path


def save_run_file(run_file: Path, result: dict):
    """
    Save a run file: a two-document YAML stream, the build's Params and then
    its result (sentences, theory, all_qs_and_as,
    correctly_answered_questions, n_correct_answers_reqd).
    """
    with open(run_file, "w", encoding="utf8") as f:
        yaml_dump_all([g.p.to_dict(), result], f)
    print(f"Saved the result to {run_file}")


def load_run_file(run_file: Path) -> Tuple[Params, dict]:
    """ Load a run file written by save_run_file: (Params, result). """
    with open(run_file, encoding="utf8") as f:
        p, result = list(yaml.safe_load_all(f))
    return Params.from_dict(p), result


def make_abs_run_file_path(run_file: Path) -> Path:
    """
    Given a run file path, make it absolute based on g.RUNS_DIR.
    """
    if run_file.exists():
        return run_file
    candidates = [Path(g.RUNS_DIR / str(run_file)),
                  # the parent, too; this works for the chatbot:
                  Path(g.RUNS_DIR / run_file.parent.name / run_file.name)]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Cannot find run file: {run_file}")


def load_run_result(run_file: Union[Path, str]) -> dict:
    """
    Load a build's result from its run file (a Path, resolved against
    g.RUNS_DIR if relative, or an s3:// URI). Returns p (Params or None),
    theory, suggested_questions (the questions the theory answered
    correctly) and sentences.
    """
    if isinstance(run_file, Path):
        with open(make_abs_run_file_path(run_file), encoding="utf8") as f:
            blobs = list(yaml.safe_load_all(f))
    else:
        blobs = list(yaml.safe_load_all(StringIO(load_s3_object(run_file))))
    p, result = blobs
    correct = list(dict.fromkeys(result.get("correctly_answered_questions") or []))
    expected = {qa[0]: qa[1] for qa in result.get("all_qs_and_as") or []
                if isinstance(qa, (list, tuple)) and len(qa) >= 2}
    return dict(
        p=Params.from_dict(p) if p else None,
        theory=result["theory"],
        suggested_questions=correct,
        # Each verified question with the expected answer the theory matched.
        verified_qs_and_as=[[q, expected[q]] for q in correct if q in expected],
        sentences=result["sentences"])


def load_all_sentences(stats: Statistics) -> List[Sentence]:
    """
    Every sentence of every subject in g.p.subject_ids, in document order,
    capped at g.p.max_sentences when that is set.
    """
    sentences: List[Sentence] = []
    for document in load_all_documents(stats):
        doc_sentences = document.sentences()
        print(f"las: {document} has {len(doc_sentences):,} sentences")
        sentences.extend(doc_sentences)
    if g.p.max_sentences is not None and len(sentences) > g.p.max_sentences:
        print(f"las: using the first {g.p.max_sentences:,} of "
              f"{len(sentences):,} sentences")
        sentences = sentences[:g.p.max_sentences]
    return sentences


def sentence_windows(sentences: List[Sentence],
                     window: int) -> List[List[Sentence]]:
    """
    Consecutive, non-overlapping windows of at most `window` sentences. A
    short final window is merged into the previous one, so a window always
    has enough material to ask questions about.

    >>> [len(w) for w in sentence_windows(list(range(24)), 10)]
    [10, 14]
    >>> [len(w) for w in sentence_windows(list(range(25)), 10)]
    [10, 10, 5]
    >>> [len(w) for w in sentence_windows(list(range(20)), 10)]
    [10, 10]
    >>> [len(w) for w in sentence_windows(list(range(4)), 10)]
    [4]
    >>> sentence_windows([], 10)
    []
    """
    window = max(1, window)
    windows = [sentences[i:i + window] for i in range(0, len(sentences), window)]
    if len(windows) > 1 and len(windows[-1]) < max(2, window // 2):
        tail = windows.pop()
        windows[-1] = windows[-1] + tail
    return windows


def split_quota(total: int, sizes: List[int]) -> List[int]:
    """
    Split `total` questions across windows in proportion to their sizes
    (largest remainder), every window getting at least one.

    >>> split_quota(25, [10, 15])
    [10, 15]
    >>> split_quota(5, [10, 10, 10])
    [2, 2, 1]
    >>> split_quota(2, [10, 10, 10])
    [1, 1, 1]
    """
    if not sizes:
        return []
    n = sum(sizes)
    exact = [total * s / n for s in sizes]
    quota = [max(1, math.floor(x)) for x in exact]
    by_remainder = sorted(range(len(sizes)), key=lambda i: exact[i] - math.floor(exact[i]),
                          reverse=True)
    for i in by_remainder:
        if sum(quota) >= total:
            break
        quota[i] += 1
    return quota


# A window that adds no new question in this many rounds in a row has run out
# of distinct questions; it is not asked again.
TEST_SET_EXHAUSTED_ROUNDS = 2


def generate_test_set(sentences: List[Sentence],
                      num_qs: int,
                      stats: Statistics) -> List[Tuple[str, str]]:
    """
    The test set for the whole document: `num_qs` unique, semantically
    distinct question / expected-answer pairs.

    The document is split into windows of g.p.qa_window_sentences
    consecutive sentences, so the questions cover all of it and every prompt
    stays small. Generation runs in rounds. Each round, every window still
    in play generates its share of the questions still missing
    (gen_diverse_set_of_qs_and_as, at most g.p.qa_pool_size windows at a
    time), told about every question it has produced before so it does not
    repeat one. Each new question is kept only if it is not too similar to
    any question already kept, from any window. Rounds continue until the
    test set reaches `num_qs`.

    The one exception: a window that adds nothing in
    TEST_SET_EXHAUSTED_ROUNDS rounds in a row has no distinct questions left
    and drops out, and when every window has, the test set is as large as
    this document allows. It is then returned short, with a warning, instead
    of looping forever.
    """
    # Windows never straddle two documents (a window is made standalone as
    # one chunk).
    windows = [w for run in Document.document_runs(sentences)
               for w in sentence_windows(run, g.p.qa_window_sentences)]
    print(f"gts: {num_qs:,} questions over {len(windows):,} windows")

    stats_lock = threading.Lock()
    similarity = QuestionSimilarity()
    seen: Set[str] = set()
    kept: List[List[Tuple[str, str]]] = [[] for _ in windows]   # per window
    tried: List[List[Tuple[str, str]]] = [[] for _ in windows]  # all it produced
    idle_rounds = [0] * len(windows)

    def one_window(i: int, quota: int) -> List[Tuple[str, str]]:
        """New (q, a) pairs from window i, excluding all it produced before."""
        window_stats = Statistics()
        before = {q for q, _ in tried[i]}
        try:
            out = gen_diverse_set_of_qs_and_as(
                sentences=windows[i], num_new_questions_reqd=quota,
                existing_qs_and_as=list(tried[i]),
                stats=window_stats, max_attempts=4 * quota + 4)
        finally:
            with stats_lock:
                stats.add_another(window_stats)
        return [(q, a) for q, a in out if q not in before]

    def total() -> int:
        return sum(len(k) for k in kept)

    with start_span("generate_test_set", num_qs=num_qs,
                    num_windows=len(windows)) as span, \
            ThreadPoolExecutor(max_workers=max(1, g.p.qa_pool_size)) as pool:
        rounds = 0
        while total() < num_qs:
            active = [i for i in range(len(windows))
                      if idle_rounds[i] < TEST_SET_EXHAUSTED_ROUNDS]
            if not active:
                print(f"gts: WARNING: every window has run out of distinct "
                      f"questions; the test set has {total():,} of the "
                      f"{num_qs:,} wanted")
                break
            rounds += 1
            missing = num_qs - total()
            quotas = split_quota(missing, [len(windows[i]) for i in active])
            print(f"gts: round {rounds}: {missing:,} questions missing; "
                  f"asking {len(active):,} windows")
            results = list(pool.map(one_window, active, quotas))
            # Accept in document order, so earlier windows keep precedence.
            for i, new in zip(active, results):
                added = 0
                for q, a in new:
                    tried[i].append((q, a))
                    if q in seen:
                        continue
                    is_diverse, embedding, _ = \
                        similarity.question_is_sufficiently_diverse(q)
                    if not is_diverse:
                        print(f"gts: dropping a question too similar to an "
                              f"earlier one: {q}")
                        continue
                    similarity.add_question(q, embedding)
                    seen.add(q)
                    kept[i].append((q, a))
                    added += 1
                idle_rounds[i] = 0 if added else idle_rounds[i] + 1
            print(f"gts: round {rounds}: test set has {total():,}/{num_qs:,}")
        # Document order: window by window. A round can overshoot the target
        # (a window's share is rounded up), so trim to exactly num_qs.
        test_set = [qa for k in kept for qa in k][:num_qs]
        print(f"gts: test set has {len(test_set):,} questions "
              f"after {rounds} round(s)")
        span.update(output=dict(num_qs=len(test_set), rounds=rounds))
        return test_set

def formalize_document() -> dict:
    """
    Build ONE valid and useful theory of every sentence of the subjects in
    g.p.subject_ids, and save it to a new run file. Returns the run file
    path (runs_path), whether a verified theory was found (ok), and counts
    for the build summary.
    """
    # Imported here: kiro_formalizer imports this module lazily too.
    from kiro_formalizer import kiro_create_valid_useful_theory  # pylint: disable=import-outside-toplevel
    g.subject_type = subject_list_type(g.p.subject_ids)
    stats = Statistics()
    start = time()
    with start_span("formalize_document", p=g.p.to_dict()) as span:
        sentences = load_all_sentences(stats)
        if not sentences:
            raise ValueError(f"no sentences for subjects {g.p.subject_ids}")
        report_progress(dict(
            type="start",
            sentences=Document.make_standalone_lines(sentences),
            raw_sentences=[s.original for s in sentences],
            # Text and MDM sentences have an LLM rewrite; FHIR entries get
            # their patient context attached structurally instead.
            standalone_sentences=[getattr(s, "standalone", None) for s in sentences]))
        runs_path = generate_uniq_run_file(g.p.subject_ids)
        print(f"fd: {len(sentences):,} sentences; writing to {runs_path}")

        report_progress(dict(type="phase", phase="test_set", spent=spent_so_far(stats)))
        qs_and_as = generate_test_set(sentences, num_qs_and_as(len(sentences)),
                                      stats)
        if len(qs_and_as) < 3:
            raise ValueError(f"only {len(qs_and_as)} test questions could be "
                             "generated; at least 3 are needed")
        n_required = num_correct_answers_reqd(len(sentences), len(qs_and_as))
        report_progress(dict(type="test_set",
                             qs_and_as=[list(qa) for qa in qs_and_as],
                             n_required=n_required))

        report_progress(dict(type="phase", phase="theory", spent=spent_so_far(stats)))
        vu = kiro_create_valid_useful_theory(
            sentences=sentences, qs_and_as=qs_and_as,
            n_correct_answers_reqd=n_required, stats=stats,
            on_event=report_progress)
        result = dict(sentences=sentences,
                      n_correct_answers_reqd=vu["n_correct_answers_reqd"],
                      correctly_answered_questions=vu["correctly_answered_questions"],
                      all_qs_and_as=[list(qa) for qa in qs_and_as],
                      theory=vu["theory"])
        save_run_file(runs_path, result)
        ok = vu["theory"] is not None
        duration = time() - start
        report_progress(dict(type="end", ok=ok, theory=vu["theory"],
                             n_correct=len(vu["correctly_answered_questions"]),
                             n_required=vu["n_correct_answers_reqd"],
                             correct_questions=list(vu["correctly_answered_questions"]),
                             duration=duration, spent=spent_so_far(stats)))
        print(f"Statistics: {stats.summary()}")
        summary = dict(runs_path=str(runs_path), ok=ok,
                       num_sentences=len(sentences), num_qs=len(qs_and_as),
                       n_required=vu["n_correct_answers_reqd"],
                       n_correct=len(vu["correctly_answered_questions"]),
                       duration_in_hours=duration / 3600.0)
        span.update(output=dict(summary, stats=stats.summarize_to_dict()))
        span.result("verified theory" if ok else "no verified theory")
        return summary



def answer_q(theory: Optional[str],
             question: str,
             generate_actual_answer: bool = True,
             model_id: Optional[str] = None,
             stats: Statistics = None,
             do_use_theorem_prover: bool = True,
             print_out_theories: bool = True) -> Optional[str]:
    """
    if do_use_theorem_prover is True, use the theorem prover to answer the question.
    Otherwise, use the LLM directly.

    If given a theory and a question, try to answer it.
    If generate_actual_answer is False, just return whether
    we can answer it or not; this is useful for coverage testing.

    Return None if the theorem prover or LLM can't answer the question.
    """
    assert stats is not None
    model_id = model_id or g.MODEL_ID
    with start_span(f"aq: answer_q {question}"):
        if do_use_theorem_prover:
            assert theory is not None, "theory must be provided if do_use_theorem_prover is True"
            assert isinstance(theory, str)
            # print_multiline("aq: theory> ", theory)
            lf_result = convert_question_to_logical_form(question, theory, stats)
            q_logical_form = lf_result["logical_form"]
            answer: Optional[str] = None

            print_multiline("aq: q_logical_form> ", q_logical_form)
            run_result = run_cvc5(theory, q_logical_form, stats,
                                    print_out_theories=print_out_theories)
            valid: Sat = run_result["validity"]
            print(f"aq: validity {valid}")
            get_model_output = run_result["get_model_output"]
            if valid == Sat.SAT and get_model_output is not None:
                if generate_actual_answer:
                    print_multiline("aq: model output> ", get_model_output)
                    ans_result = get_model_output_2_nat_lang(valid_theory=theory,
                                                             question=question,
                                                             q_logical_form=q_logical_form,
                                                             get_model_output=get_model_output,
                                                             stats=stats)
                    answer = ans_result["answer"]
                else:
                    answer = "ANSWERABLE"
        else:
            prompt = dedent(f"""\
                Answer the following question:

                ```
                {question}
                ```

                You must wrap your answer in ```...``` delimiters.
                """)
            # print_multiline("aq:prompt classic> ", prompt)
            response = do_inference(prompt, stats)
            answers = extract_code_blocks(response)
            if len(answers) == 1:
                answer = answers[0]["contents"]
            else:
                print(f"aq: expected one answer code block, got {len(answers):,}")
                answer = None
        print(f"aq: answer_q -> {answer}")
        return answer


def generate_uniq_q_and_as(sentences: List[str],
                           num_qs: int,
                           temperature: float,
                           stats: Statistics) -> List[Tuple[str, str]]:
    """
    Generate a list of unique questions from the sentences.

    DEPRECATED. Only used when testing coverage.
    """
    with start_span("generate_uniq_q_and_as",
                    sentences=sentences,
                    num_qs=num_qs,
                    temperature=temperature) as span:
        iteration: int = -1
        for iteration in itertools.count():
            prompt = dedent(f"""\
                Consider the following background information:

                ```
                {" ".join(sentences)}
                ```

                I want you to generate {num_qs} unique pairs of questions and answers.
                Each question should be something that a reasonable person would expect
                to ask of the given background information. The answer should be your 
                best possible answer to that question.
                
                Your reply should use pairs of quotes for each question-answer pair:
                
                ```q
                ...
                ```

                ```a
                ...
                ```

                For example, if the background information is ```Diabetes is a common
                disease``` and you are asked to generate two questions then one
                possible response would be

                ```q
                Is diabetes a disease?
                ```
                ```a
                Yes, diabetes is a disease.
                ```
                ```q
                Is diabetes common?
                ```
                ```a
                Yes, diabetes is common.
                ```
                """)
            # print_multiline("q&a> ", prompt)
            response = do_inference(
                prompt, stats,
                system_prompt="You are an expert at analyzing documents",
                temperature=temperature)
            # print_multiline("guq:response> ", response)
            code_blocks = extract_code_blocks(response)
            q_and_as = parse_q_and_as_from_codeblocks(code_blocks)
            if q_and_as is not None:
                break
            print("guq: failed to parse q and as, retrying...")
        for q, a in q_and_as:
            print(f"guq: {q}: {a}")
        print(f"guq: generated {len(q_and_as):,} q+as, asked for {num_qs:,}")
        span.update(output=dict(
            q_and_as=q_and_as,
            num_iterations=iteration+1
        ))
        return q_and_as


def parse_q_and_as_from_codeblocks(
    code_blocks: List[dict]) -> Optional[List[Tuple[str, str]]]:
    """
    Given a list of code blocks, parse out the q and a pairs.
    If we don't see the correct alternating pattern of qs and as, return None
    """
    uniq_qs: Set[str] = set()
    q_and_as: List[Tuple[str, str]] = []
    for idx in range(0, len(code_blocks), 2):
        if not(code_blocks[idx]["type"] == "q" and
               code_blocks[idx+1]["type"] == "a"):
            print(f"pqafc: unexpected code block types at idx {idx}: "
                  f"{code_blocks[idx]['type']}, {code_blocks[idx+1]['type']}")
            return None
        assert code_blocks[idx+1]["type"] == "a"
        q = code_blocks[idx]["contents"].strip()
        a = code_blocks[idx+1]["contents"].strip()
        if q in uniq_qs:
            print(f"guq: skipping duplicate question: {q}")
            continue
        q_and_as.append((q, a))
        uniq_qs.add(q)
    return q_and_as


def coverage_job(question: str,
                 theory: str,
                 stats: Statistics,) -> bool:
    """
    Job for coverage testing: return True if we can answer the question.
    """
    answer = answer_q(theory, question, generate_actual_answer=False,
                      stats=stats)
    print(f"cj: question: {question} -> answer: {answer}")
    return answer is not None


def load_all_documents(stats: Statistics) -> List[Document]:
    """
    Load all sentences for a given set of subjects.
    """
    print(f"load_all_documents: subject_ids={g.p.subject_ids}")
    return [Document.load_for_subject(sid, stats) for sid in g.p.subject_ids]


def compute_coverage(run_file: Path,
                     this_pool_size: int,
                     temperature: float,
                     stats: Statistics,
                     num_tests: int = 100):
    """
    Test how many freshly generated questions about the build's sentences
    the theory can answer (that is, it doesn't say "I don't know").
    g.p must name the build's subjects (g.p.subject_ids).
    """
    with start_span("coverage test",
                    run_file=str(run_file),
                    pool_size=this_pool_size,
                    num_tests=num_tests,
                    temperature=temperature) as span:
        theory = load_run_result(run_file)["theory"]
        sentences = load_all_sentences(stats)
        print(f"tc: testing coverage with {num_tests:,} questions "
            f"and {len(sentences):,} sentences")
        uniq_q_and_as = generate_uniq_q_and_as(sentences, num_tests,
                                                temperature=temperature,
                                                stats=stats)
        uniq_questions = [q for q, _ in uniq_q_and_as]
        print(f"Generated {len(uniq_questions):,} unique questions")
        answers = starmap_with_dupes(
            worker=coverage_job,
            arguments=[(q, theory) for q in uniq_questions],
            job_prefix="cov",
            pool_size=this_pool_size
        )
        num_answered = sum(answers)
        print(f"tc: coverage results: {100*num_answered/num_tests:.2f} %"
              f" out of {num_tests:,} questions answered")
        span.update(output=dict(
            num_answered=num_answered,
            pct_answered=100*num_answered/num_tests
        ))


class ConversationalChatbot:
    """
    Use AgentCore to maintain conversation history and context, and 
    to decontextualize questions wrt previous turns in the conversation.
    """

    def __init__(self,
                 memory_id,
                 question_answerer: Callable[[str], str],
                 session_id=None):
        """
        Initialize chatbot with AgentCore memory
        
        Args:
            memory_id: The ID of your AgentCore memory
            session_id: Optional session ID for conversation continuity
        """
        self._client = boto3.client("bedrock-agentcore", region_name="us-east-1")
        self._memory_id = memory_id
        if session_id is None:
            self._session_id = f"session-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        else:
            self._session_id = session_id
        self._conversation_history = []
        self._actor_id = "actor-001"
        self._question_answerer = question_answerer


    def add_conversation_turn(self, role: str, message: str):
        """Add a conversation turn to memory"""
        response = self._client.create_event(
            memoryId=self._memory_id,
            actorId=self._actor_id,
            sessionId=self._session_id,
            eventTimestamp=datetime.datetime.now(),
            payload=[{
                "conversational": {
                    "role": role,
                    "content": {"text": message}
                }
            }]
        )
        return response["event"]["eventId"]

    def list_all_turns(self) -> list:
        """List all events/turns in a session"""
        response = self._client.list_events(
            memoryId=self._memory_id,
            actorId=self._actor_id,
            sessionId=self._session_id,
            maxResults=20  # Adjust as needed
        )
        events = list(reversed(response.get('events', [])))
        # for event in events:
        #     print(f"Event: {event}")
        return events

    def rewrite_q(self,
                  user_input: str,
                  model_id: Optional[str]=None) -> str:
        """Generate response using conversation context from memory"""
        if model_id is None:
            # model_id = "anthropic.claude-3-sonnet-20240229-v1:0"
            model_id = g.MODEL_ID
        context = self.list_all_turns()
        # print(f"context: {context}")
        if context:
            prompt = self._build_contextual_prompt(user_input, context)
            # print_multiline("contextual prompt> ", prompt)
            bedrock_runtime = boto3.client("bedrock-runtime", region_name="us-east-1")
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            })
            response = bedrock_runtime.invoke_model(
                modelId=model_id,
                body=body
            )
            response_body = json.loads(response["body"].read())
            response = response_body["content"][0]["text"]
            # print_multiline("rewrite_q response: ", response)
            code_blocks = extract_code_blocks(response)
            # print(f"code_blocks: {code_blocks}")
            rewritten_question = code_blocks[0]["contents"]
            # print(f"rewritten_question: {rewritten_question}")
            # self.add_to_memory(user_input, assistant_response)
            return rewritten_question
        else:
            # print("No conversation history found, returning original question.")
            return user_input

    def _build_contextual_prompt(self, current_input: str, context: list) -> str:
        """Build prompt with conversation history"""
        assert context, "only come in here if there are previous turns"
        turns = [(turn["payload"][0]["conversational"]["role"],
                    turn["payload"][0]["conversational"]["content"]["text"])
                    for turn in context]
        turns += [("USER", current_input)]
        # for i, turn in enumerate(turns):
        #     print(f"turn #{i:,}: {turn}")
        convo_history = "\n".join(f"{role}: {text}" for role, text in turns)
        return get_prompt("build_contextual_prompt",
                          conversation_history=convo_history)

    def chat(self, user_input: str, stats: Statistics) -> Tuple[str, str]:
        """Main chat interface"""
        assert user_input is not None and isinstance(user_input, str)
        print(f"\nUser: {user_input}")
        rewritten_q = self.rewrite_q(user_input)
        self.add_conversation_turn(role="USER", message=rewritten_q)
        response = self._question_answerer(rewritten_q, stats)
        print(f"Assistant: {response}")
        self.add_conversation_turn(role="ASSISTANT", message=response)
        return response, rewritten_q


def answer_questions(run_file: Union[Path, str],
                     stats: Statistics,
                     temperature: float = 0.7): # pylint: disable=unused-argument
    """
    Interactively answer questions based on the theory, command-line
    interface for testing.
    """
    result = load_run_result(run_file)
    theory, suggested_questions = itemgetter("theory", "suggested_questions")(result)
    history = InMemoryHistory()
    for question in suggested_questions:
        history.append_string(question)
    session = PromptSession(history=history)

    def question_answerer(q: str, stats: Statistics) -> str:
        answer = answer_q(theory, q,
                          generate_actual_answer=True,
                          stats=stats,
                          print_out_theories=False)
        return answer if answer is not None else "I don't know"

    # The conversation memory lives in an Amazon Bedrock AgentCore memory of
    # your own (it rewrites follow-up questions using earlier turns).
    memory_id = os.environ.get("CHATBOT_AGENTCORE_MEMORY_ID")
    if not memory_id:
        raise SystemExit("Set CHATBOT_AGENTCORE_MEMORY_ID to the id of an "
                         "AgentCore memory for the chatbot's conversation history.")
    chatbot = ConversationalChatbot(
        memory_id=memory_id,
        session_id=str(int(datetime.datetime.now().timestamp())),
        question_answerer=question_answerer
    )
    while True:
        try:
            one_stats: Statistics = Statistics()
            question = session.prompt("Ask me a question: ")
            if strip(question) == "":
                break
            assert question is not None and isinstance(question, str)
            answer, rewritten_question = chatbot.chat(question, one_stats)
            # answer = answer_q(theory, question, generate_actual_answer=True,
                            # stats=one_stats)
            if answer is None:
                answer = "I don't know"
            print(one_stats.summary() + "\n")
            print(f"\n{'='*50}\nQuestion: {question}")
            print(f"Rewritten Question: {rewritten_question}")
            print(f"Answer: {answer}")
            stats.add_another(one_stats)
        except (EOFError, KeyboardInterrupt):
            break
    print(stats.summary() + "\n")


def gen_diverse_set_of_qs_and_as(
        sentences: List[Sentence],
        num_new_questions_reqd: int,
        stats: Statistics,
        temperature: Optional[float] = None,
        existing_qs_and_as: Optional[List[Tuple[str, str]]] = None,
        question_similarity: Optional[QuestionSimilarity] = None,
        output_path: Optional[Path] = None,
        max_attempts: Optional[int] = None,
        ) -> List[Tuple[str, str]]:
    """
    Generate a diverse, unique, and informative set of questions from the
    sentences. With `max_attempts`, stop after that many generation calls
    even if fewer questions were accepted (a short window can run out of
    distinct questions).

    First, generate a stream of hopefully-unique qeustions in parallel.
    Then, filter them down to a unique set.

    Being informative means we also need an answer to the question and
    it shouldn't be something like "I don't know".

    The result is a list of tuples (question, answer)
    """
    assert isinstance(sentences[0], Sentence)
    existing_qs_and_as = existing_qs_and_as or []
    question_similarity = question_similarity or QuestionSimilarity()
    num_total_qs_reqd = len(existing_qs_and_as) + num_new_questions_reqd
    uniq_qs: Set[str] = set(q for q, a in existing_qs_and_as)
    a_to_q: Dict[str, str] = dict(existing_qs_and_as)
    rejected_qs: Set[str] = set()
    num_non_unique_qs: int = 0
    num_non_diverse_qs: int = 0
    num_i_dont_knows: int = 0

    print(f"gdsoq: starting with {len(uniq_qs):,} questions, "
          f"{len(sentences):,} sentences, "
          f"temp={temperature}, #new qs reqd={num_new_questions_reqd:,}")

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf8") as f:
            f.write("")  # just create/clear the file

    with start_span("gen_diverse_set_of_qs_and_as",
                    num_questions=num_new_questions_reqd,
                    temperature=temperature) as span:
        for attempt in itertools.count():
            if max_attempts is not None and attempt >= max_attempts:
                print(f"gdsoq: stopping after {attempt:,} attempts with "
                      f"{len(uniq_qs):,}/{num_total_qs_reqd:,} questions")
                break
            print(f"gdsoq: have {len(uniq_qs):,} unique+diverse questions, "
                  f"{num_non_unique_qs:,} non-unique, "
                  f"{num_non_diverse_qs:,} non-diverse, "
                  f"{num_i_dont_knows:,} I-dont-knows")
            if len(uniq_qs) >= num_total_qs_reqd:
                print(f"gdsoq: {len(uniq_qs)} >= {num_total_qs_reqd}; we're done")
                break
            # result = q.get()
            result = generate_q_and_a(
                nat_lang_sentences=sentences,
                qs_to_exclude=list(uniq_qs | rejected_qs),
                temperature=temperature,
                stats=stats
            )
            print(f"gdsoq: got result {result}")
            question = result["question"]
            print(f"gdsoq: generated question: {question}")
            answer = result["answer"]
            print(f"gdsoq: generated answer: {answer}")

            if result is not None and result["question"] is not None:
                question, answer = itemgetter("question", "answer")(result)
                print(f"gdsoq: got question: {question} and answer {answer}")
                if question in uniq_qs:
                    print(f"gdsoq: got a non-unique question: {question}")
                    num_non_unique_qs += 1
                elif is_this_answer_i_dont_know(answer, stats):
                    print(f"gdsoq: got an I-don't-know answer: {answer}")
                    num_i_dont_knows += 1
                else:
                    print("gdsoq: checking diversity of question")
                    is_diverse, embedding, _most_similar_question = \
                        question_similarity.question_is_sufficiently_diverse(question)
                    if is_diverse:
                        uniq_qs.add(question)
                        question_similarity.add_question(question, embedding)
                        a_to_q[question] = answer
                        print(f"gdsoq: got a high-diversity question: {question}, "
                              f"now have {len(uniq_qs):,} unique questions:")
                        for uq in uniq_qs:
                            print(f"gdsoq: - {uq}")
                        if output_path is not None:
                            with open(output_path, "w", encoding="utf8") as f:
                                f.write("\n".join(uniq_qs) + "\n")
                    else:
                        print(f"gdsoq: discarded a low-diversity question: {question}")
                        num_non_diverse_qs += 1
                        rejected_qs.add(question)
            # start_worker()  # replace the finished worker

        # for p in all_processes:
        #     p.kill()
        rv = [(q, a_to_q[q]) for q in uniq_qs]
        span.update(output=dict(
            qs_and_as=rv
        ))
        print(f"gdsoq: returning {rv}")
        return rv


def q_minimum_diversity_score(new_q: str,
                              existing_qs: Set[str],
                              stats: Statistics) -> float:
    """
    Compute a diversity score for a new question compared to
    a set of existing questions. The higher the score, the more
    diverse the question is.
    """
    return min(
        q_diversity_score(new_q, existing_q, stats)
        for existing_q in existing_qs
    )


def q_diversity_score(new_q: str, existing_q: str,
                      stats: Statistics) -> float:
    """
    Compute a diversity score for a new question compared to
    a set of existing questions. The higher the score, the more
    diverse the question is.
    """
    prompt = dedent(f"""\
        I want you take take two questions and return a diversity score
        between 0.0 and 1.0, where 0.0 means the questions are identical
        or nearly identical in meaning, and 1.0 means the questions are
        completely different in meaning.

        For example, if the questions are "Is the sky blue?" and "Is the
        sky blue?", the score should be 0.0. On the other hand, if the
        questions are "Is the sky blue?" and "What is the capital of
        France?", the score should be close to 1.0.

        The first question is:
        ```
        {new_q}
        ```

        The second question is:
        ```
        {existing_q}
        ```

        Return the diversity score (between 0.0 and 1.0)
        wrapped in ``` ... ```
        """)
    # print_multiline("qd: prompt> ", prompt)
    response = do_inference(prompt, stats)
    sentences = extract_code_blocks(response)
    if len(sentences) == 0:
        print("qd: no code blocks found in diversity score response")
        return 0.0
    rv = sentences[0]["contents"]  # ignore multiple code blocks
    print(f"qd: diversity of {new_q} vs {existing_q}: {rv}")
    try:
        return float(rv)
    except ValueError:
        print(f"qd: failed to parse diversity score: {rv}")
        return 0.0


# if __name__ == "__main__":
#     test_yaml_representation()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Parameters for one theory build. In their own module so they can be imported
anywhere (the formalizer, the chatbot and the web service all read g.p).
"""

import os
from typing import Optional, List
from dataclasses import dataclass, asdict, field, fields


@dataclass
class Params:
    """ Parameters for one build of a theory from a set of subjects """
    # The subjects (documents) whose sentences are formalized together.
    subject_ids: List[str] = field(default_factory=list)
    # Formalize only the first N sentences (None = all of them). The inline
    # (no-builder) mode uses this to keep the web task responsive.
    max_sentences: Optional[int] = None

    # --- Test set -------------------------------------------------------
    # Questions to generate per sentence, for text and for FHIR documents.
    qas_per_sentence: float = 1.0
    fhir_qas_per_sentence: float = 2.0
    # Correct answers the theory needs, as a fraction of the test set
    # (clamped to the test-set size minus two).
    ncar_ratio: float = 0.85
    # The test set is generated window by window: each window of this many
    # consecutive sentences gets its share of the questions, so they cover
    # the whole document, and the windows run in parallel.
    qa_window_sentences: int = 10
    # Max concurrent question-generation windows. Each makes Bedrock calls,
    # so this is a throttling knob (like standalone_pool_size).
    qa_pool_size: int = 16

    # --- Chatbot / answering --------------------------------------------
    # Caps on the convert_question_to_logical_form (cq2lf) retry loop, which
    # otherwise retries forever when the LLM never emits a parseable
    # ```result``` block. A question that never yields a logical form fails
    # cleanly (logical_form=None) and is answered "I don't know".
    max_cq2lf_iterations: int = 10
    max_cq2lf_seconds: float = 1200.0  # 20 min wall-clock ceiling per question
    do_profile: bool = False

    # --- kiro-cli theory engine (kiro_formalizer.py) --------------------
    # kiro-cli --model; None uses the kiro-cli default.
    kiro_model: Optional[str] = field(default_factory=lambda: os.environ.get(
        "CHATBOT_KIRO_MODEL") or None)
    # Wall-clock budget across ALL of the build's kiro-cli sessions.
    kiro_timeout_seconds: float = 14400.0
    # kiro-cli sessions: the first build plus resumes that feed back the
    # harness's verification report when the result falls short.
    kiro_max_attempts: int = 3
    # Stall watchdog: a kiro-cli session that emits no stream-json event for
    # this long is killed and resumed with a nudge (up to
    # kiro_max_stall_resumes times, within kiro_timeout_seconds). Writing a
    # large theory in one tool call takes ~2 minutes, so 8 is generous.
    kiro_idle_timeout_seconds: float = 480.0
    kiro_max_stall_resumes: int = 3
    # The harness always re-runs cvc5 on the agent's theory and fragments.
    # With this on (default) it also re-derives each answer from the cvc5
    # model (get_model_output_2_nat_lang) and judges it (llm_as_judge) instead
    # of trusting the agent's own answer and verdict, so an answer only counts
    # if it is grounded in the model. Off = trust the agent's "correct" flag
    # for SAT fragments (cheaper, weaker).
    kiro_reverify_answers: bool = True
    # Questions the harness re-verifies at once (cvc5 + two Bedrock calls
    # each).
    verify_pool_size: int = 16

    # --- Decontextualization ("make standalone") ------------------------
    # Each sentence is rewritten using only the previous N RAW sentences as
    # context (not the whole preceding document), so every rewrite is an
    # independent job and the prompt size stays bounded.
    standalone_context_sentences: int = 10
    # Max concurrent standalone rewrites. Each is one Bedrock call, so this is
    # the throttling knob: do_inference backs off on ThrottlingException, but a
    # NUM_CORES-wide fan-out just keeps the account throttled.
    standalone_pool_size: int = 16

    def to_dict(self) -> dict:
        """ Convert to a dict """
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        """ Create from a dict, ignoring keys this version does not know """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

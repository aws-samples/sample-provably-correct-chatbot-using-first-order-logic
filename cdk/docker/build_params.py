# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
User-settable parameters for one theory build (the "Build parameters" dialog).

One list, SPECS, describes every parameter the dialog offers: its label, type,
bounds, help text and default. The defaults are the params.Params dataclass
defaults (the kiro-cli model default comes from the CHATBOT_KIRO_MODEL
environment the builder sees), so a build started with an untouched form is a
default build.

Flow: GET /api/build-params serves `specs()`; the create/rebuild endpoints
pass the submitted values through `validate()` (raises ValueError -> 400); the
result is stored on the job and handed to the builder as BUILD_PARAMS JSON,
where store.run_job calls `apply()`.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from params import Params

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{0,100}$")


def _params_default(name: str) -> Any:
    return getattr(Params(), name)


def specs() -> List[Dict[str, Any]]:
    """Every user-settable parameter, with its current default."""
    return [
        dict(key="qas_per_sentence", group="Test set", type="float",
             label="Questions per sentence", min=0.1, max=5.0, step=0.1,
             default=_params_default("qas_per_sentence"),
             help="Size of the test set for a text document, per sentence "
                  "(FHIR documents use twice this many)."),
        dict(key="ncar_ratio", group="Test set", type="float",
             label="Required correct ratio", min=0.1, max=1.0, step=0.05,
             default=_params_default("ncar_ratio"),
             help="Correct test answers the theory needs, as a fraction of "
                  "the test set (at most the test-set size minus two)."),
        dict(key="qa_window_sentences", group="Test set", type="int",
             label="Question window (sentences)", min=2, max=200, step=1,
             default=_params_default("qa_window_sentences"),
             help="Questions are generated over windows of this many "
                  "consecutive sentences, in parallel, so they cover the "
                  "whole document."),
        dict(key="kiro_model", group="kiro-cli", type="text",
             label="kiro-cli model", default=_params_default("kiro_model") or "",
             help="Passed to kiro-cli --model. Leave empty for kiro-cli's "
                  "default."),
        dict(key="kiro_max_attempts", group="kiro-cli", type="int",
             label="Sessions", min=1, max=10, step=1,
             default=_params_default("kiro_max_attempts"),
             help="The first kiro-cli session plus resumes that feed back the "
                  "harness's verification report."),
        dict(key="kiro_timeout_seconds", group="kiro-cli", type="int",
             label="Time budget (s)", min=300, max=43200, step=300,
             default=int(_params_default("kiro_timeout_seconds")),
             help="Wall-clock budget across all of the build's kiro-cli "
                  "sessions."),
        dict(key="kiro_idle_timeout_seconds", group="kiro-cli", type="int",
             label="Stall timeout (s)", min=60, max=3600, step=30,
             default=int(_params_default("kiro_idle_timeout_seconds")),
             help="A session silent for this long is killed and resumed with "
                  "a nudge (does not use up a session)."),
    ]


def defaults() -> Dict[str, Any]:
    return {s["key"]: s["default"] for s in specs()}


def validate(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Fill missing keys with defaults, coerce types, check bounds. Unknown keys
    are rejected so a typo never silently falls back to a default.
    """
    raw = dict(raw or {})
    by_key = {s["key"]: s for s in specs()}
    unknown = sorted(set(raw) - set(by_key))
    if unknown:
        raise ValueError(f"unknown build parameter(s): {', '.join(unknown)}")
    out: Dict[str, Any] = {}
    for key, spec in by_key.items():
        value = raw.get(key, spec["default"])
        label = spec["label"]
        kind = spec["type"]
        if kind in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a number") from None
            if not math.isfinite(number):
                raise ValueError(f"{label} must be a number")
            if kind == "int":
                if number != int(number):
                    raise ValueError(f"{label} must be a whole number")
                number = int(number)
            lo, hi = spec.get("min"), spec.get("max")
            if hi is None and lo is not None and number < lo:
                raise ValueError(f"{label} must be at least {lo}")
            if hi is not None and not lo <= number <= hi:
                raise ValueError(f"{label} must be between {lo} and {hi}")
            out[key] = number
        elif kind == "choice":
            if value not in spec["choices"]:
                raise ValueError(f"{label} must be one of "
                                 f"{', '.join(spec['choices'])}")
            out[key] = value
        else:
            text = str(value or "").strip()
            if not _MODEL_RE.match(text):
                raise ValueError(f"{label} may only contain letters, digits "
                                 "and . _ : / -")
            out[key] = text
    return out


def apply(values: Dict[str, Any], subject_id: str,
          max_sentences: Optional[int] = None) -> Params:
    """The Params for a build of `subject_id` with these (validated) values."""
    return Params(
        subject_ids=[subject_id],
        max_sentences=max_sentences,
        qas_per_sentence=float(values["qas_per_sentence"]),
        fhir_qas_per_sentence=2 * float(values["qas_per_sentence"]),
        ncar_ratio=float(values["ncar_ratio"]),
        qa_window_sentences=int(values["qa_window_sentences"]),
        kiro_model=values["kiro_model"] or None,
        kiro_max_attempts=int(values["kiro_max_attempts"]),
        kiro_timeout_seconds=float(values["kiro_timeout_seconds"]),
        kiro_idle_timeout_seconds=float(values["kiro_idle_timeout_seconds"]),
    )

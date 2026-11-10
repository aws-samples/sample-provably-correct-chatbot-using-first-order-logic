# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The stack's Bedrock IAM grant names exact model ids (cdk/lib/bedrock-models.ts).
Every model id the Python code invokes must be in that list, or the deployed
tasks get AccessDenied from Bedrock."""

import os
import re

import globals as g

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _cdk_list(name: str) -> list:
    src = _read("cdk", "lib", "bedrock-models.ts")
    m = re.search(rf"export const {name} = \[(.*?)\];", src, re.S)
    assert m, f"{name} not found in bedrock-models.ts"
    return re.findall(r"'([^']+)'", m.group(1))


def _used_ids() -> set:
    ids = {g.MODEL_ID, g.FORMALIZATION_MODEL_ID, g.EMBEDDING_MODEL_ID}
    # Literal model ids passed in code, such as the content-filter fallback.
    for name in ("utils.py", "auto_formalizer.py", "questions_similarity.py"):
        for line in _read(name).splitlines():
            if line.lstrip().startswith("#"):
                continue
            ids.update(re.findall(r'model_id\s*=\s*"([^"]+)"', line))
    return ids


def test_every_invoked_model_is_granted():
    granted = set(_cdk_list("BEDROCK_INFERENCE_PROFILE_IDS")) | set(
        _cdk_list("BEDROCK_FOUNDATION_MODEL_IDS"))
    assert _used_ids() <= granted, sorted(_used_ids() - granted)


def test_no_model_is_granted_that_the_code_does_not_use():
    granted = set(_cdk_list("BEDROCK_INFERENCE_PROFILE_IDS")) | set(
        _cdk_list("BEDROCK_FOUNDATION_MODEL_IDS"))
    assert granted <= _used_ids(), sorted(granted - _used_ids())


def test_profiles_are_cross_region_and_models_are_not():
    assert all(re.match(r"^(global|us)\.", i)
               for i in _cdk_list("BEDROCK_INFERENCE_PROFILE_IDS"))
    assert not any(re.match(r"^(global|us)\.", i)
                   for i in _cdk_list("BEDROCK_FOUNDATION_MODEL_IDS"))

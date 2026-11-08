# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
A wrapper around the prompts in prompts.yaml.
"""
from pathlib import Path

import yaml

import globals as g


SYSTEM_PROMPTS = yaml.safe_load((Path.cwd() / "system-prompts.yaml").read_text())
PROMPTS = yaml.safe_load((Path.cwd() / "prompts.yaml").read_text())


def get_system_prompt(prompt_id: str, **kwargs) -> str:
    """
    Given an id, pull the system prompt out of system-prompts.yaml.
    """
    return _get_either_prompt(SYSTEM_PROMPTS, prompt_id, **kwargs)


def get_prompt(prompt_id: str, **kwargs) -> str:
    """
    Given an id, pull the prompt out of prompts.yaml.
    """
    return _get_either_prompt(PROMPTS, prompt_id, **kwargs)


def _get_either_prompt(prompts: dict,
                       prompt_id: str,
                       **kwargs) -> str:
    """ Internal helper method """
    subj_type = g.subject_type
    if subj_type not in prompts:
        subj_type = "default"
    # print(f"_get_either_prompt {subj_type} {prompt_id}")
    try:
        prompt = prompts[subj_type][prompt_id]
    except KeyError:
        prompt = prompts["default"][prompt_id]
    # print(f"prompt {prompt}")
    if kwargs:
        # print(f"kwargs: {kwargs}")
        prompt = prompt.format(**kwargs)
        # print(f"prompt {prompt}")
    return prompt

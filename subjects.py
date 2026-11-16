# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Central list of all subjects.
"""

import re
from dataclasses import dataclass
from typing import Optional, List, Tuple
from pathlib import Path


@dataclass
class Subject:
    name: str
    id: str
    wikipedia_url: Optional[str] = None
    synthea_path: Optional[str] = None

    def title(self) -> str:
        """
        This is intended to be used in the web front end.
        """
        return f"A provably-correct chatbot that is an expert in {self.name.title()}."

    def __str__(self):
        result = f"Subject(id='{self.id}', name='{self.name}'"
        if self.wikipedia_url:
            result += f", wikipedia_url='{self.wikipedia_url}'"
        if self.synthea_path:
            result += f", synthea_path='{self.synthea_path}'"
        return result + ")"
    
    __repr__ = __str__


SUBJECTS = [
    Subject("diabetes", "diabetes",
            wikipedia_url="https://en.wikipedia.org/wiki/Diabetes"),
    Subject("Oncology", "oncology",
            wikipedia_url="https://en.wikipedia.org/wiki/Oncology"),
    Subject("Cardiovascular disease", "cardiovascular_disease",
            wikipedia_url="https://en.wikipedia.org/wiki/Cardiovascular_disease"),
    Subject("Cerebrovascular disease", "stroke",
            wikipedia_url="https://en.wikipedia.org/wiki/Cerebrovascular_disease"),
    Subject("Chronic obstructive pulmonary disease", "COPD",
            wikipedia_url="https://en.wikipedia.org/wiki/Chronic_obstructive_pulmonary_disease"),
    Subject("New Zealand", "new_zealand",
            wikipedia_url="https://en.wikipedia.org/wiki/New_Zealand"),
]


def parse_synthea_file_name(file_name) -> Tuple[str, str]:
    """
    Extract the person's name, so we can use it as a subject ID.

    >>> parse_synthea_file_name("Adriana394_Almaraz628_d00420ac-9a07-66dc-ee93-d2e3e9bbf23e")
    ('Adriana', 'Almaraz')
    """
    parts = file_name.split("_")
    first = re.sub(r"\d+$", "", parts[0])
    last  = re.sub(r"\d+$", "", parts[1])
    return (first, last)


def get_synthea_subjects() -> List[Subject]:
    # The Synthea sample FHIR bundles are OPTIONAL demo data and are not shipped
    # in every build (the directory is gitignored, so a clean git checkout — e.g.
    # the CodeCommit-sourced CodeBuild deploy — does not contain it). Its absence
    # must not crash module import: without this guard .iterdir() raises
    # FileNotFoundError at import time, the FastAPI app fails to load, the ECS
    # task never becomes healthy, and the deployment circuit breaker rolls back.
    # When the data is present the "Add theory -> pick a Synthea sample patient"
    # list is populated as before; when absent it is simply empty.
    synthea_dir = Path.cwd() / "sentences" / "synthea_sample_data_fhir_dstu2_latest"
    if not synthea_dir.is_dir():
        return []
    results = []
    for path in synthea_dir.iterdir():
        first, last = parse_synthea_file_name(path.stem)
        results.append(Subject(f"{first} {last}",
                               f"{first}_{last}",
                               synthea_path=path))
    return results


SUBJECTS += get_synthea_subjects()
ID_TO_SUBJECT = {subject.id: subject for subject in SUBJECTS}


def is_synthea_based(subject_id: str) -> bool:
    return get_subject_by_id(subject_id).synthea_path is not None


def subject_type(subject_id: str) -> str:
    if is_synthea_based(subject_id):
        return "fhir"
    else:
        return "default"


def subject_list_type(subject_ids: List[str]) -> str:
    types = [subject_type(sid) for sid in subject_ids]
    assert len(set(types)) == 1, f"all subjects should have same type: {subject_ids}, {types}"
    return types[0]


def get_subject_by_id(subject_id: str) -> Subject:
    try:
        return ID_TO_SUBJECT[subject_id]
    except KeyError as ex:
        raise ValueError(f"Subject with id {subject_id} not found.") from ex

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# pylint: disable=use-dict-literal, too-many-locals, no-else-return, too-many-instance-attributes, too-many-lines, multiple-imports, broad-exception-raised
# pylint: disable=not-context-manager # doesn't get context from otel.py
# pylint: disable=too-many-positional-arguments, too-many-arguments, unnecessary-pass
# pylint: disable=wrong-import-position,import-error,multiple-imports, use-dict-literal
"""
Code to handle sentences, including how to make individual sentences standalone
and groups of sentences (this is mostly an issue with FHIR records)
"""
from __future__ import annotations
from typing import Optional, List, Union, Type
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED
from concurrent.futures import wait as futures_wait
from pathlib import Path
from abc import ABC, abstractmethod

import yaml

import globals as g
from subjects import is_synthea_based, get_subject_by_id, is_mdm_based
from utils import (Statistics, load_lines, _cancelled,
                   extract_code_blocks, do_inference, flatten1, JSON,
                   get_global_stats, yaml_dump, ForcedLiteralDumper,
                   exactly_one_is_non_none, at_most_one_is_non_none)
from otel import start_span
from prompts import get_prompt
from params import Params


class Document(ABC):
    """ Super class for all types of documents (text, FHIR, etc.) """
    def __init__(self, subject_id: str,
                       stats: Optional[Statistics] = None):
        self._stats = stats or get_global_stats()
        if is_synthea_based(subject_id):
            suffix = ""
            fname = get_subject_by_id(subject_id).synthea_path
        else:
            suffix = ".txt"
            fname = subject_id
        if not isinstance(fname, Path) or fname.is_absolute():
            fname = Path.cwd() / "sentences" / fname
        self._raw_sentences_path: Path = Path(f"{fname}{suffix}")
        self._standalones_path: Path = Path(f"{fname}-standalone{suffix}")

    @property
    def stats(self) -> Statistics:
        """ Statistics object """
        return self._stats

    @classmethod
    def make_chunk_standalone(cls, sentences: List[Sentence]) -> List[str]:
        """
        Return a list of strings that represent the standalone versions of
        the sentences. For a TextDocument is easy, we just concatenate them.
        For a FHIR document we have to handle the (potentially) different
        patient entries from the incoming sentences.
        """
        doc_classes = {s.doc_class for s in sentences}
        assert len(doc_classes) == 1, f"Chunk has different doc types: {doc_classes}"
        [doc_class] = list(doc_classes)
        return doc_class.make_chunk_standalone_(sentences)

    @staticmethod
    def document_runs(sentences: List[Sentence]) -> List[List[Sentence]]:
        """ The consecutive runs of sentences that come from one document. """
        def key(s: Sentence):
            return (s.doc_class, id(getattr(s, "document", None)))

        runs: List[List[Sentence]] = []
        for s in sentences:
            if runs and key(s) == key(runs[-1][-1]):
                runs[-1].append(s)
            else:
                runs.append([s])
        return runs

    @classmethod
    def make_standalone_lines(cls, sentences: List[Sentence]) -> List[str]:
        """
        Like make_chunk_standalone, for sentences that may come from several
        documents (of different types): each run of sentences from one
        document is made standalone on its own, in order.
        """
        return [line for run in cls.document_runs(sentences)
                for line in cls.make_chunk_standalone(run)]

    @classmethod
    @abstractmethod
    def make_chunk_standalone_(cls, sentences: List[Sentence]) -> List[str]:
        """
        Don't replicate the error checking.
        """

    @abstractmethod
    def sentences(self) -> List[Sentence]:
        """
        Return all standalone Sentences in this document
        """
        pass

    @classmethod
    def load_for_subject(cls, subject_id: str, stats: Statistics) -> Document:
        """
        Load the standalone sentences, either computing them or
        loading them from a local file.
        """
        if is_synthea_based(subject_id):
            ctor = FHIRdocument
        elif is_mdm_based(subject_id):
            ctor = MDMdocument
        else:
            ctor = TextDocument
        return ctor(subject_id, stats)


def make_text_sentence_standalone(
        sentence: str,
        previous_sentences: List[str],
        stats: Statistics) -> str:
    """
    Given the context (prior sentences), rewrite and return a
    sentence so its meaning is preserved in isolation.

    Raises ValueError if the LLM reply has no ``` code block to take the
    rewrite from; compute_standalones() retries that.
    """
    with start_span("make_text_sentence_standalone",
                    sentence=sentence,
                    previous_sentences=previous_sentences) as span:
        if len(previous_sentences) == 0:
            # No context to resolve against; the sentence is already standalone.
            return sentence
        prompt = get_prompt("make_text_sentence_standalone",
                            previous_sentences="\n".join(previous_sentences),
                            sentence=sentence)
        response = do_inference(
            prompt, stats,
            system_prompt="You are an expert in the english language, "
                          "and in particular biomedical literature")
        sentences = extract_code_blocks(str(response))
        if not sentences:
            raise ValueError(f"no ``` block in standalone rewrite of {sentence!r}")
        rv = sentences[0]["contents"]  # ignore multiple code blocks
        span.update(output=dict(
            standalone_sentence=rv,
            stats=stats.summarize_to_dict()
        ))
        print(f"standalone: {rv}")
        return rv


# do_inference already retries transient Bedrock errors (throttling, timeouts)
# itself, so these attempts only cover a reply we could not parse.
STANDALONE_MAX_ATTEMPTS: int = 3


class TextDocument(Document):
    """
    For text documents (like Wikipedia pages), computing the standalone versions
    is slow (requires an LLM inference) so we precompute them and store
    them in a local file.
    """
    def __init__(self, subject_id: str,
                       stats: Optional[Statistics] = None):
        super().__init__(subject_id, stats)
        self._originals: List[str] = load_lines(self._raw_sentences_path)
        self._standalones: List[str] = self._compute_or_load_standalone_sentences()

    def sentences(self) -> List[Sentence]:
        """
        Return all standalone Sentences in this document, each paired with the
        rewrite at the same position. (Pairing by position, not by looking the
        text up, so a sentence that occurs twice is not an error.)
        """
        if len(self._standalones) != len(self._originals):
            raise ValueError(
                f"{self._standalones_path.name} has {len(self._standalones):,} "
                f"sentences but {self._raw_sentences_path.name} has "
                f"{len(self._originals):,}; delete the standalone file to "
                "recompute it")
        return [TextSentence(orig, standalone=standalone)
                for orig, standalone in zip(self._originals, self._standalones)]

    @classmethod
    def make_chunk_standalone_(cls, sentences: List[Sentence]) -> List[str]:
        """
        Don't replicate the error checking.
        """
        rv = [s.standalone for s in sentences]
        print(f"TextDocument::make_chunk_standalone_ -> {rv}")
        return rv

    def get_standalone_version(self, original: str) -> str:
        """
        Find the standalone sentence in the same index as the original
        """
        indices = [i for i, ith_orig in enumerate(self._originals)
                   if ith_orig == original]
        assert len(indices) > 0, f"Sentence '{original}' not found in source document"
        assert len(indices) == 1, f"Sentence '{original}' found in multiple places: {indices}"
        [index] = indices
        return self._standalones[index]

    def _compute_or_load_standalone_sentences(self) -> List[str]:
        if not self._standalones_path.exists():
            self.compute_standalones()
            assert self._standalones_path.exists()
        return load_lines(self._standalones_path)

    def standalone_jobs(self, context_sentences: int) -> List[tuple]:
        """
        One (sentence, context) pair per sentence. Sentence i's context is
        the previous `context_sentences` RAW sentences (never earlier
        rewrites), so the jobs are independent of each other.
        """
        assert context_sentences >= 0, f"context_sentences={context_sentences}"
        return [(orig, self._originals[max(0, i - context_sentences):i])
                for i, orig in enumerate(self._originals)]

    def compute_standalones(self) -> List[str]:
        """
        For each sentence, we use the preceding N raw sentences
        (Params.standalone_context_sentences) to replace pronouns with
        their referents.

        The rewrites are independent single LLM calls, so they run on a
        thread pool (I/O-bound; no process spawn) capped at
        Params.standalone_pool_size concurrent calls to limit Bedrock
        throttling. Each sentence runs once; only an unparseable reply is
        retried, up to STANDALONE_MAX_ATTEMPTS.
        """
        p = g.p or Params()
        jobs = self.standalone_jobs(p.standalone_context_sentences)
        pool_size = max(1, min(p.standalone_pool_size, len(jobs)))
        print(f"compute_standalones: {len(jobs):,} sentences, "
              f"context {p.standalone_context_sentences}, pool {pool_size}")
        results: List[Optional[str]] = [None] * len(jobs)
        attempts: List[int] = [0] * len(jobs)

        executor = ThreadPoolExecutor(max_workers=pool_size,
                                      thread_name_prefix="standalone")
        pending: dict = {}  # future -> (sentence index, its Statistics)

        def submit(i: int) -> None:
            attempts[i] += 1
            job_stats = Statistics()
            sentence, context = jobs[i]
            fut = executor.submit(make_text_sentence_standalone,
                                  sentence, context, job_stats)
            pending[fut] = (i, job_stats)

        try:
            for i in range(len(jobs)):
                submit(i)
            while pending:
                # Poll so a cooperative cancel (store build kill) is seen
                # within ~1s even while every thread is mid-LLM-call.
                if _cancelled():
                    raise RuntimeError("compute_standalones cancelled")
                done, _ = futures_wait(list(pending), timeout=1.0,
                                       return_when=FIRST_COMPLETED)
                for fut in done:
                    i, job_stats = pending.pop(fut)
                    # Count the tokens even of a failed attempt.
                    self.stats.add_another(job_stats)
                    try:
                        results[i] = fut.result()
                    except Exception as ex:  # pylint: disable=broad-except
                        if attempts[i] >= STANDALONE_MAX_ATTEMPTS:
                            raise RuntimeError(
                                f"standalone rewrite of sentence {i} failed "
                                f"{attempts[i]} times: {ex}") from ex
                        print(f"compute_standalones: sentence {i} attempt "
                              f"{attempts[i]} failed ({ex!r}); retrying")
                        submit(i)
        finally:
            # On error/cancel, drop queued rewrites; in-flight calls finish
            # in the background (threads cannot be killed) and are ignored.
            executor.shutdown(wait=False, cancel_futures=True)

        # One rewrite per line, index-aligned with the raw sentences file, so
        # a rewrite that came back with a line break is joined into one line.
        results = [" ".join(r.split()) for r in results]
        self._standalones_path.write_text("\n".join(results))
        return results


class FHIRdocument (Document):
    """ A document that contains JSON-formatted HL7 FHIR records for one patient """
    def __init__(self, subject_id: str,
                       stats: Optional[Statistics] = None):
        super().__init__(subject_id, stats)
        orig_json: JSON = json.loads(self._raw_sentences_path.read_text())
        all_entries: JSON = orig_json["entry"]
        print(f"Found {len(all_entries):,} entries")
        entries_by_type: dict = defaultdict(list)
        for entry in all_entries:
            entries_by_type[entry["resource"]["resourceType"]].append(entry)
        len_by_type = {k: len(v) for k, v in entries_by_type.items()}
        print(f"#entries/type: {len_by_type}")
        self._patient_entries: List[JSON] = entries_by_type["Patient"]
        print(f"Found {len(self._patient_entries):,} patient entries")
        self._other_entries: List[JSON] =\
            flatten1([entries for rtype, entries in entries_by_type.items()
                      if rtype != "Patient"])

    @property
    def patient_entries(self) -> List[JSON]:
        """
        Return the patient-info for this Document (one patient's records)
        """
        return list(self._patient_entries)

    def sentences(self) -> List[Sentence]:
        """
        return all standalone Sentences in this document
        """
        return [FHIRsentence(observation, document=self)
                for observation in self._other_entries]

    @classmethod
    def make_chunk_standalone_(cls, sentences: List[Sentence]) -> List[str]:
        """
        Don't replicate the error checking.
        """
        print(f"FHIR::make_chunk_standalone_ {sentences}")
        def key_fn(s: Sentence):
            return json.dumps(s.patient_entries)
        pes_2_sents = defaultdict(list)
        for s in sentences:
            pes_2_sents[key_fn(s)].append(s)
        rv = flatten1(json.loads(pes_str) + [s.original for s in sentences]
                      for pes_str, sentences in pes_2_sents.items())
        rv = list(map(str,rv))
        print(f"make_chunk_standalone_ -> {rv}")
        return rv


class MDMdocument (TextDocument):
    """ A document that contains sentences extracted from the MDM pdf """
    def __init__(self, subject_id: str,
                       stats: Optional[Statistics] = None):
        super().__init__(subject_id, stats)
        originals_with_header = self._raw_sentences_path.read_text().split("---")
        originals_with_header = [s.replace("\n", " ")
                                 for s in originals_with_header]
        print("originals_with_header:")
        for s in originals_with_header:
            print(f" - <<{s}>>")
        self.header = originals_with_header[0]
        self._originals = originals_with_header[1:]

    @classmethod
    def make_chunk_standalone_(cls, sentences: List[Sentence]) -> List[str]:
        """
        Return a list of strings that represent the standalone versions of
        the sentences. For a TextDocument is easy, we just concatenate them.
        For a FHIR document we have to handle the (potentially) different
        patient entries from the incoming sentences.
        """
        docs = {s.document for s in sentences}
        [doc] = list(docs)
        assert len(docs) == 1, f"all sents must be from same doc: {docs}"
        return [doc.header] + [s.original for s in sentences]

    def sentences(self) -> List[Sentence]:
        """
        Iterate over all standalone Sentences in this document
        """
        return [MDMsentence(original, document=self)
                for original in self._originals]


class Sentence:
    """
    base class for all types of sentences.

    We use instances thereof to copy sentences from one layer to
    another, while keeping track of any document-specific context
    (such as the patient info, in the case of FHIR). We thus DONT point
    to the associated document in here to avoid serializing the entire
    Document when we serialize a Sentence.
    """
    def __init__(self,
                 value: Union[str, JSON],
                 document: Optional[Document] = None,
                 document_class: Optional[Type] = None):
        assert at_most_one_is_non_none(document, document_class)
        self._original = value
        self._doc_class = type(document) if document_class is None else document_class

    @property
    def original(self) -> str:
        """ The original sentence """
        if isinstance(self._original, str):
            return self._original
        else:
            return json.dumps(self._original)

    @property
    def doc_class(self):
        """ The class of the Document instance that owns this. """
        return self._doc_class

    @original.setter
    def original(self, value: Optional[str]):
        self._original = value

    def __str__(self) -> str:
        return f"Sentence(original='{self.original}')"

    def __repr__(self) -> str:
        return self.__str__()


class TextSentence(Sentence):
    """
    A Sentence that belongs to a TextDocument. Here
    we can associate the standalone version of each 
    sentence directly in the sentence, as it doesn't change
    as we combine sentences across Documents.
    """
    def __init__(self, original: Union[str, JSON],
                 standalone: Optional[Union[str, JSON]] = None,
                 document: Optional[Document] = None,
                 document_class: Optional[Type] = None):
        assert exactly_one_is_non_none(standalone, document, document_class)
        super().__init__(original, document, document_class)
        if standalone is None:
            self._standalone = document.get_standalone_version(self.original)
        else:
            self._standalone = standalone
        self._doc_class = TextDocument

    @property
    def standalone(self) -> str:
        """
        Return the standalone version of this original sentence.
        For a text document we can cache this as it's expensive
        to create.
        """
        return self._standalone

    def yaml_representer(self, dumper):
        """
        Convert into YAML representation
        """
        return dumper.represent_mapping('!TextSentence', {
            'original': self.original,
            'standalone': self.standalone
        })

    @classmethod
    def yaml_constructor(cls, loader, node):
        """
        Convert from YAML representation
        """
        fields = loader.construct_mapping(node, deep=True)
        return TextSentence(**fields)

    def __eq__(self, other):
        return isinstance(other, TextSentence) and \
               self.original == other.original and \
               self.standalone == other.standalone

    def __hash__(self):
        return hash((self.original, self.standalone))

    def __str__(self) -> str:
        return f"TextSentence(original='{self.original}' standalone='{self.standalone}')"

    def __repr__(self) -> str:
        return self.__str__()


ForcedLiteralDumper.add_representer(TextSentence,
                                    lambda dumper, sentence:
                                        sentence.yaml_representer(dumper))
yaml.SafeLoader.add_constructor('!TextSentence', TextSentence.yaml_constructor)


class FHIRsentence(Sentence):
    """
    An observation from a FHIR document
    """
    def __init__(self, original: str,
                 document: Optional[Document] = None,
                 document_class: Optional[Type] = None,
                 patient_entries: Optional[List[JSON]] = None):
        assert exactly_one_is_non_none(document, document_class, patient_entries)
        super().__init__(original, document, document_class)
        self._patient_entries: List[JSON] = patient_entries or document.patient_entries
        self._doc_class = FHIRdocument

    @property
    def patient_entries(self) -> List[JSON]:
        """
        The list of JSON chunks that are the patient entries
        for the document this sentence belongs to.
        """
        return self._patient_entries

    def yaml_representer(self, dumper):
        """
        Convert into YAML representation
        """
        return dumper.represent_mapping('!FHIRsentence', {
            'original': self.original,
            'patient_entries': json.dumps(self.patient_entries)
        })

    @classmethod
    def yaml_constructor(cls, loader, node):
        """
        Convert from YAML representation
        """
        fields = loader.construct_mapping(node, deep=True)
        return FHIRsentence(original=fields["original"],
                            patient_entries=json.loads(fields["patient_entries"]))

    def __eq__(self, other):
        return isinstance(other, FHIRsentence) and \
               self.original == other.original and \
               self.patient_entries == other.patient_entries

    def __hash__(self):
        return hash((self.original, json.dumps(self.patient_entries)))


ForcedLiteralDumper.add_representer(FHIRsentence,
                                    lambda dumper, sentence:
                                        sentence.yaml_representer(dumper))
yaml.SafeLoader.add_constructor("!FHIRsentence",
                                FHIRsentence.yaml_constructor)


class MDMsentence(TextSentence):
    """
    An MDM sentence is just like a Text Sentence, we just serialize it 
    differently.
    """

    def yaml_representer(self, dumper):
        return dumper.represent_mapping('!MDMsentence', {
            'original': self.original,
            'standalone': self.standalone
        })

    @classmethod
    def yaml_constructor(cls, loader, node):
        fields = loader.construct_mapping(node, deep=True)
        return MDMsentence(**fields)

    def __eq__(self, other):
        return isinstance(other, MDMsentence) and \
               self.original == other.original and \
               self.standalone == other.standalone

    def __str__(self) -> str:
        return f"MDMsentence(original='{self.original}' standalone='{self.standalone}')"

    def __repr__(self) -> str:
        return self.__str__()


ForcedLiteralDumper.add_representer(MDMsentence,
                                    lambda dumper, sentence:
                                        sentence.yaml_representer(dumper))
yaml.SafeLoader.add_constructor('!MDMsentence', MDMsentence.yaml_constructor)


def test_sentence_serialization(document_class: Type, subject_id: str):
    doc = document_class(subject_id=subject_id)
    sent = doc.sentences()[1]
    in_obj = {
        "a": 42,
        "b": sent
    }
    print(f"in_obj: {in_obj}")
    yaml_str = yaml_dump(in_obj)
    print(f"yaml_str: {yaml_str}")
    out_obj = yaml.safe_load(yaml_str)
    print(f"out_obj: {out_obj}")
    assert in_obj == out_obj


def test_text_sentence_serialization():
    test_sentence_serialization(TextDocument, "diabetes")


def test_fhir_sentence_serialization():
    test_sentence_serialization(FHIRdocument, "Austin_McLaughlin")


def test_mdm_sentence_serialization():
    test_sentence_serialization(MDMdocument, "mdm")


if __name__ == "__main__":
    test_text_sentence_serialization()
    test_fhir_sentence_serialization()
    # test_mdm_sentence_serialization()

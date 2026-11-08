# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Efficiently (w/o quadratic inferences) determine how similar
a question is to a set of other questions. We then use this
when combining sets of questions during reconciliation.
"""

from typing import Optional, List, Dict, Tuple
import json
from pathlib import Path

import globals as g

import boto3
import numpy as np
import colorama

colorama.init()

def red(text: str) -> str:
    print(f"{colorama.Fore.RED}{text}{colorama.Style.RESET_ALL}")


class QuestionSimilarity:
    def __init__(self,
                 model_id: Optional[str] = None,
                 max_similarity_threshold: float = 1.0):
        self._model_id: str = model_id or g.EMBEDDING_MODEL_ID
        self._max_similarity_threshold: float = max_similarity_threshold
        self._questions: List[str] = []
        self._q_2_embeddings: Dict[str, "np.ndarray"] = {}
        self._bedrock_runtime = boto3.client("bedrock-runtime",
                                             region_name="us-east-1")
        # self._max_existing_similarity: Optional[float] = None
        self._n_rejections: int = 0
        self._n_stored_questions: int = 0

    def num_questions(self) -> int:
        return len(self._questions)

    def get_questions(self) -> List[str]:
        return list(self._questions) # copy

    @property
    def n_stored_questions(self) -> int:
        return len(self._questions)

    @property
    def n_rejections(self) -> int:
        return self._n_rejections

    @n_rejections.setter
    def n_rejections(self, value: int):
        self._n_rejections = value

    def question_is_sufficiently_diverse(
            self,
            question: str) -> Tuple[bool, np.ndarray, Optional[str]]:
        """
        Check if the question is sufficiently diverse from existing questions.

        Args:
            question: The new question to check (string)
        """
        embedding = self._get_embedding(question)
        similarities = [self.compute_similarity(q, question, embedding)
                        for q in self._questions]
        print(f"similarities: {similarities}")
        max_similarity = max(similarities) if similarities else None
        print(f"max_similarity: {max_similarity}")
        is_diverse = (max_similarity is None or
                      max_similarity < self._max_similarity_threshold)
        if is_diverse:
            return True, embedding, None
        else:
            most_similar_idx = sorted(enumerate(similarities), key=lambda x: x[1], reverse=True)[0][0]
            red(f"most_similar_idx: {most_similar_idx}")
            most_similar_question = self._questions[most_similar_idx]
            return False, embedding, most_similar_question

    def maybe_add_question(self, question: str,
                           warn_on_rejection: bool = False):
        print(f"\nMaybe add question: '{question}'")
        # embedding = self._get_embedding(question)
        # similarities = [self.compute_similarity(q, question, embedding)
        #                 for q in self._questions]
        # print(f"similarities: {similarities}")
        # max_similarity = max(similarities) if similarities else None
        # print(f"max_similarity: {max_similarity}")
        is_diverse, embedding, most_similar_question = \
            self.question_is_sufficiently_diverse(question)
        # if max_similarity is not None and max_similarity > self._max_similarity_threshold:
        if not is_diverse:
            print(f"Rejecting question '{question}' due to high similarity.")
            self.n_rejections += 1
            if warn_on_rejection:
                red(f"WARNING: Question '{question}' should not have been rejected due to similarity.")
                red(f"Most similar existing question: '{most_similar_question}'")
        else:
            self.add_question(question, embedding)


    def add_question(self, question: str, embedding: np.ndarray):
        self._questions.append(question)
        self._q_2_embeddings[question] = embedding
        print(f"Added question '{question}' to the set.")
        self._n_stored_questions += 1

    def _get_embedding(self, question: str) -> list:
        """
        Args:
            question: The text to embed (string)

        Returns:
            List of floats representing the embedding vector
        """
        # Prepare the request body
        body = json.dumps({
            "inputText": question
        })
        try:
            response = self._bedrock_runtime.invoke_model(
                modelId=self._model_id,
                contentType='application/json',
                accept='application/json',
                body=body
            )
            response_body = json.loads(response['body'].read())
            embedding = response_body['embedding']
            return np.array(embedding)
        except Exception as e:
            print(f"Error generating embedding: {str(e)}", flush=True)
            return None
        
    def compute_similarity(self,
                           existing_q: str, 
                           new_q: str,
                           new_q_embedding: Optional[np.ndarray]) -> float:
        """
        Compute a similarity score between two questions.
        This is a placeholder implementation. You can replace it with a more sophisticated method.
        """
        embedding1 = self._q_2_embeddings.get(existing_q)
        embedding2 = self._q_2_embeddings.get(new_q, new_q_embedding)
        if embedding1 is None or embedding2 is None:
            print(f"One of the embeddings is None: existing_q={existing_q}, new_q={new_q}")
            rv = 0.0
        else:
            # Compute cosine similarity between the two embeddings
            dot_product = np.dot(embedding1, embedding2)
            norm1 = np.linalg.norm(embedding1)
            norm2 = np.linalg.norm(embedding2)
            if norm1 == 0 or norm2 == 0:
                rv = 0.0
            else:
                rv = dot_product / (norm1 * norm2)
        print(f"Computed similarity between '{existing_q}' and '{new_q}': {rv}")
        return rv


if __name__ == "__main__":
    qsim = QuestionSimilarity()
    qs = (Path.cwd() / "example-diverse-q-set.txt").read_text().splitlines()
    for q in qs:
        qsim.maybe_add_question(q.strip(), warn_on_rejection=True)
    print(f"Finished; {qsim.n_rejections} rejections, {qsim.n_stored_questions} stored questions.")

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""do_inference retries transient Bedrock errors but not an invalid request."""
import pytest

import utils


class FakeClient:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def converse(self, **_kw):
        self.calls += 1
        if self.errors:
            raise Exception(self.errors.pop(0))
        usage = {"inputTokens": 1, "outputTokens": 1,
                 "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
        return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": usage}


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def make(errors):
        holder["c"] = FakeClient(errors)
        monkeypatch.setattr(utils.boto3, "client", lambda *a, **k: holder["c"])
        monkeypatch.setattr(utils, "sleep", lambda s: None)
        return holder["c"]
    return make


def test_a_validation_exception_is_raised_not_retried(client):
    c = client(["An error occurred (ValidationException) when calling the Converse "
                "operation: The model returned the following errors: Input is too long "
                "for requested model."])
    stats = utils.Statistics()
    with pytest.raises(Exception, match="Input is too long"):
        utils.do_inference("prompt", stats)
    assert c.calls == 1 and stats.n_inference_exceptions == 1


def test_throttling_is_still_retried(client):
    c = client(["An error occurred (ThrottlingException): slow down"])
    assert utils.do_inference("prompt", utils.Statistics()) == "ok"
    assert c.calls == 2

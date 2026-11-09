# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A deployed server (AUTH_REQUIRED) refuses to start without Cognito."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

COGNITO = ("COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_DOMAIN")


def _auth(monkeypatch, **env):
    for name in COGNITO + ("AUTH_REQUIRED",):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import auth
    return importlib.reload(auth)


def test_required_without_cognito_refuses_to_start(monkeypatch):
    auth = _auth(monkeypatch, AUTH_REQUIRED="1", COGNITO_USER_POOL_ID="pool")
    with pytest.raises(RuntimeError, match="COGNITO_CLIENT_ID, COGNITO_DOMAIN"):
        auth.init()


def test_local_without_cognito_runs_without_an_auth_wall(monkeypatch):
    auth = _auth(monkeypatch)
    auth.init()  # no error
    assert auth.AUTH_ENABLED is False and auth.AUTH_REQUIRED is False


def teardown_module(module):
    # Leave the module as the environment configures it for later tests.
    import auth
    importlib.reload(auth)

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""cvc5_path() runs only an absolute, executable file named cvc5*."""
import os

import pytest

from cvc5_script import cvc5_path


def _exe(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_accepts_an_absolute_executable_cvc5(tmp_path, monkeypatch):
    exe = _exe(tmp_path / "cvc5")
    monkeypatch.setenv("CVC5_PATH", str(exe))
    assert cvc5_path() == str(exe)


def test_defaults_to_home_bin_cvc5(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    exe = _exe(tmp_path / "bin" / "cvc5")
    monkeypatch.delenv("CVC5_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cvc5_path() == str(exe)


@pytest.mark.parametrize("value", [
    "cvc5",            # relative: resolved against PATH / cwd
    "/bin/sh",         # another program
    "/nonexistent/cvc5",
])
def test_rejects_anything_else(monkeypatch, value):
    monkeypatch.setenv("CVC5_PATH", value)
    with pytest.raises(ValueError, match="CVC5_PATH"):
        cvc5_path()


def test_rejects_a_non_executable_file(tmp_path, monkeypatch):
    f = tmp_path / "cvc5"
    f.write_text("")
    os.chmod(f, 0o644)
    monkeypatch.setenv("CVC5_PATH", str(f))
    with pytest.raises(ValueError):
        cvc5_path()

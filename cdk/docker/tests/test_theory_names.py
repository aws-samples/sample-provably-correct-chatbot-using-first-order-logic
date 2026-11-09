# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Theory names are unique: a clashing create is refused, and rename checks too."""
import pytest

from test_build_params import store_env  # noqa: F401  (pytest fixture)


def test_create_refuses_a_taken_name_case_insensitively(store_env):  # noqa: F811
    store, jobs, dispatched = store_env
    store.create_theory("Photosynthesis", "One. Two.", doc_type="text")
    dispatched.clear()
    before = dict(jobs.items)
    with pytest.raises(store.TheoryNameTaken, match='"Photosynthesis" already exists'):
        store.create_theory("  photosynthesis ", "Three. Four.", doc_type="text")
    assert jobs.items == before and dispatched == {}


def test_create_trims_the_name(store_env):  # noqa: F811
    store, _jobs, _ = store_env
    out = store.create_theory("  Tides  ", "One. Two.", doc_type="text")
    assert out["theory"]["name"] == "Tides"


def test_rebuild_keeps_its_own_name(store_env):  # noqa: F811
    store, _jobs, _ = store_env
    out = store.create_theory("Tides", "One. Two.", doc_type="text")
    tid = out["theory"]["id"]
    again = store.create_theory("Tides", "One. Two.", doc_type="text", rebuild_of=tid)
    assert again["theory"]["id"] == tid and again["theory"]["name"] == "Tides"


def test_rename_updates_the_theory_and_its_jobs(store_env):  # noqa: F811
    store, jobs, _ = store_env
    out = store.create_theory("Tides", "One. Two.", doc_type="text")
    tid = out["theory"]["id"]
    summary = store.rename_theory(tid, " Ocean tides ")
    assert summary["name"] == "Ocean tides"
    assert store._get_theory_item(tid)["name"] == "Ocean tides"
    assert [j["theory_name"] for j in jobs.items.values()] == ["Ocean tides"]


def test_rename_to_its_own_name_in_another_case_is_allowed(store_env):  # noqa: F811
    store, _jobs, _ = store_env
    tid = store.create_theory("Tides", "One. Two.", doc_type="text")["theory"]["id"]
    assert store.rename_theory(tid, "TIDES")["name"] == "TIDES"


def test_rename_refuses_taken_empty_and_unknown(store_env):  # noqa: F811
    store, _jobs, _ = store_env
    store.create_theory("Tides", "One. Two.", doc_type="text")
    tid = store.create_theory("Waves", "One. Two.", doc_type="text")["theory"]["id"]
    with pytest.raises(store.TheoryNameTaken):
        store.rename_theory(tid, "tides")
    with pytest.raises(ValueError, match="required"):
        store.rename_theory(tid, "   ")
    with pytest.raises(ValueError, match="at most"):
        store.rename_theory(tid, "x" * (store.MAX_THEORY_NAME_LENGTH + 1))
    assert store.rename_theory("no-such-id", "Anything") is None
    assert store._get_theory_item(tid)["name"] == "Waves"

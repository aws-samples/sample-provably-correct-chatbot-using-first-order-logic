"""
Things that we want to use everywhere.
"""

from pathlib import Path
from typing import Optional

from params import Params

DO_USE_OTEL: bool = False
NUM_CORES: int = 96  # c8g.24xlarge
MODEL_ID: str = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
# MODEL_ID: str = "global.anthropic.claude-sonnet-4-6"
# MODEL_ID: str = "global.anthropic.claude-opus-4-5-20251101-v1:0"  # over-throttled: Bedrock converse read-timeouts under batch fan-out (2026-10-15)
# MODEL_ID: str = "global.anthropic.claude-opus-4-6-v1"
# MODEL_ID: str = "us.deepseek.r1-v1:0"
# MODEL_ID: str = "deepseek.v3.2"
# MODEL_ID: str = "us.amazon.nova-premier-v1:0"
# MODEL_ID: str = "us.amazon.nova-2-pro-preview-20251202-v1:0"
FAST_MODEL_ID: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
# Model used for the LIVE question-answering path (question -> SMT-LIB,
# validity repair, and model-output -> natural language). cvc5 is ~0.1% of
# answer latency; the Bedrock calls dominate. Haiku (FAST_MODEL_ID) was tried
# here for speed but regressed open-ended "list all X" formalization (invented
# ungrounded predicates), so this is reverted to MODEL_ID (Opus) pending a
# model that is both fast and robust at predicate reuse.
FORMALIZATION_MODEL_ID: str = MODEL_ID
EMBEDDING_MODEL_ID: str = "amazon.titan-embed-text-v1"
THEORIES_DIR: Path = Path.cwd() / "theories"
SENTENCES_DIR: Path = Path.cwd() / "sentences"
RUNS_DIR: Path = Path.cwd() / "runs"
if not RUNS_DIR.exists():
    RUNS_DIR = Path.cwd().parent / "runs"
    if not RUNS_DIR.exists():
        raise Exception("Can't find runs directory")
STATS: Optional["Statistics"] = None
p: Optional["Params"] = None
subject_type: Optional[str] = None
# Fargate pricing (us-east-1 as of 2026)
cpu_price_per_hour = 0.03238 # dollars
cpu_price_per_second = cpu_price_per_hour / 3600
memory_price_per_hour = 0.00356 # dollars
memory_price_per_second = memory_price_per_hour / 3600

def serialize() -> dict:
    """
    Before forking a process, use this to get something to
    send to the child.
    """
    exclusions = {"Path", "Optional", "serialize", "deserialize"}
    global_vbles = globals().keys() - exclusions
    global_vbles = {k for k in global_vbles if not k.startswith("__")}

    def get_value(name):
        value = globals()[name]
        if name == "p":
            return value.to_dict() if value else None
        else:
            return value

    return {
        name: get_value(name)
        for name in global_vbles
    }


def deserialize(d: dict):
    """
    Call this in a subprocess to make globals() look like the parent
    """
    print(f"deserialize {d}")
    def morph_value(name, value):
        if name == "p":
            return Params.from_dict(value) if value else None
        else:
            return value

    for name, value in d.items():
        globals()[name] = morph_value(name, value)

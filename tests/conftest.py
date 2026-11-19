"""
Make the repo-root modules importable in tests.

globals.py needs a runs/ directory in (or above) the cwd, and prompts.py reads
prompts.yaml / system-prompts.yaml from the cwd, so run the tests from a
throw-away directory that has both. This has to happen before any test module
imports them, i.e. at conftest import time.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_cwd = Path(tempfile.mkdtemp(prefix="chatbot-tests-"))
(_cwd / "runs").mkdir()
(_cwd / "theories").mkdir()
for name in ("prompts.yaml", "system-prompts.yaml"):
    (_cwd / name).symlink_to(REPO_ROOT / name)
os.chdir(_cwd)
os.environ.setdefault("DISABLE_LANGFUSE", "1")
atexit.register(shutil.rmtree, _cwd, ignore_errors=True)

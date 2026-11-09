# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Every endpoint the browser calls must have its own CloudFront behavior.
The default behavior allows only GET/HEAD (and is meant for the SPA), so a
POST to a path missing from additionalBehaviors is refused by CloudFront
(regression: Super Chat's POST /ask-multi had no behavior).
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"
STACK = HERE.parent.parent / "lib" / "chatbot-stack.ts"

# Served through the default behavior on purpose: the SPA and health check.
DEFAULT_BEHAVIOR_PATHS = {"/", "/{full_path:path}", "/healthz"}


def _routes():
    return set(re.findall(r'@app\.(?:get|post|patch|put|delete)\("([^"]+)"',
                          SERVER.read_text()))


def _behaviors():
    block = STACK.read_text().split("additionalBehaviors:", 1)[1].split("},\n    });", 1)[0]
    return set(re.findall(r"'(/[^']*)':", block))


def _covered(route: str, patterns: set) -> bool:
    for p in patterns:
        if p.endswith("/*") and route.startswith(p[:-1]):
            return True
        if route == p:
            return True
    return False


def test_every_endpoint_has_a_cloudfront_behavior():
    patterns = _behaviors()
    assert "/ask" in patterns and "/api/*" in patterns  # the parse found them
    missing = sorted(r for r in _routes() - DEFAULT_BEHAVIOR_PATHS
                     if not _covered(r, patterns))
    assert missing == []

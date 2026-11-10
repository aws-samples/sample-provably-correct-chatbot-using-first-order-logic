# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cognito ID token verification (PyJWT): signature, claims and at_hash."""
import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

POOL, CLIENT, REGION = "us-east-1_TEST", "client-123", "us-east-1"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"


@pytest.fixture(scope="module")
def auth():
    env = {"COGNITO_USER_POOL_ID": POOL, "COGNITO_CLIENT_ID": CLIENT,
           "COGNITO_DOMAIN": "example.auth.us-east-1.amazoncognito.com",
           "COGNITO_REGION": REGION}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    import auth as module
    module = importlib.reload(module)
    yield module
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(module)


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update(kid="k1", alg="RS256", use="sig")
    return private, {"keys": [public_jwk]}


def token(private, auth, access="access-tok", kid="k1", **overrides):
    now = int(time.time())
    claims = {"sub": "u1", "email": "a@b.c", "aud": CLIENT, "iss": ISSUER,
              "iat": now, "exp": now + 300, "token_use": "id",  # nosec B105 (a JWT claim value, not a password)
              "at_hash": auth._at_hash(access)}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


def test_a_valid_token_verifies(auth, keys):
    private, jwks = keys
    claims = auth.verify_id_token(token(private, auth), "access-tok", jwks)
    assert claims["sub"] == "u1" and claims["email"] == "a@b.c"


@pytest.mark.parametrize("overrides, access, message", [
    ({"aud": "someone-else"}, "access-tok", "(?i)audience"),
    ({"iss": "https://evil.example"}, "access-tok", "issuer"),
    ({"exp": 1}, "access-tok", "expired"),
    ({"token_use": "access"}, "access-tok", "not an ID token"),  # nosec B105 (JWT claim value)
    ({}, "another-access-token", "at_hash"),
    ({}, None, "at_hash"),
])
def test_a_bad_token_is_refused(auth, keys, overrides, access, message):
    private, jwks = keys
    with pytest.raises(Exception, match=message):
        auth.verify_id_token(token(private, auth, **overrides), access, jwks)


def test_an_unknown_key_or_another_signer_is_refused(auth, keys):
    private, jwks = keys
    with pytest.raises(ValueError, match="unknown signing key"):
        auth.verify_id_token(token(private, auth, kid="nope"), "access-tok", jwks)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        auth.verify_id_token(token(other, auth), "access-tok", jwks)


def test_an_hmac_token_signed_with_the_public_key_is_refused(auth, keys):
    """The python-jose flaw (CVE-2024-33663): HS256 keyed with the public key."""
    private, jwks = keys
    now = int(time.time())

    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    # Built by hand: PyJWT itself refuses to sign with an asymmetric key.
    pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    body = b64(json.dumps({"sub": "x", "aud": CLIENT, "iss": ISSUER, "iat": now,
                           "exp": now + 300, "token_use": "id"}).encode())  # nosec B105 (a JWT claim value)
    sig = b64(hmac.new(pem, head + b"." + body, hashlib.sha256).digest())
    forged = (head + b"." + body + b"." + sig).decode()
    with pytest.raises(jwt.InvalidAlgorithmError):
        auth.verify_id_token(forged, None, jwks)

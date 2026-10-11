import hashlib
import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

KEYS = {}


def proof_for(request, key, token="", *, jti=None, issued=None):
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    body = request.read()
    return jwt.encode(
        {
            "jti": jti or uuid.uuid4().hex,
            "iat": time.time() if issued is None else issued,
            "htm": request.method,
            "htu": request.url.raw_path.decode(),
            "bht": hashlib.sha256(body).hexdigest(),
            "ath": hashlib.sha256(token.encode()).hexdigest(),
        },
        key,
        algorithm="ES256",
        headers={"typ": "rage-proof+jwt", "jwk": public},
    )


class SignedClient(TestClient):
    """Real test signatures on exact httpx body bytes; never disables server proof validation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.login_key = None

    def request(self, method, url, **kwargs):
        if str(url).endswith(("/auth/login", "/auth/register")) and "json" in kwargs:
            response = super().request("GET", "/api/v1/auth/challenge")
            if response.status_code != 200:
                return response
            kwargs["json"] = {**kwargs["json"], "challenge": response.json()["challenge"]}
            self.login_key = self.key
        return super().request(method, url, **kwargs)

    def send(self, request, **kwargs):
        path = request.url.path
        key = None
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if path.endswith(("/auth/login", "/auth/register")):
            key = self.key
            token = ""
        elif path.endswith("/auth/refresh"):
            token = json.loads(request.read())["refresh_token"]
            key = KEYS.get(token, self.key)
        elif token in KEYS:
            key = KEYS[token]
        if key and "X-Request-Proof" not in request.headers:
            request.headers["X-Request-Proof"] = proof_for(request, key, token)
        response = super().send(request, **kwargs)
        if path.endswith(("/auth/login", "/auth/refresh")) and response.status_code == 200:
            data = response.json()
            KEYS[data["access_token"]] = key
            KEYS[data["refresh_token"]] = key
        return response

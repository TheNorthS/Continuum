"""Python API client using the same signed-request contract as the browser."""

import hashlib
import json
import time
import uuid

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec


class SecurityClient:
    def __init__(self, base_url, **kwargs):
        self.http = httpx.Client(base_url=base_url, **kwargs)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.access = ""
        self.refresh_token = ""

    def sign(self, request, token=""):
        proof = {
            "htm": request.method,
            "htu": request.url.raw_path.decode(),
            "iat": time.time(),
            "jti": uuid.uuid4().hex,
            "bht": hashlib.sha256(request.read()).hexdigest(),
            "ath": hashlib.sha256(token.encode()).hexdigest(),
        }
        public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        request.headers["X-Request-Proof"] = jwt.encode(
            proof, self.key, algorithm="ES256", headers={"typ": "rage-proof+jwt", "jwk": public}
        )
        if token:
            request.headers["Authorization"] = "Bearer " + token
        return request

    def login(self, username, password):
        challenge = self.http.get("/auth/challenge")
        challenge.raise_for_status()
        request = self.http.build_request(
            "POST",
            "/auth/login",
            json={"username": username, "password": password, "challenge": challenge.json()["challenge"]},
        )
        response = self.http.send(self.sign(request))
        response.raise_for_status()
        self.access, self.refresh_token = response.json()["access_token"], response.json()["refresh_token"]
        return response.json()["user"]

    def request(self, method, path, **kwargs):
        request = self.http.build_request(method, path, **kwargs)
        response = self.http.send(self.sign(request, self.access))
        if response.status_code == 401 and self.refresh_token:
            refresh = self.http.build_request(
                "POST", "/auth/refresh", json={"refresh_token": self.refresh_token}
            )
            result = self.http.send(self.sign(refresh, self.refresh_token))
            if result.status_code == 200:
                self.access, self.refresh_token = (
                    result.json()["access_token"],
                    result.json()["refresh_token"],
                )
                response = self.http.send(self.sign(request, self.access))
        return response

    def close(self):
        self.http.close()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

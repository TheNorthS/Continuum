"""Install the pinned upstream release; Docker Hub images were removed upstream."""

import hashlib
import sys
import time
import urllib.request
from pathlib import Path

VERSION = "RELEASE.2024-12-18T13-15-44Z"
CHECKSUMS = {
    "amd64": "88182336ab5793488f2caad54846056b330f0dcb5c488b60d52434beeebae3ca",
    "arm64": "455c9b3093b7c1e6153c85cfd745c7c401a92497346ba24dbe45775c7b3ab1cd",
}
ASSETS = {"amd64": 404668345, "arm64": 404668139}
arch = sys.argv[1]
expected = CHECKSUMS[arch]
url = f"https://api.github.com/repos/minio/minio/releases/assets/{ASSETS[arch]}"
target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/usr/local/bin/minio")
target.parent.mkdir(parents=True, exist_ok=True)
digest = hashlib.sha256()
started = time.monotonic()
received = 0
next_progress = 16 * 1024 * 1024
request = urllib.request.Request(
    url, headers={"Accept": "application/octet-stream", "User-Agent": "RAG-E-build"}
)
with (
    urllib.request.urlopen(request, timeout=120) as response,
    target.open("wb") as output,
):
    while block := response.read(1024 * 1024):
        if time.monotonic() - started > 1800:
            raise TimeoutError("MinIO download exceeded 30 minutes")
        digest.update(block)
        output.write(block)
        received += len(block)
        if received >= next_progress:
            print(f"Downloaded MinIO: {received // (1024 * 1024)} MiB", flush=True)
            next_progress += 16 * 1024 * 1024
if digest.hexdigest() != expected:
    target.unlink()
    raise SystemExit(
        f"MinIO SHA-256 mismatch: received={received}, actual={digest.hexdigest()}, expected={expected}"
    )
target.chmod(0o755)

"""Read-only checks on Git-visible files; never print suspected secret values."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "provider-token": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws-access-key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
}
PRIVATE_PARTS = {".secrets", ".cache", ".venv", "node_modules", "__pycache__", "test-results"}
PRIVATE_PATHS = {
    "config/rag.production.json",
    "config/evaluation.current.json",
    "k8s/secrets.yaml",
    "deploy/prometheus/metrics.token",
}


def git_files(root):
    """Include tracked files even if newly ignored; use temporary metadata before init."""
    found = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode == 0 and Path(found.stdout.strip()).resolve() != root:
        raise ValueError("Run from an independent Continuum repository, not a parent repository")
    with tempfile.TemporaryDirectory(prefix="continuum-publication-") as temporary:
        command = ["git", "-c", "core.quotepath=false", "-c", f"core.excludesFile={os.devnull}"]
        if found.returncode:
            command += ["--git-dir", str(Path(temporary) / "repo.git"), "--work-tree", str(root)]
            subprocess.run(command + ["init", "--quiet"], cwd=root, check=True, capture_output=True)
        raw = subprocess.check_output(
            command + ["ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
    return sorted({p.decode("utf-8") for p in raw.split(b"\0") if p})


def protected(path):
    parts = Path(path).parts
    name = Path(path).name
    return (
        bool(PRIVATE_PARTS.intersection(parts))
        or path in PRIVATE_PATHS
        or path.startswith(("docs/local/", "data/legacy/", "data/documents/md/"))
        or (name.startswith(".env") and not name.endswith(".example"))
        or name == "keyring.json"
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3"))
    )


def check(root):
    names = git_files(root)
    visible = {(root / name).resolve() for name in names}
    issues, total = [], 0
    for name in names:
        path = root / name
        if protected(name):
            issues.append({"file": name, "kind": "protected-path"})
            continue
        if path.is_symlink():
            issues.append({"file": name, "kind": "symlink-review-required"})
            continue
        if not path.is_file():
            issues.append({"file": name, "kind": "missing-tracked-file"})
            continue
        size = path.stat().st_size
        total += size
        if size > 10 * 1024 * 1024:
            issues.append({"file": name, "kind": "file-over-10-MiB"})
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(source):
                issues.append({"file": name, "line": source.count("\n", 0, match.start()) + 1, "kind": kind})
        if path.suffix.lower() != ".md":
            continue
        # Ignore examples inside fenced code; inspect real Markdown and HTML links.
        prose = re.sub(r"(?ms)^```.*?^```[^\n]*", "", source)
        links = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", prose)
        links += re.findall(r"""(?:href|src)=["']([^"']+)["']""", prose)
        for target in links:
            target = target.strip().split(' "', 1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith(("#", "//")):
                continue
            linked = (path.parent / unquote(parsed.path)).resolve()
            available = linked in visible or (
                linked.is_dir() and any(p.is_relative_to(linked) for p in visible)
            )
            if not available:
                issues.append({"file": name, "kind": "link-not-in-publication", "target": target})
    return {"files": len(names), "bytes": total, "issues": issues, "ok": not issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    report = check(args.root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

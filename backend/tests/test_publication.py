import importlib.util
import subprocess

from app.config import ROOT

spec = importlib.util.spec_from_file_location("publication_check", ROOT / "scripts/check_publication.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def test_clean_export_links_ignore_private_files_without_creating_repository(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\ntest-results/\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE=true", encoding="utf-8")
    (tmp_path / "guide.md").write_text("Guide", encoding="utf-8")
    (tmp_path / "README.md").write_text("[Guide](guide.md)", encoding="utf-8")
    before = set(tmp_path.iterdir())
    assert publication.check(tmp_path)["ok"]
    assert set(tmp_path.iterdir()) == before
    (tmp_path / "README.md").write_text("[Local](test-results/report.md)", encoding="utf-8")
    report = publication.check(tmp_path)
    assert not report["ok"]
    assert report["issues"][0]["kind"] == "link-not-in-publication"


def test_already_tracked_private_file_is_detected_even_after_ignore(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".env").write_text("PRIVATE=true", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".env"], check=True)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    report = publication.check(tmp_path)
    assert {"file": ".env", "kind": "protected-path"} in report["issues"]


def test_suspected_token_is_reported_without_value(tmp_path):
    token = "sk-" + "x" * 32
    (tmp_path / "accidental.txt").write_text(token, encoding="utf-8")
    report = publication.check(tmp_path)
    assert not report["ok"]
    assert report["issues"][0]["kind"] == "provider-token"
    assert token not in str(report)

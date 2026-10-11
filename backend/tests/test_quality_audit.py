from app.evaluation.quality_audit import make_matcher, matching_anchors, normalize_source, source_anchors


def test_reference_anchors_ignore_header_and_normalize_spacing_without_qa_injection():
    content = "甲乙丙丁" * 80 + "终点"
    anchors = source_anchors("来源：新闻站，正文：" + content)
    assert len(anchors) <= 5 and all(len(anchor) == 48 for anchor in anchors)
    assert all("来源" not in anchor for anchor in anchors)
    assert normalize_source("甲\\n乙，Ａ。") == "甲乙A"
    matcher = make_matcher({"reference": {"anchors": anchors}})
    assert matching_anchors(matcher, content)["reference"] == set(range(len(anchors)))
    assert matching_anchors(matcher, "unrelated public article") == {}


def test_auxiliary_scope_is_explicit_and_not_injected_from_qa(tmp_path):
    import json

    from app.evaluation.full_data import full_crud

    folder = tmp_path / "crud/full-corpus"
    folder.mkdir(parents=True)
    for part in range(1, 16):
        for section in range(1, 4):
            (folder / f"documents_dup_part_{part}_part_{section}").write_text(
                "common source\n", encoding="utf-8"
            )
    for part in range(1, 4):
        (folder / f"documents_hallu.txt_part_{part}").write_text(
            f"auxiliary source {part}\n", encoding="utf-8"
        )
    tasks = ("questanswer_1doc", "questanswer_2docs", "questanswer_3docs")
    data = {
        task: [
            {"ID": "event", "questions": "question", "answers": "NEVER INDEX ANSWER", "news1": "reference"}
        ]
        for task in tasks
    }
    (tmp_path / "crud/split_merged.json").write_text(json.dumps(data), encoding="utf-8")
    legacy = full_crud(tmp_path)
    full = full_crud(tmp_path, include_auxiliary=True)
    assert len(legacy.corpus) == 1 and len(full.corpus) == 4
    assert legacy.metadata["corpus_partition_count"] == 45
    assert full.metadata["corpus_partition_count"] == 48
    assert legacy.queries == full.queries
    assert all("NEVER INDEX" not in text and text != "reference" for text in full.corpus.values())

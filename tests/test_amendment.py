import json

import pytest

from app import store
from app.errors import BusinessError
from app.pipeline import run, sample_text
from app.exporter import evidence_csv


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    return store.save(sample_text(), run(sample_text()))


def _split(record_ids=("00001",), issues=("1", "2"), bindings=("B01A", "B01B")):
    return {"record_ids": list(record_ids),
            "segments": [{"issues": issue, "binding": binding}
                         for issue, binding in zip(issues, bindings)]}


def test_current_derives_from_original(batch):
    base = batch["result"]
    updated, outcome = store.amend_batch(batch["id"], 1, [_split()], note="拆为上下册")
    assert outcome["status"] == "applied"
    # 原批次不可变，当前批次从原批次派生
    assert updated["base_result"]["books"] == base["books"] == 40
    assert updated["result"]["books"] == 41
    assert updated["result"]["held"][0]["record_id"] == "00001"
    assert updated["result"]["held"][1]["record_id"] == "00001-2"


def test_merge_reverses_split(batch):
    store.amend_batch(batch["id"], 1, [_split()])
    merged, outcome = store.amend_batch(batch["id"], 2, [{
        "record_ids": ["00001", "00001-2"],
        "segments": [{"issues": "1-2", "binding": "B01M"}],
    }], note="合回一册")
    assert outcome["status"] == "applied"
    assert merged["result"]["books"] == 40
    assert merged["comparison"]["metrics"][0] == {"metric": "实体册", "original": 40, "current": 40, "delta": 0}


def test_amendment_cannot_change_coverage(batch):
    with pytest.raises(BusinessError) as caught:
        store.amend_batch(batch["id"], 1, [{
            "record_ids": ["00001"],
            "segments": [{"issues": "1-3", "binding": "X"}],
        }])
    assert caught.value.code == "AMEND_COVERAGE"
    assert store.get(batch["id"])["result"]["books"] == 40


def test_replay_same_revision_is_idempotent(batch):
    first, _ = store.amend_batch(batch["id"], 1, [_split()])
    again, outcome = store.amend_batch(batch["id"], 1, [_split()])
    assert outcome["status"] == "replayed"
    assert again["revision"] == first["revision"]
    assert len(again["amendments"]) == 1
    assert again["result"]["books"] == 41


def test_same_revision_different_value_quarantined(batch):
    store.amend_batch(batch["id"], 1, [_split()])
    with pytest.raises(BusinessError) as caught:
        store.amend_batch(batch["id"], 1, [{
            "record_ids": ["00001"],
            "segments": [{"issues": "1", "binding": "ZZ"}, {"issues": "2", "binding": "YY"}],
        }])
    assert caught.value.code == "AMEND_CONFLICT"
    item = store.get(batch["id"])
    assert item["result"]["books"] == 41
    assert [a["status"] for a in item["amendments"]].count("quarantined") == 1
    assert item["amendment_revision"] == 1


def test_late_revision_does_not_roll_back(batch):
    store.amend_batch(batch["id"], 2, [_split()])
    with pytest.raises(BusinessError) as caught:
        store.amend_batch(batch["id"], 1, [_split(bindings=("OLD1", "OLD2"))])
    assert caught.value.code == "AMEND_STALE"
    item = store.get(batch["id"])
    assert item["result"]["books"] == 41
    assert item["amendment_revision"] == 2
    assert item["result"]["held"][0]["binding"] == "B01A"


def test_signed_review_survives_later_revision(batch):
    store.amend_batch(batch["id"], 1, [_split()])
    reviewed = store.review(batch["id"], 0, "通过")
    assert reviewed["confirmed"]["amendment_revision"] == 1
    assert reviewed["confirmed"]["snapshot"]["books"] == 41
    merged, outcome = store.amend_batch(batch["id"], 2, [{
        "record_ids": ["00001", "00001-2"],
        "segments": [{"issues": "1-2", "binding": "B01M"}],
    }])
    assert outcome["status"] == "applied"
    # 已签复核的册数与覆盖凭据不被后来的修改抹掉
    assert merged["confirmed"]["snapshot"]["books"] == 41
    assert merged["confirmed"]["snapshot"]["covered"] == 44
    assert merged["result"]["books"] == 40
    assert merged["confirmed_current"] is False


def test_review_write_failure_keeps_last_step(batch, monkeypatch):
    store.amend_batch(batch["id"], 1, [_split()])
    store.review(batch["id"], 0, "通过")

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_snapshot", broken)
    with pytest.raises(BusinessError) as caught:
        store.review(batch["id"], 1, "退回")
    assert caught.value.code == "REVIEW_WRITE"
    item = store.get(batch["id"])
    assert item["revision"] == 1
    assert item["confirmed"]["decision"] == "通过"
    assert item["confirmed"]["amendment_revision"] == 1


def test_amend_write_failure_is_atomic(batch, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_insert_record", broken)
    with pytest.raises(BusinessError) as caught:
        store.amend_batch(batch["id"], 1, [_split()])
    assert caught.value.code == "AMEND_WRITE"
    item = store.get(batch["id"])
    assert item["result"]["books"] == 40
    assert item["amendment_revision"] == 0
    assert item["amendments"] == []


def test_switch_failure_shows_last_review(batch, monkeypatch):
    store.amend_batch(batch["id"], 1, [_split()])
    store.review(batch["id"], 0, "通过")

    def broken(*args, **kwargs):
        raise BusinessError("AMEND_TARGET", "修订目标不存在")

    monkeypatch.setattr("app.amend.derive", broken)
    item = store.get(batch["id"])
    assert item["derivation"]["status"] == "failed"
    assert item["result"]["books"] == 41          # 来自已签复核快照
    assert item["confirmed"]["snapshot"]["books"] == 41
    with pytest.raises(BusinessError) as caught:
        store.review(batch["id"], 1, "退回")
    assert caught.value.code == "DERIVATION"


def test_comparison_lists_both_batches(batch):
    store.amend_batch(batch["id"], 1, [_split()])
    item = store.get(batch["id"])
    metrics = {m["metric"]: m for m in item["comparison"]["metrics"]}
    assert metrics["实体册"]["original"] == 40 and metrics["实体册"]["current"] == 41
    assert metrics["覆盖期次"]["original"] == metrics["覆盖期次"]["current"] == 44
    assert item["comparison"]["journals"]


def test_evidence_exports_both_batches(batch):
    store.amend_batch(batch["id"], 1, [_split()], note="拆册")
    store.review(batch["id"], 0, "通过")
    text = evidence_csv(store.get(batch["id"]))
    for kind in ("manifest", "original_manifest", "comparison", "comparison_journal", "amendment", "review"):
        assert kind in text
    assert "拆册" in text


def test_amend_route_matches_store(batch):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    response = client.post("/api/amend", json={"id": batch["id"], "revision": 1,
                                               "changes": [_split()], "note": "接口"})
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    assert response.json()["run"]["result"]["books"] == 41
    compare = client.get(f"/api/compare/{batch['id']}").json()["comparison"]
    assert compare["metrics"][0]["current"] == 41


def test_page_renders_side_by_side(batch):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    for compare in ("base", "current"):
        page = client.get(f"/module/6?run_id={batch['id']}&compare={compare}")
        assert page.status_code == 200
        assert "原批次" in page.text and "当前批次" in page.text
        assert "并排核对" in page.text

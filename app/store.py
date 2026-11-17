import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.config import DATA_DIR, RULE_VERSION
from app.errors import BusinessError
from app import amend

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    result TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT '待复核',
    created TEXT NOT NULL,
    current_amendment INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS amendments (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS amendments_run ON amendments(run_id, revision, seq);
CREATE TABLE IF NOT EXISTS reviews (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision TEXT NOT NULL,
    amendment_revision INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_run ON reviews(run_id, seq);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "runs.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
    if "current_amendment" not in columns:
        db.execute("ALTER TABLE runs ADD COLUMN current_amendment INTEGER NOT NULL DEFAULT 0")
    review_columns = {row["name"] for row in db.execute("PRAGMA table_info(reviews)")}
    if "result" not in review_columns:
        db.execute("ALTER TABLE reviews ADD COLUMN result TEXT NOT NULL DEFAULT '{}'")
    db.commit()
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _result_digest(result):
    material = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def save(text, result):
    digest = hashlib.sha256((RULE_VERSION + "\n" + text).encode()).hexdigest()
    ident = digest[:16]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,digest,result,created) VALUES(?,?,?,?)",
                   (ident, digest, json.dumps(dict(result, rule_version=RULE_VERSION), ensure_ascii=False), _now()))
    return get(ident)


def _load_run(db, ident):
    row = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise BusinessError("NOT_FOUND", "批次不存在", 404)
    item = dict(row)
    item["result"] = json.loads(item["result"])
    item["rule_version"] = item["result"].get("rule_version", RULE_VERSION)
    return item


def _segment_label(change):
    parts = []
    for segment in change.get("segments", []):
        issues = segment.get("issues", [])
        if all(i.isdigit() for i in issues) and issues == [str(n) for n in range(int(issues[0]), int(issues[-1]) + 1)]:
            text = issues[0] if len(issues) == 1 else "%s-%s" % (issues[0], issues[-1])
        else:
            text = "/".join(issues)
        parts.append("%s→%s" % (text, segment.get("binding", "")))
    return "；".join(parts)


def _amendment_rows(db, ident):
    records = []
    for row in db.execute("SELECT * FROM amendments WHERE run_id=? ORDER BY revision, seq", (ident,)):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        changes = item["payload"].get("changes", [])
        item["target_label"] = "；".join(_target_label(c["target"]) for c in changes) if changes else ""
        item["segment_label"] = "；".join(_segment_label(c) for c in changes) if changes else ""
        records.append(item)
    return records


def _effective(records):
    effective = []
    for record in records:
        if record["status"] != "applied":
            continue
        for position, change in enumerate(record["payload"].get("changes", [])):
            effective.append({"revision": record["revision"], "seq": record["seq"], "position": position,
                              "target": change["target"], "segments": change["segments"],
                              "note": change.get("note", "")})
    return effective


def _review_rows(db, ident):
    return [dict(row) for row in db.execute("SELECT * FROM reviews WHERE run_id=? ORDER BY seq", (ident,))]


def _snapshot(result):
    gaps = [{"issn": g["issn"], "year": g["year"], "title": g["title"],
             "covered": list(g["covered"]), "missing": list(g["missing"])} for g in result.get("gaps", [])]
    return {
        "books": result.get("books", 0),
        "covered": sum(len(r["issues"]) for r in result.get("held", [])),
        "missing": sum(len(g["missing"]) for g in result.get("gaps", [])),
        "tasks": len(result.get("final", [])),
        "gaps": gaps,
        "conflicts": len(result.get("binding_conflicts", [])) + len(result.get("identity_conflicts", [])),
        "result_digest": _result_digest(result),
    }


def _derive_or_recover(base_result, records):
    effective = _effective(records)
    try:
        current = amend.derive(base_result, effective)
        return current, {"status": "ok", "error": ""}
    except BusinessError as error:
        return None, {"status": "failed", "error": str(error)}
    except Exception as error:  # 派生异常一律视为切换失败，保留最后已签复核
        return None, {"status": "failed", "error": str(error)}


def _target_label(target):
    if "record_ids" in target:
        return "、".join(target["record_ids"])
    scope = target.get("scope", {})
    return "%s/%s/复本%s" % (scope.get("issn", ""), scope.get("year", ""), scope.get("copy", ""))


def get(ident):
    with connect() as db:
        item = _load_run(db, ident)
        base_result = item["result"]
        records = _amendment_rows(db, ident)
        reviews = _review_rows(db, ident)

    current, derivation = _derive_or_recover(base_result, records)
    confirmed = None
    if reviews:
        last = reviews[-1]
        confirmed = dict(last)
        confirmed["snapshot"] = json.loads(last["snapshot"])
        confirmed["result"] = json.loads(last.get("result") or "{}")
    if current is None:
        # 切换失败：仍显示已签复核的结果；没有任何复核时才退回原批次。
        current = (confirmed or {}).get("result") or base_result
        if not (confirmed or {}).get("result"):
            derivation = dict(derivation, fallback="base")

    applied = [r for r in records if r["status"] == "applied"]
    item["base_result"] = base_result
    item["result"] = current
    item["amendments"] = records
    item["applied_amendments"] = applied
    item["amendment_revision"] = max([r["revision"] for r in applied], default=0)
    item["derivation"] = derivation
    item["reviews"] = [{**r, "snapshot": json.loads(r["snapshot"])} for r in reviews]
    item["confirmed"] = confirmed
    item["comparison"] = amend.comparison(base_result, current)
    item["confirmed_current"] = bool(
        confirmed and derivation["status"] == "ok"
        and confirmed["amendment_revision"] == item["amendment_revision"])
    item["blocked"] = bool(current.get("blocked"))
    return item


def history():
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,revision,decision,current_amendment,created FROM runs ORDER BY created DESC LIMIT 40")]


def _insert_record(db, ident, revision, digest, payload, status, note):
    existing = db.execute(
        "SELECT seq FROM amendments WHERE run_id=? AND revision=? AND digest=? AND status=?",
        (ident, revision, digest, status)).fetchone()
    if existing:
        return existing["seq"], False
    cursor = db.execute(
        "INSERT INTO amendments(run_id,revision,digest,payload,status,note,created) VALUES(?,?,?,?,?,?,?)",
        (ident, revision, digest, json.dumps(payload, ensure_ascii=False), status, note, _now()))
    return cursor.lastrowid, True


def amend_batch(ident, revision, changes, note=""):
    """登记一条补录修订并按修订号裁决。

    - 同号同值：重办，不生成新版本；
    - 同号异值：隔离，当前事实不变；
    - 低于当前已生效修订：晚到旧修订，不得回退当前事实；
    - 更高修订号：从原批次重放校验通过后原子写入。
    """
    if type(revision) is not int or revision < 0:
        raise BusinessError("REVISION", "修订号须为非负整数")
    normalized = amend.normalize_changes(changes, revision)
    if not isinstance(note, str):
        raise BusinessError("AMEND_NOTE", "备注须为文本")
    digest = amend.submission_digest(normalized, note)

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            item = _load_run(db, ident)
            base_result = item["result"]
            records = _amendment_rows(db, ident)
            applied = [r for r in records if r["status"] == "applied"]
            current_revision = max([r["revision"] for r in applied], default=0)

            same = [r for r in records if r["revision"] == revision]
            if revision < current_revision:
                if not any(r["digest"] == digest and r["status"] == "late" for r in same):
                    _insert_record(db, ident, revision, digest, {"changes": normalized, "note": note}, "late", note)
                db.commit()
                raise BusinessError("AMEND_STALE", "晚到的修订 %d 低于当前修订 %d，不能回退当前事实"
                                    % (revision, current_revision))
            if same:
                applied_same = [r for r in same if r["status"] == "applied"]
                if applied_same and applied_same[0]["digest"] == digest:
                    db.commit()
                    return get(ident), {"status": "replayed", "seq": applied_same[0]["seq"], "revision": revision}
                if not any(r["digest"] == digest and r["status"] == "quarantined" for r in same):
                    _insert_record(db, ident, revision, digest, {"changes": normalized, "note": note},
                                   "quarantined", note)
                db.commit()
                raise BusinessError("AMEND_CONFLICT", "修订 %d 已存在不同内容，已隔离待裁决" % revision, 409)

            candidate = [dict(c) for c in _effective(records)]
            candidate += [{"revision": revision, "seq": 0, "position": position,
                           "target": c["target"], "segments": c["segments"], "note": c["note"]}
                          for position, c in enumerate(normalized)]
            # 先完整重放校验，任何目标失效都不落盘
            amend.derive(base_result, candidate)
            seq, _ = _insert_record(db, ident, revision, digest, {"changes": normalized, "note": note},
                                    "applied", note)
            db.execute("UPDATE runs SET current_amendment=? WHERE id=?", (revision, ident))
            db.commit()
            return get(ident), {"status": "applied", "seq": seq, "revision": revision}
        except BusinessError:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise BusinessError("AMEND_WRITE", "修订写入失败，已回到最后确认的步骤", 500)


def review(ident, revision, decision):
    if not isinstance(decision, str) or decision not in {"通过", "退回"}:
        raise BusinessError("DECISION", "复核决定无效")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            item = _load_run(db, ident)
            if item["revision"] != revision:
                raise BusinessError("STALE", "版本已更新，请刷新", 409)
            base_result = item["result"]
            records = _amendment_rows(db, ident)
            current, derivation = _derive_or_recover(base_result, records)
            if derivation["status"] != "ok":
                raise BusinessError("DERIVATION", "当前批次切换失败，仍保留原复核：%s" % derivation["error"], 409)
            if decision == "通过" and current.get("blocked"):
                raise BusinessError("BLOCKED", "存在阻断项，请先修正数据", 409)
            applied = [r for r in records if r["status"] == "applied"]
            amendment_revision = max([r["revision"] for r in applied], default=0)
            db.execute(
                "INSERT INTO reviews(run_id,revision,decision,amendment_revision,snapshot,result,created)"
                " VALUES(?,?,?,?,?,?,?)",
                (ident, revision + 1, decision, amendment_revision,
                 json.dumps(_snapshot(current), ensure_ascii=False),
                 json.dumps(current, ensure_ascii=False), _now()))
            db.execute("UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",
                       (decision, ident, revision))
            db.commit()
        except BusinessError:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise BusinessError("REVIEW_WRITE", "复核写入失败，已保留最后确认的步骤", 500)
    return get(ident)


def amend_history(ident):
    item = get(ident)
    return {"id": item["id"], "revision": item["amendment_revision"],
            "derivation": item["derivation"], "amendments": item["amendments"]}

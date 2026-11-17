"""馆藏装订事实的补录修订。

原批次（base）一经载入即不可变；合并/拆分等装订事实变更以修订号追加，
当前结果始终从原批次重放生效修订派生。这里只做纯计算：
目标解析、单条修订应用、重放派生与并排比对。
修订号的裁决、版本与复核快照的持久化在 app.store。
"""
import hashlib
import json

from app.errors import BusinessError
from app.modules.m3_issues.rules import expand
from app.report import report


class TargetMissing(BusinessError):
    """修订指向的记录已不存在（被更晚的生效修订替换）。"""

    def __init__(self, message="修订目标不存在"):
        super().__init__("AMEND_TARGET", message)


def _text(value):
    return "" if value is None else str(value).strip()


def normalize_segments(segments):
    if not isinstance(segments, list) or not segments:
        raise BusinessError("AMEND_SEGMENTS", "修订须给出至少一个装订分段")
    out, seen = [], set()
    for position, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise BusinessError("AMEND_SEGMENT", "装订分段须为对象")
        raw = segment.get("issues", segment.get("issue"))
        if raw is None:
            raise BusinessError("AMEND_ISSUE", "装订分段缺少期号")
        values = raw if isinstance(raw, list) else [raw]
        issues = []
        for value in values:
            if not isinstance(value, str):
                raise BusinessError("AMEND_ISSUE", "期号须为文本")
            try:
                issues.extend(expand(value))
            except BusinessError as error:
                raise BusinessError("AMEND_ISSUE", "期号表达无效：%s" % error)
        if not issues:
            raise BusinessError("AMEND_ISSUE", "装订分段缺少期号")
        for issue in issues:
            if issue in seen:
                raise BusinessError("AMEND_OVERLAP", "同一修订内期号 %s 重复覆盖" % issue)
            seen.add(issue)
        binding = _text(segment.get("binding"))
        if not binding:
            raise BusinessError("BINDING_REQUIRED", "在馆分段缺少装订号")
        status = _text(segment.get("status")) or "held"
        if status != "held":
            raise BusinessError("AMEND_STATUS", "补录修订只处理在馆装订事实")
        record_id = _text(segment.get("record_id")) or None
        if record_id is not None and not record_id:
            raise BusinessError("AMEND_ID", "记录号不能为空")
        out.append({
            "position": position,
            "issues": issues,
            "binding": binding,
            "status": status,
            "record_id": record_id,
            "note": _text(segment.get("note")),
        })
    return out


def normalize_change(change, revision, position):
    if not isinstance(change, dict):
        raise BusinessError("AMEND_CHANGE", "修订条目须为对象")
    declared = change.get("revision")
    if declared is not None and declared != revision:
        raise BusinessError("AMEND_REVISION", "条目修订号须与批次修订号一致")
    record_ids = change.get("record_ids")
    if record_ids is not None:
        if isinstance(record_ids, str):
            record_ids = [record_ids]
        if not isinstance(record_ids, list) or not record_ids:
            raise BusinessError("AMEND_TARGET", "记录号列表不能为空")
        cleaned = []
        for value in record_ids:
            if not isinstance(value, str) or not value.strip():
                raise BusinessError("AMEND_TARGET", "记录号须为非空文本")
            cleaned.append(value.strip())
        if len(set(cleaned)) != len(cleaned):
            raise BusinessError("AMEND_TARGET", "记录号重复")
        target = {"record_ids": cleaned}
    else:
        issn = _text(change.get("issn"))
        year = _text(change.get("year"))
        copy = _text(change.get("copy"))
        if not (issn and year and copy):
            raise BusinessError("AMEND_TARGET", "修订须指定记录号或ISSN、年度、复本")
        target = {"scope": {"issn": issn, "year": year, "copy": copy}}
    return {
        "position": position,
        "revision": revision,
        "target": target,
        "segments": normalize_segments(change.get("segments")),
        "note": _text(change.get("note")),
    }


def normalize_changes(changes, revision):
    if not isinstance(changes, list) or not changes:
        raise BusinessError("AMEND_CHANGES", "修订须包含至少一条记录变更")
    return [normalize_change(change, revision, position) for position, change in enumerate(changes)]


def submission_digest(changes, note=""):
    material = json.dumps({"changes": changes, "note": note}, sort_keys=True,
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def resolve_targets(rows, target):
    """在当前工作集里定位被修订的在馆实体册记录。"""
    if "record_ids" in target:
        wanted = set(target["record_ids"])
        found = [r for r in rows if r.get("record_id") in wanted and r.get("status") == "held"]
        missing = wanted - {r["record_id"] for r in found}
        if missing:
            raise TargetMissing("修订目标不存在：%s" % "、".join(sorted(missing)))
        order = {rid: index for index, rid in enumerate(target["record_ids"])}
        return sorted(found, key=lambda r: order[r["record_id"]])
    scope = target["scope"]
    found = [r for r in rows
             if r.get("status") == "held"
             and _text(r.get("journal_key")) == scope["issn"]
             and _text(r.get("year")) == scope["year"]
             and _text(r.get("copy")) == scope["copy"]]
    if not found:
        raise TargetMissing("修订目标不存在：%s/%s/复本%s" % (scope["issn"], scope["year"], scope["copy"]))
    return found


def change_kind(targets, segments):
    if len(segments) > len(targets):
        return "split"
    if len(segments) < len(targets):
        return "merge"
    return "restate"


def issue_text(issues):
    ordered = sorted(issues, key=lambda i: (not i.isdigit(), int(i[1:]) if i[:1] == "S" and i[1:].isdigit() else 0, i))
    numbers = [int(i) for i in ordered if i.isdigit()]
    if numbers and len(numbers) == len(ordered) and numbers == list(range(numbers[0], numbers[-1] + 1)):
        return str(numbers[0]) if len(numbers) == 1 else "%d-%d" % (numbers[0], numbers[-1])
    return "/".join(ordered)


def _derived_id(primary, segments, position):
    explicit = segments[position]["record_id"]
    if explicit:
        return explicit
    if position == 0:
        return primary
    return "%s-%d" % (primary, position + 1)


def _derived_row(template, record_id, segment, revision, source_ids):
    row = dict(template)
    row["record_id"] = record_id
    row["issue"] = issue_text(segment["issues"])
    row["issues"] = list(segment["issues"])
    row["binding"] = segment["binding"]
    row["status"] = "held"
    row["derived"] = True
    row["derived_from"] = list(source_ids)
    row["derived_revision"] = revision
    row["amendment_note"] = segment.get("note", "")
    return row


def apply_change(rows, change):
    """把一条修订应用到工作集，返回 (新工作集, 元数据)。

    修订只能重排目标记录已覆盖的期次：合并把若干册并为一段，拆分把一册
    拆为多段；不能借修订新增或删除期次覆盖。
    """
    target = change["target"]
    segments = change["segments"]
    revision = change["revision"]
    targets = resolve_targets(rows, target)
    kind = change_kind(targets, segments)
    source_ids = [r["record_id"] for r in targets]
    template = targets[0]
    primary = source_ids[0]

    held_issues = {issue for r in targets for issue in r["issues"]}
    segment_issues = {issue for segment in segments for issue in segment["issues"]}
    if held_issues != segment_issues:
        raise BusinessError("AMEND_COVERAGE", "修订须保持已覆盖期次不变：原 %s，新 %s"
                            % (issue_text(held_issues), issue_text(segment_issues)))

    derived, used = [], set()
    for position, segment in enumerate(segments):
        record_id = _derived_id(primary, segments, position)
        if record_id in used:
            raise BusinessError("AMEND_ID", "派生记录号重复：%s" % record_id)
        if record_id not in source_ids and any(r["record_id"] == record_id for r in rows):
            raise BusinessError("AMEND_ID", "派生记录号冲突：%s" % record_id)
        used.add(record_id)
        derived.append(_derived_row(template, record_id, segment, revision, source_ids))

    victim_ids = set(source_ids)
    index = min(index for index, row in enumerate(rows) if row["record_id"] in victim_ids)
    before = sum(1 for row in rows[:index] if row["record_id"] not in victim_ids)
    survivors = [row for row in rows if row["record_id"] not in victim_ids]
    working = survivors[:before] + derived + survivors[before:]
    meta = {
        "revision": revision,
        "kind": kind,
        "targets": source_ids,
        "segments": [{"record_id": row["record_id"], "issues": list(segment["issues"]),
                      "binding": segment["binding"]}
                     for segment, row in zip(segments, derived)],
        "note": change.get("note", ""),
    }
    return working, meta


def applied_changes(base_result, changes):
    """按修订号与登记顺序重放已生效修订，返回工作集与逐条元数据。"""
    rows = [dict(row) for row in base_result.get("expanded", [])]
    metadata = []
    ordered = sorted(changes, key=lambda item: (item["revision"],
                                                item.get("seq", item.get("id", 0)),
                                                item.get("position", 0)))
    for change in ordered:
        rows, meta = apply_change(rows, change)
        meta["seq"] = change.get("seq", change.get("id"))
        metadata.append(meta)
    return rows, metadata


def derive(base_result, changes):
    """从原批次结果派生当前结果；原批次对象不被修改。"""
    expanded, metadata = applied_changes(base_result, changes)

    from app.modules.m4_combined.rules import apply as stage4
    from app.modules.m5_gaps.rules import apply as stage5
    from app.modules.m6_binding.rules import apply as stage6
    from app.modules.m7_supplement.rules import apply as stage7

    ctx = {
        "expanded": expanded,
        "quarantine": list(base_result.get("quarantine", [])),
        "input_count": base_result.get("input_count", 0),
        "blank_rows": base_result.get("blank_rows", 0),
        "identity_conflicts": list(base_result.get("identity_conflicts", [])),
    }
    report4 = stage4(ctx)
    report5 = stage5(ctx)
    report6 = stage6(ctx)
    report7 = stage7(ctx)

    base_reports = base_result.get("reports", [])
    reports = list(base_reports) if len(base_reports) == 7 else [None] * 7
    reports[2] = report(expanded, 展开期次=sum(len(r["issues"]) for r in expanded), 实体记录=len(expanded))
    reports[3] = report4
    reports[4] = report5
    reports[5] = report6
    reports[6] = report7

    return {
        "reports": reports,
        "raw": base_result.get("raw", []),
        "canonical": base_result.get("canonical", []),
        "expanded": expanded,
        "held": ctx["held"],
        "gaps": ctx["gaps"],
        "final": ctx["final"],
        "books": ctx["books"],
        "balance": ctx["balance"],
        "binding_conflicts": ctx["binding_conflicts"],
        "identity_conflicts": ctx["identity_conflicts"],
        "blocked": ctx["blocked"],
        "quarantine": list(base_result.get("quarantine", [])),
        "input_count": base_result.get("input_count", 0),
        "blank_rows": base_result.get("blank_rows", 0),
        "reasons": base_result.get("reasons", {}),
        "amendment_meta": metadata,
    }


def _covered(result):
    return sum(len(r["issues"]) for r in result.get("held", []))


def _missing(result):
    return sum(len(r["missing"]) for r in result.get("gaps", []))


def _journals(result):
    return {(r["issn"], r["year"], r["title"]): r for r in result.get("gaps", [])}


def comparison(base_result, current_result):
    """原批次与当前批次的可并排核对指标与逐刊覆盖。"""
    metrics = []
    pairs = [
        ("实体册", base_result.get("books", 0), current_result.get("books", 0)),
        ("覆盖期次", _covered(base_result), _covered(current_result)),
        ("缺期", _missing(base_result), _missing(current_result)),
        ("补录任务", len(base_result.get("final", [])), len(current_result.get("final", []))),
        ("合刊记录", sum(len(r["issues"]) > 1 for r in base_result.get("held", [])),
         sum(len(r["issues"]) > 1 for r in current_result.get("held", []))),
        ("装订冲突", len(base_result.get("binding_conflicts", [])),
         len(current_result.get("binding_conflicts", []))),
    ]
    for name, left, right in pairs:
        metrics.append({"metric": name, "original": left, "current": right, "delta": right - left})
    metrics.append({"metric": "阻断", "original": bool(base_result.get("blocked")),
                    "current": bool(current_result.get("blocked")),
                    "delta": len(current_result.get("binding_conflicts", []))
                    + len(current_result.get("identity_conflicts", []))
                    - len(base_result.get("binding_conflicts", []))
                    - len(base_result.get("identity_conflicts", []))})

    base_journals, cur_journals = _journals(base_result), _journals(current_result)
    journals = []
    for key in sorted(set(base_journals) | set(cur_journals), key=lambda k: (k[0], k[1], k[2])):
        left = base_journals.get(key) or {"covered": [], "missing": [], "suspended": []}
        right = cur_journals.get(key) or {"covered": [], "missing": [], "suspended": []}
        journals.append({
            "issn": key[0], "year": key[1], "title": key[2],
            "original": {"covered": list(left.get("covered", [])), "missing": list(left.get("missing", [])),
                         "suspended": list(left.get("suspended", []))},
            "current": {"covered": list(right.get("covered", [])), "missing": list(right.get("missing", [])),
                        "suspended": list(right.get("suspended", []))},
            "changed": (sorted(left.get("covered", [])) != sorted(right.get("covered", []))
                        or sorted(left.get("missing", [])) != sorted(right.get("missing", []))),
        })
    return {"metrics": metrics, "journals": journals}

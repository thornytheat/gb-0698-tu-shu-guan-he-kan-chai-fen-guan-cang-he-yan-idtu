import csv
import io
import json


def _row(writer, run, kind, line, reason, evidence):
    writer.writerow([run["id"], run["digest"], run["rule_version"], kind, line, reason,
                     json.dumps(evidence, ensure_ascii=False)])


def evidence_csv(run):
    """唯一证据CSV：运行摘要、原批次与当前批次并排核对、修订与复核留痕、隔离记录。"""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["run_id", "sha256", "rule_version", "kind", "line", "reason", "evidence"])
    base = run.get("base_result", run["result"])
    current = run["result"]
    _row(writer, run, "manifest", "", "", {
        "balance": current["balance"],
        "decision": run["decision"],
        "revision": run["revision"],
        "amendment_revision": run.get("amendment_revision", 0),
        "confirmed_amendment_revision": (run.get("confirmed") or {}).get("amendment_revision"),
        "derivation": run.get("derivation", {"status": "ok"}),
        "confirmed_current": run.get("confirmed_current", False),
    })
    _row(writer, run, "original_manifest", "", "", {
        "digest": run.get("digest"),
        "books": base.get("books"), "covered": sum(len(r["issues"]) for r in base.get("held", [])),
        "missing": sum(len(g["missing"]) for g in base.get("gaps", [])),
        "tasks": len(base.get("final", [])),
    })
    comparison = run.get("comparison") or {}
    for metric in comparison.get("metrics", []):
        _row(writer, run, "comparison", "", metric["metric"], metric)
    for journal in comparison.get("journals", []):
        _row(writer, run, "comparison_journal", "", journal["issn"], journal)
    for change in run.get("amendments", []):
        _row(writer, run, "amendment", "", change["status"], {
            "revision": change["revision"], "seq": change["seq"], "digest": change["digest"],
            "note": change["note"], "payload": change["payload"], "created": change["created"],
        })
    for entry in run.get("reviews", []):
        _row(writer, run, "review", "", entry["decision"], {
            "revision": entry["revision"], "amendment_revision": entry["amendment_revision"],
            "snapshot": entry["snapshot"], "created": entry["created"],
        })
    for row in current.get("final", []):
        _row(writer, run, "result", row.get("line", ""), row.get("status", ""), row)
    for row in current.get("quarantine", []):
        _row(writer, run, "quarantine", row.get("line", ""), row["reason"], row)
    return "﻿" + out.getvalue()

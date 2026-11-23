"""验收结果导出：页面与导出同版，均以封存 result_json 为唯一数据源。"""

import csv
import io

from ..config import ARTIFACT_DIR
from ..db import loads
from ..domain import format_score


def safe_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@"))
                                   or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def export_filename(attempt_id):
    return f"acceptance-{attempt_id}.csv"


def template_csv():
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["批号", "票号", "票种", "B 置信分", "A 是否放行", "人工真值是否合格"])
    writer.writerow(["BATCH-001", "BATCH-001-增-001", "增值税发票", 96.5, "是", "是"])
    writer.writerow(["BATCH-001", "BATCH-001-增-002", "增值税发票", 72.0, "否", "否"])
    writer.writerow(["BATCH-001", "BATCH-001-零-001", "零售小票", 88.2, "否", "是"])
    writer.writerow(["BATCH-001", "BATCH-001-零-002", "零售小票", 45.0, "是", "否"])
    return "﻿" + buffer.getvalue()


def render_csv(attempt_row):
    result = loads(attempt_row["result_json"])
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["票据置信分放行验收报告"])
    writer.writerow(["验收结论", "通过" if result["passed"] else "不通过"])
    writer.writerow(["结论说明", safe_cell(result["reason"])])
    writer.writerow(["封存阈值版本", result["threshold_version"]])
    writer.writerow(["验收批号", result["dataset_batch_id"]])
    writer.writerow(["数据指纹 SHA-256", result["dataset_checksum"]])
    writer.writerow([])
    writer.writerow(["票种", "封存阈值", "错放上限", "合格放行数（B）", "错放数（B）",
                     "合格放行数（A）", "错放数（A）", "B-A 合格放行差", "错放是否超限"])
    for item in result["per_type"]:
        writer.writerow([
            safe_cell(item["ticket_type"]),
            item["threshold_label"],
            item["cap"],
            item["b_released_qualified"],
            item["b_wrong_releases"],
            item["a_released_qualified"],
            item["a_wrong_releases"],
            item["delta_qualified"],
            "否" if item["within_cap"] else "是",
        ])
    writer.writerow([])
    writer.writerow(["合计 B 合格放行数", result["total_b_released_qualified"]])
    writer.writerow(["合计 A 合格放行数", result["total_a_released_qualified"]])
    writer.writerow(["合计 B 错放数", result["total_b_wrong_releases"]])
    writer.writerow(["B 较 A 多放行合格票", result["delta_qualified"]])
    return "﻿" + buffer.getvalue()


def write_export(attempt_row):
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    name = export_filename(attempt_row["id"])
    path = ARTIFACT_DIR / name
    staging = path.with_suffix(".tmp")
    staging.write_text(render_csv(attempt_row), encoding="utf-8")
    staging.replace(path)
    return name

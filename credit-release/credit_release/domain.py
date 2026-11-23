"""核心领域逻辑：CSV 解析、阈值选择、封存验收评估。

规则口径（与财务约定一致）：
- 调参批与验收批不得共用批号或票号；票号在所有导入数据中全局唯一。
- 每种票在错放上限内，选放行合格票最多的阈值；放行数相同取更严（更高分）。
- 阈值仅在调参批上封存；验收批封存前不得导入，封存揭晓后不得再用于换阈值复验。
- 验收通过：各票种错放均不超上限，且 B 放行合格票总数严格多于 A。
"""

import csv
import io
import json
import hashlib
from collections import OrderedDict, defaultdict

from .config import BOOL_TEXT, CSV_FIELDS, DEFAULT_CAP, MAX_ROWS, SCORE_MAX, SCORE_MIN, TICKET_TYPES


class DomainError(ValueError):
    """带业务含义的输入错误，HTTP 层映射为 400/409。"""


def _parse_bool(raw, label, line_no):
    text = raw.strip().lower() if isinstance(raw, str) else raw
    if text in (None, ""):
        raise DomainError(f"CSV 第 {line_no} 行：{label}不能为空")
    try:
        return BOOL_TEXT[text]
    except (KeyError, TypeError):
        raise DomainError(f"CSV 第 {line_no} 行：{label}需为 是/否、true/false 或 1/0") from None


def parse_csv(text):
    """解析 UTF-8 CSV（允许 BOM），返回规范化后的行列表。"""
    reader = csv.DictReader(io.StringIO(text.removeprefix("﻿"), newline=""), strict=True)
    headers = reader.fieldnames or []
    expected = [label for _, label in CSV_FIELDS]
    if headers != expected:
        raise DomainError(f"CSV 表头须为：{', '.join(expected)}")
    rows = []
    try:
        for raw in reader:
            line_no = reader.line_num
            if len(rows) >= MAX_ROWS:
                raise DomainError(f"单个批次最多导入 {MAX_ROWS} 行")
            if None in raw or any(value is None for value in raw.values()):
                raise DomainError(f"CSV 第 {line_no} 行：列数与表头不一致")
            batch_id = (raw.get("批号") or "").strip()
            ticket_id = (raw.get("票号") or "").strip()
            ticket_type = (raw.get("票种") or "").strip()
            if not batch_id or not ticket_id:
                raise DomainError(f"CSV 第 {line_no} 行：批号、票号不能为空")
            if len(batch_id) > 64 or len(ticket_id) > 64:
                raise DomainError(f"CSV 第 {line_no} 行：批号/票号长度不能超过 64")
            if ticket_type not in TICKET_TYPES:
                raise DomainError(f"CSV 第 {line_no} 行：票种须为 {'/'.join(TICKET_TYPES)}")
            score_text = (raw.get("B 置信分") or "").strip()
            try:
                score = float(score_text)
            except ValueError:
                raise DomainError(f"CSV 第 {line_no} 行：B 置信分需为数值") from None
            if not (SCORE_MIN <= score <= SCORE_MAX):
                raise DomainError(f"CSV 第 {line_no} 行：B 置信分需在 {SCORE_MIN:g} 至 {SCORE_MAX:g} 之间")
            rows.append({
                "batch_id": batch_id,
                "ticket_id": ticket_id,
                "ticket_type": ticket_type,
                "score_b": round(score, 6),
                "a_release": _parse_bool(raw.get("A 是否放行"), "A 是否放行", line_no),
                "manual_qualified": _parse_bool(raw.get("人工真值是否合格"), "人工真值是否合格", line_no),
            })
    except csv.Error:
        raise DomainError(f"CSV 第 {reader.line_num} 行附近格式不正确") from None
    if not rows:
        raise DomainError("CSV 至少需要一行有效数据")
    return rows


def validate_batch_integrity(rows):
    """单文件内部完整性：只允许一个批号；票号不重复；每个票种至少一行。"""
    batch_ids = {row["batch_id"] for row in rows}
    if len(batch_ids) > 1:
        raise DomainError(f"一个导入文件只能包含一个批号，检测到：{', '.join(sorted(batch_ids))}")
    seen = set()
    types_seen = set()
    for row in rows:
        if row["ticket_id"] in seen:
            raise DomainError(f"票号在文件内重复：{row['ticket_id']}")
        seen.add(row["ticket_id"])
        types_seen.add(row["ticket_type"])
    missing = sorted(set(TICKET_TYPES) - types_seen)
    if missing:
        raise DomainError(f"批次需覆盖全部票种，缺少：{', '.join(missing)}")
    return next(iter(batch_ids))


def checksum(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_caps(caps):
    """规范化错放上限：{票种: 非负整数}，缺省给默认值。"""
    if not isinstance(caps, dict):
        raise DomainError("错放上限需按票种提供")
    result = {}
    for ticket_type in TICKET_TYPES:
        value = caps.get(ticket_type, DEFAULT_CAP)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DomainError(f"{ticket_type} 的错放上限需为非负整数")
        result[ticket_type] = value
    if set(caps) - set(TICKET_TYPES):
        raise DomainError("错放上限包含未知票种")
    return result


def _outcome(rows, threshold):
    """给定阈值（None 表示全部不放行），逐票种统计合格放行数与错放数。"""
    stats = {t: {"released_qualified": 0, "wrong_releases": 0, "released": 0} for t in TICKET_TYPES}
    for row in rows:
        released = threshold is not None and row["score_b"] >= threshold
        if not released:
            continue
        bucket = stats[row["ticket_type"]]
        bucket["released"] += 1
        if row["manual_qualified"]:
            bucket["released_qualified"] += 1
        else:
            bucket["wrong_releases"] += 1
    return stats


def candidate_table(rows):
    """候选阈值 = 本批出现过的全部不同置信分（从高到低），另含 None（不放行）哨兵。"""
    scores = sorted({row["score_b"] for row in rows}, reverse=True)
    return [None] + scores


def select_thresholds(tuning_rows, caps):
    """每个票种独立选阈值。

    选择规则：在错放数 ≤ 上限的可行阈值里，取合格放行数最大者；
    合格放行数相同取更严阈值（分数更高；None 哨兵视为最严）。
    若不存在可放行的可行阈值，封存 None（该票种不放行任何票）并标记 blocked。
    """
    caps = normalize_caps(caps)
    by_type = defaultdict(list)
    for row in tuning_rows:
        by_type[row["ticket_type"]].append(row)

    sealed = OrderedDict()
    for ticket_type in TICKET_TYPES:
        type_rows = by_type[ticket_type]
        cap = caps[ticket_type]
        candidates = candidate_table(type_rows)
        feasible = []
        for threshold in candidates:
            stats = _outcome(type_rows, threshold)[ticket_type]
            if stats["wrong_releases"] <= cap:
                feasible.append((threshold, stats))
        # feasible 一定非空：None 哨兵错放数为 0。
        def rank(item):
            threshold, stats = item
            # 合格放行数最大；同分按更严优先（候选本身已按严格度降序，稳定排序即可）。
            return stats["released_qualified"]

        best_threshold, best_stats = max(feasible, key=rank)
        # 放行数相同取更严：在合格放行数相同的可行阈值里选严格度最高者。
        top = [item for item in feasible if item[1]["released_qualified"] == best_stats["released_qualified"]]
        # candidates 顺序即严格度降序（None 最严，随后分数由高到低），取第一个。
        order = {value: index for index, value in enumerate(candidates)}
        best_threshold, best_stats = min(top, key=lambda item: order[item[0]])

        tuning_stats = _outcome(type_rows, best_threshold)[ticket_type]
        sealed[ticket_type] = {
            "ticket_type": ticket_type,
            "threshold": best_threshold,
            "cap": cap,
            "blocked": best_threshold is None,
            "tuning_released_qualified": tuning_stats["released_qualified"],
            "tuning_wrong_releases": tuning_stats["wrong_releases"],
            "tuning_total": len(type_rows),
        }
    return sealed


def build_candidate_preview(tuning_rows, caps):
    """封存前预览：每票种全部候选阈值的表现与入选标记。"""
    caps = normalize_caps(caps)
    chosen = select_thresholds(tuning_rows, caps)
    preview = {}
    by_type = defaultdict(list)
    for row in tuning_rows:
        by_type[row["ticket_type"]].append(row)
    for ticket_type in TICKET_TYPES:
        type_rows = by_type[ticket_type]
        entries = []
        for threshold in candidate_table(type_rows):
            stats = _outcome(type_rows, threshold)[ticket_type]
            entries.append({
                "threshold": threshold,
                "threshold_label": "不放行（最严）" if threshold is None else format_score(threshold),
                "released_qualified": stats["released_qualified"],
                "wrong_releases": stats["wrong_releases"],
                "released": stats["released"],
                "feasible": stats["wrong_releases"] <= caps[ticket_type],
                "selected": threshold == chosen[ticket_type]["threshold"],
            })
        preview[ticket_type] = entries
    return preview, chosen


def format_score(value):
    if value is None:
        return "不放行"
    return f"{value:g}"


def evaluate_acceptance(rows, sealed):
    """用已封存阈值在验收批上评估 A/B 放行表现与通过结论。"""
    by_type = defaultdict(list)
    for row in rows:
        by_type[row["ticket_type"]].append(row)

    per_type = []
    total_b_qualified = total_a_qualified = 0
    total_b_wrong = 0
    all_within_cap = True
    for ticket_type in TICKET_TYPES:
        config = sealed[ticket_type]
        threshold = config["threshold"]
        cap = config["cap"]
        type_rows = by_type.get(ticket_type, [])
        b_stats = _outcome(type_rows, threshold)[ticket_type]
        a_qualified = sum(1 for row in type_rows if row["a_release"] and row["manual_qualified"])
        a_wrong = sum(1 for row in type_rows if row["a_release"] and not row["manual_qualified"])
        within_cap = b_stats["wrong_releases"] <= cap
        all_within_cap = all_within_cap and within_cap
        total_b_qualified += b_stats["released_qualified"]
        total_a_qualified += a_qualified
        total_b_wrong += b_stats["wrong_releases"]
        per_type.append({
            "ticket_type": ticket_type,
            "threshold": threshold,
            "threshold_label": format_score(threshold),
            "cap": cap,
            "total": len(type_rows),
            "b_released_qualified": b_stats["released_qualified"],
            "b_wrong_releases": b_stats["wrong_releases"],
            "b_released": b_stats["released"],
            "a_released_qualified": a_qualified,
            "a_wrong_releases": a_wrong,
            "delta_qualified": b_stats["released_qualified"] - a_qualified,
            "within_cap": within_cap,
        })

    b_beats_a = total_b_qualified > total_a_qualified
    passed = all_within_cap and b_beats_a
    reasons = []
    if not all_within_cap:
        over = [item["ticket_type"] for item in per_type if not item["within_cap"]]
        reasons.append(f"错放超限票种：{'、'.join(over)}")
    if not b_beats_a:
        reasons.append(f"B 合格放行 {total_b_qualified} 张，未严格多于 A 的 {total_a_qualified} 张")
    return {
        "per_type": per_type,
        "total_b_released_qualified": total_b_qualified,
        "total_a_released_qualified": total_a_qualified,
        "total_b_wrong_releases": total_b_wrong,
        "delta_qualified": total_b_qualified - total_a_qualified,
        "all_within_cap": all_within_cap,
        "b_beats_a": b_beats_a,
        "passed": passed,
        "reason": "验收通过：各票种错放均不超上限，且 B 比 A 多放行合格票。"
        if passed else "验收不通过：" + "；".join(reasons) + "。",
    }

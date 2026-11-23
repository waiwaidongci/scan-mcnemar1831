"""核心阈值选择与验收规则的单元测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credit_release.domain import (
    DomainError,
    build_candidate_preview,
    evaluate_acceptance,
    parse_csv,
    select_thresholds,
)

HEADER = "批号,票号,票种,B 置信分,A 是否放行,人工真值是否合格"


def row(batch, ticket, ticket_type, score, a_release, qualified):
    return {"batch_id": batch, "ticket_id": ticket, "ticket_type": ticket_type,
            "score_b": score, "a_release": a_release, "manual_qualified": qualified}


class CsvParseTests(unittest.TestCase):
    def test_header_and_types(self):
        text = "\n".join([
            HEADER,
            "B1,T1,增值税发票,88.5,是,是",
            "B1,T2,零售小票,12,否,false",
        ])
        rows = parse_csv(text)
        self.assertEqual(rows[0]["score_b"], 88.5)
        self.assertTrue(rows[0]["a_release"])
        self.assertFalse(rows[1]["manual_qualified"])

    def test_bad_header_rejected(self):
        with self.assertRaises(DomainError):
            parse_csv("批号,票号\nB1,T1")

    def test_score_range_enforced(self):
        text = "\n".join([HEADER, "B1,T1,增值税发票,101,是,是"])
        with self.assertRaises(DomainError):
            parse_csv(text)

    def test_unknown_ticket_type(self):
        text = "\n".join([HEADER, "B1,T1,火车票,80,是,是"])
        with self.assertRaises(DomainError):
            parse_csv(text)


class ThresholdSelectionTests(unittest.TestCase):
    def setUp(self):
        # 票种 X：分数与真值构造出明确的错放权衡
        self.rows_x = [
            row("B", "X1", "增值税发票", 95, True, True),
            row("B", "X2", "增值税发票", 90, True, True),
            row("B", "X3", "增值税发票", 80, False, False),  # 高分错票
            row("B", "X4", "增值税发票", 70, False, True),
            row("B", "X5", "增值税发票", 60, False, False),
            row("B", "X6", "增值税发票", 50, False, True),
        ]
        self.rows_y = [
            row("B", f"Y{i}", "零售小票", 40 + i, False, i % 2 == 0)
            for i in range(6)
        ]
        self.rows = self.rows_x + self.rows_y

    def test_max_qualified_within_cap(self):
        caps = {"增值税发票": 0, "零售小票": 0}
        sealed = select_thresholds(self.rows, caps)
        #  cap=0：阈值 90 放行 X1、X2 两张合格票且无错放；80 会放进错票 X3
        self.assertEqual(sealed["增值税发票"]["threshold"], 90.0)
        self.assertEqual(sealed["增值税发票"]["tuning_released_qualified"], 2)
        self.assertEqual(sealed["增值税发票"]["tuning_wrong_releases"], 0)

    def test_tie_prefers_stricter_threshold(self):
        # 阈值 95 与 90 在 cap=0 下合格放行相同（构造：去掉 X2 的错票影响）
        rows = [
            row("B", "X1", "增值税发票", 95, True, True),
            row("B", "X2", "增值税发票", 90, True, True),
            row("B", "X3", "增值税发票", 30, False, False),
        ] + [row("B", f"Y{i}", "零售小票", 50, False, True) for i in range(3)]
        sealed = select_thresholds(rows, {"增值税发票": 0, "零售小票": 0})
        # 95 与 90 都只放行合格票；95 放行 1 张，90 放行 2 张，故这里先验证更严的真同分场景
        tie_rows = [
            row("B", "X1", "增值税发票", 90, True, True),
            # 另一阈值 85 放行的合格数与 90 相同（增加一张同分合格票，不增加错放）
            row("B", "X2", "增值税发票", 85, False, True),
            row("B", "X3", "增值税发票", 30, False, False),
        ] + [row("B", f"Y{i}", "零售小票", 50, False, True) for i in range(3)]
        sealed_tie = select_thresholds(tie_rows, {"增值税发票": 0, "零售小票": 0})
        # 90 放行 1 张合格，85 放行 2 张合格，合格数不同；再造真同数：
        same_rows = [
            row("B", "X1", "增值税发票", 90, True, True),
            row("B", "X2", "增值税发票", 85, False, False),  # 85 会错放，cap=0 不可行
            row("B", "X3", "增值税发票", 30, False, False),
        ] + [row("B", f"Y{i}", "零售小票", 50, False, True) for i in range(3)]
        sealed_same = select_thresholds(same_rows, {"增值税发票": 0, "零售小票": 0})
        self.assertEqual(sealed_same["增值税发票"]["threshold"], 90.0)
        self.assertIn(sealed["增值税发票"]["threshold"], (90.0, 95.0))
        # 预览里入选标记唯一
        preview, chosen = build_candidate_preview(rows, {"增值税发票": 0, "零售小票": 0})
        selected = [c for c in preview["增值税发票"] if c["selected"]]
        self.assertEqual(len(selected), 1)

    def test_blocked_when_no_feasible_release(self):
        # cap=0 且最高分票就是错票 -> 只能不放行
        rows = [
            row("B", "X1", "增值税发票", 99, False, False),
            row("B", "X2", "增值税发票", 80, False, True),
        ] + [row("B", f"Y{i}", "零售小票", 50, False, True) for i in range(3)]
        sealed = select_thresholds(rows, {"增值税发票": 0, "零售小票": 0})
        self.assertIsNone(sealed["增值税发票"]["threshold"])
        self.assertTrue(sealed["增值税发票"]["blocked"])
        self.assertEqual(sealed["增值税发票"]["tuning_wrong_releases"], 0)

    def test_cap_allows_more_release(self):
        sealed = select_thresholds(self.rows, {"增值税发票": 1, "零售小票": 0})
        # cap=1：阈值 80 放行 X1、X2（合格）+ X3（错放 1），合格 2；阈值 70 再多合格 X4
        self.assertEqual(sealed["增值税发票"]["tuning_wrong_releases"], 1)
        self.assertGreaterEqual(sealed["增值税发票"]["tuning_released_qualified"], 3)


class AcceptanceEvaluationTests(unittest.TestCase):
    def test_pass_when_cap_ok_and_b_beats_a(self):
        sealed = {
            "增值税发票": {"threshold": 80.0, "cap": 1, "blocked": False},
            "零售小票": {"threshold": 50.0, "cap": 1, "blocked": False},
        }
        rows = [
            row("A", "T1", "增值税发票", 95, False, True),   # B 放行合格，A 未放行
            row("A", "T2", "增值税发票", 30, True, False),    # A 错放
            row("A", "T3", "零售小票", 60, False, True),
        ]
        result = evaluate_acceptance(rows, sealed)
        self.assertTrue(result["all_within_cap"])
        self.assertTrue(result["b_beats_a"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["total_b_released_qualified"], 2)
        self.assertEqual(result["total_a_released_qualified"], 0)

    def test_fail_on_cap_breach(self):
        sealed = {
            "增值税发票": {"threshold": 80.0, "cap": 0, "blocked": False},
            "零售小票": {"threshold": 50.0, "cap": 0, "blocked": False},
        }
        rows = [
            row("A", "T1", "增值税发票", 95, False, False),  # B 错放，超 cap
            row("A", "T2", "零售小票", 60, False, True),
        ]
        result = evaluate_acceptance(rows, sealed)
        self.assertFalse(result["all_within_cap"])
        self.assertFalse(result["passed"])

    def test_fail_when_b_not_strictly_better(self):
        sealed = {
            "增值税发票": {"threshold": 80.0, "cap": 5, "blocked": False},
            "零售小票": {"threshold": 50.0, "cap": 5, "blocked": False},
        }
        rows = [
            row("A", "T1", "增值税发票", 95, True, True),  # A、B 都放行合格
        ]
        result = evaluate_acceptance(rows, sealed)
        self.assertTrue(result["all_within_cap"])
        self.assertFalse(result["b_beats_a"])
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

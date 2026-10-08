"""原始标注不变；安全失败、保守漏判和强度偏差分别评估。"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from test_affection import plugin_module

HERE = Path(__file__).parent
MATCHER = plugin_module.IntentMatcher(["麦麦", "小麦"])
CLAUDE_SAMPLES = [
    json.loads(line) for line in (HERE / "affection_samples.jsonl").read_text().splitlines() if line.strip()
]
LOCAL_SAMPLES = json.loads((HERE / "scoring_cases.json").read_text())["cases"]
MENTION_RE = re.compile(r"@([^\s@，,。.!！?？；;：:]+)")
# 原始120条标注保持不变；以下是已批准的新契约，不能按实现输出自动改标注。
CONTRACT_CHANGES = {
    "positive_09": (2, "爱默认2"),
    "different_targets_10": (2, "爱默认2"),
    "same_target_turn_07": (2, "爱默认2"),
    "aggregation_04": (2, "重复的爱仍只计2"),
    "same_target_turn_10": (0, "未解析否定不能被转折清除"),
    "uncertain_reference_07": (1, "普通请求的时间指代不当作态度指代"),
    "uncertain_reference_08": (1, "普通提问没有情绪或否定信号"),
    "uncertain_reference_10": (1, "时间比较不遮蔽当前明确致谢"),
}


def evaluate_sample(sample: dict) -> tuple[int, str]:
    # 只用聊天类型、正文和引用来源；target/emotion/expected 仅用于验收。
    text = sample["text"]
    mentions = MENTION_RE.findall(text)
    other = sample.get("other_target", False) or any(name not in MATCHER.names for name in mentions)
    if sample["quote"] is not None:
        other |= sample["quote"]["from"] == "other"
    return MATCHER.score(
        text,
        private=sample["chat"] == "private",
        directed=sample.get("directed", any(name in MATCHER.names for name in mentions)),
        other_target=other,
    )


def safety_failures(sample: dict, actual: int) -> list[str]:
    expected = sample["expected"]
    reasons = []
    if expected * actual < 0:
        reasons.append("方向相反")
    if expected <= 0 and actual > 0:
        reasons.append("不该加分却加分")
    if sample["target"] != "bot" and actual < 0:
        reasons.append("误扣其他对象")
    if sample["emotion"] == 0 and expected == 1 and actual not in (0, 1):
        reasons.append("基础分超出0或1")
    return reasons


def summarize(samples: list[dict]) -> dict:
    results = [(sample, *evaluate_sample(sample)) for sample in samples]
    basic = [(s, a) for s, a, _ in results if s["emotion"] == 0 and s["expected"] == 1]
    emotions = [(s, a) for s, a, _ in results if s["emotion"] != 0 and s["expected"] != 0]
    failures = sum(bool(safety_failures(s, a)) for s, a, _ in results)
    basic_hits = sum(a == 1 for _, a in basic)
    sources = sorted({s["source"] for s in samples})
    return {
        "source": sources[0] if len(sources) == 1 else sources,
        "sample_count": len(samples),
        "safety_failure_count": failures,
        "basic_case_count": len(basic),
        "basic_hit_count": basic_hits,
        "basic_coverage": basic_hits / len(basic) if basic else None,
        "basic_miss_count": sum(a == 0 for _, a in basic),
        "emotion_case_count": len(emotions),
        "emotion_direction_covered": sum(s["expected"] * a > 0 for s, a in emotions),
        "emotion_miss_count": sum(a == 0 for _, a in emotions),
        "intensity_difference_count": sum(s["expected"] * a > 0 and a != s["expected"] for s, a in emotions),
        "exact_match_count": sum(s["expected"] == a for s, a, _ in results),
        "acceptance_passed": failures == 0 and bool(basic) and basic_hits / len(basic) >= 0.8,
    }


class ScoringSamplesTests(unittest.TestCase):
    def test_frozen_local_rule_examples(self) -> None:
        self.assertEqual(len(LOCAL_SAMPLES), 120)
        for sample in LOCAL_SAMPLES:
            with self.subTest(id=sample["id"], text=sample["text"]):
                actual, _ = MATCHER.score(
                    sample["text"],
                    private=sample["private"],
                    directed=sample["directed"],
                    other_target=sample["other_target"],
                )
                expected, reason = CONTRACT_CHANGES.get(sample["id"], (sample["expected"], "旧规则仍适用"))
                self.assertEqual(actual, expected, reason)

    def test_user_samples_with_asymmetric_acceptance(self) -> None:
        self.assertEqual(len(CLAUDE_SAMPLES), 130)
        for sample in CLAUDE_SAMPLES:
            with self.subTest(id=sample["id"], text=sample["text"]):
                actual, _ = evaluate_sample(sample)
                self.assertFalse(safety_failures(sample, actual))


if __name__ == "__main__":
    unittest.main()

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


def evaluate_sample(sample: dict) -> tuple[int, str]:
    # 只用聊天类型、正文和引用来源；target/emotion/expected 仅用于验收。
    text = sample["text"]
    other = any(name not in MATCHER.names for name in MENTION_RE.findall(text))
    if sample["quote"] is not None:
        other |= sample["quote"]["from"] == "other"
    return MATCHER.score(
        text,
        private=sample["chat"] == "private",
        directed=bool(MATCHER.name_re.match(text.strip())),
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
                self.assertEqual(actual, sample["expected"])

    def test_user_samples_with_asymmetric_acceptance(self) -> None:
        self.assertEqual(len(CLAUDE_SAMPLES), 130)
        for sample in CLAUDE_SAMPLES:
            with self.subTest(id=sample["id"], text=sample["text"]):
                actual, _ = evaluate_sample(sample)
                self.assertFalse(safety_failures(sample, actual))


if __name__ == "__main__":
    unittest.main()

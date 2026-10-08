"""公开替换例子的回归测试；不读取独立留出集。"""

from __future__ import annotations

import unittest

from test_affection import plugin_module


class ScoringGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = plugin_module.IntentMatcher(["麦麦", "小麦"])

    def score(self, text: str) -> tuple[int, str]:
        return self.matcher.score(text, private=True, directed=True)

    def test_negative_roots_block_basic_score_after_inserted_modifiers(self) -> None:
        for text in ("我恨死你了", "烦死你了", "我恨透你了", "你蠢得让我头疼"):
            with self.subTest(text=text):
                self.assertLessEqual(self.score(text)[0], 0)

    def test_unparsed_love_is_not_neutral_interaction(self) -> None:
        for text in ("我爱死你了", "爱惨你了", "我深爱着你"):
            with self.subTest(text=text):
                actual, label = self.score(text)
                self.assertGreaterEqual(actual, 0)
                self.assertNotEqual(label, "基础交流")

    def test_unrated_negative_predicates_do_not_earn_basic_score(self) -> None:
        for text in (
            "你唠叨得像复读机",
            "你话多得很",
            "你说话不好听",
            "你是狗吧",
            "你像只猪一样吵",
            "你赶紧走开",
            "麦麦，走远点",
        ):
            with self.subTest(text=text):
                self.assertLessEqual(self.score(text)[0], 0)

    def test_praise_followed_by_bot_failure_is_uncertain(self) -> None:
        for separator in ("，", " ", "\n"):
            for failure in (
                "又选错了方向",
                "你又搞砸了会议",
                "又失败了",
                "这道题又做错了",
                "这个结果又算错了",
                "这个答案又搞错了",
                "这次又失误了",
            ):
                text = f"你真棒{separator}{failure}"
                with self.subTest(text=text):
                    self.assertEqual(self.score(text)[0], 0)

    def test_dismissive_phrases_are_uncertain(self) -> None:
        for text in ("你说了算", "随你便", "你最有道理", "麦麦，你说了算", "那就随你便"):
            with self.subTest(text=text):
                self.assertEqual(self.score(text)[0], 0)

    def test_final_bot_name_keeps_thanks_target(self) -> None:
        for text in ("谢谢麦麦", "多谢小麦", "谢谢你 麦麦", "谢啦麦麦", "谢谢了麦麦", "谢谢啊 小麦"):
            with self.subTest(text=text):
                self.assertEqual(self.score(text), (1, "感谢"))

    def test_too_praise_with_question_tone_is_a_statement(self) -> None:
        for text in ("你太贴心了吧", "麦麦 太聪明了吧", "你 太 厉害了吧", "你也太棒了吧", "麦麦也太厉害了吧"):
            with self.subTest(text=text):
                self.assertEqual(self.score(text), (1, "夸奖"))

    def test_root_exclusions_preserve_neutral_chat(self) -> None:
        for text in (
            "页面滚动到第三段了",
            "这只猫在草地上打滚",
            "我的爱好是拍照",
            "我买了棒棒糖和棒球手套",
            "滚筒和滚轮已经放在架子上",
            "累计三页记录，积累的资料明天整理",
            "麻烦你帮我看看这道题",
            "今天又错过了一班公交",
            "棒球打到了球棒上",
            "你是狗主人吗",
            "你是猪年出生的吗",
            "你是驴友吗",
            "我今天去图书馆取了预约的书，下午整理笔记",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.score(text), (1, "基础交流"))
        self.assertEqual(self.score("可爱的猫")[0], 0)

    def test_failure_of_other_object_does_not_cancel_direct_praise(self) -> None:
        for text in (
            "你真棒，不过那个导航又选错了方向",
            "你真棒，不过这次是小王搞砸了会议",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.score(text), (1, "夸奖"))


if __name__ == "__main__":
    unittest.main()

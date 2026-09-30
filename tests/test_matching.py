import unittest

from subfinder.matching import (blog_title_alias, filename_episode, is_special, matching_post,
                                matching_public_title, season_of, suitable_file, title_queries,
                                without_mixed_specials)


class ReferenceMatchingTests(unittest.TestCase):
    def test_filename_episode_recognizes_episode_markers_used_in_archives(self):
        examples = {
            "01.smi": 1,
            "folder/Example #24 END.srt": 24,
            "Example S02E03.ass": 3,
            "01Ohys-Raws.smi": 1,
            "1화 -ns.smi": 1,
            "Example 1.5화.srt": 1.5,
            "Example02.srt": None,
        }
        for name, expected in examples.items():
            with self.subTest(name=name):
                self.assertEqual(filename_episode(name), expected)

    def test_numbered_plain_file_for_other_episode_is_excluded(self):
        self.assertFalse(suitable_file("Example 03.srt", 2))
        self.assertFalse(suitable_file("Example #03.srt", 2))
        self.assertTrue(suitable_file("Example 02.srt", 2))
        self.assertTrue(suitable_file("Example.zip", 2))

    def test_post_title_accepts_subtitle_tag_and_checks_season_and_episode(self):
        self.assertEqual(matching_post("[자막] 예시 작품 02화 자막", ["예시 작품"], 1, 2), "exact")
        self.assertEqual(matching_post("[자막] 예시 작품 03화 자막", ["예시 작품"], 1, 2), "none")
        self.assertEqual(matching_post("예시 작품 2기 02화 자막", ["예시 작품 2기"], 1, 2), "none")
        self.assertEqual(matching_post("예시 작품 2기 02화 자막", ["예시 작품 2기"], 2, 2), "exact")
        self.assertEqual(matching_post("예시 작품 1기/2기 02화 자막", ["예시 작품 2기"], 2, 2), "exact")
        self.assertEqual(matching_post("예시 작품 (Example 2026) 02화 자막",
                                       ["예시 작품", "Example"], 1, 2), "exact")

    def test_specials_and_sequel_markers_are_not_folded_into_tv_season(self):
        self.assertTrue(is_special("예시 작품 OVA 01화"))
        self.assertFalse(is_special(without_mixed_specials("예시 작품 TV + OVA 01화")))
        self.assertEqual(matching_post("예시 작품 OVA 01화", ["예시 작품"], 1, 1), "none")
        self.assertEqual(season_of("예시 작품 II"), 2)
        self.assertEqual(season_of("예시 작품 2nd Season"), 2)
        self.assertIsNone(season_of("예시 작품 Part 2"))

    def test_safe_title_queries_preserve_season_suffix(self):
        self.assertEqual(title_queries("예시 작품 - 멋진 부제", ["예시 작품 2기", "Example"]),
                         ["예시 작품 - 멋진 부제", "예시 작품", "예시 작품 2기"])
        self.assertEqual(title_queries("예시 작품 - 2기"), ["예시 작품 - 2기"])

    def test_double_tilde_subtitle_can_be_omitted_from_search(self):
        full = "아가씨 돌보기 ~~영애들이 다니는 명문 학교의 시중 담당~~"
        self.assertEqual(title_queries(full), [full, "아가씨 돌보기"])
        for marker in ("2기", "II", "Part 2", "Cour 2", "극장판", "OVA", "특별편"):
            with self.subTest(marker=marker):
                subject = f"예시 작품 ~~{marker}~~"
                self.assertEqual(title_queries(subject), [subject])

    def test_registered_post_confirms_complete_short_title(self):
        full = "아가씨 돌보기 영애들이 다니는 명문 학교의 시중 담당"
        for post in ("아가씨 돌보기 12화 자막", "[자막] 아가씨돌보기 10~11화 자막",
                     "아가씨 돌보기 전체 자막"):
            with self.subTest(post=post):
                short = blog_title_alias(post, [full])
                self.assertIn(short, ("아가씨 돌보기", "아가씨돌보기"))
        self.assertEqual(blog_title_alias("예시 작품 2기 12화 자막", ["예시 작품 2기 ~~긴 부제~~"], 2),
                         "예시 작품 2기")

    def test_registered_post_cannot_confirm_partial_or_unrelated_words(self):
        full = "아가씨 돌보기 ~~명문 학교의 시중 담당~~"
        for post in ("아가씨 돌보 12화 자막", "돌보기 12화 자막", "아가씨 다른 작품 12화 자막",
                     "아가씨 돌보기 OVA 12화 자막", "아가씨 돌보기 2기 12화 자막"):
            with self.subTest(post=post):
                self.assertIsNone(blog_title_alias(post, [full]))

    def test_registered_post_cannot_remove_sequel_or_special_markers(self):
        for marker in ("2기", "II", "Part 2", "Cour 2", "극장판", "OVA", "특별편"):
            with self.subTest(marker=marker):
                self.assertIsNone(blog_title_alias("예시 작품 12화 자막", [f"예시 작품 {marker} ~~긴 부제~~"]))
        self.assertIsNone(blog_title_alias("예시 작품 12화 자막", ["예시 작품 ~~긴 부제~~"], 2))

    def test_public_aggregate_title_is_reviewable_but_other_episode_is_not(self):
        self.assertEqual(matching_post("예시 작품 자막", ["예시 작품"], 1, 2), "none")
        self.assertEqual(matching_public_title("예시 작품 자막", ["예시 작품"], 1, 2), "exact")
        self.assertEqual(matching_public_title("[자막] 예시 작품 (Example 2026) 전체 자막",
                                               ["예시 작품", "Example"], 1, 2), "exact")
        self.assertEqual(matching_public_title("예시 작품 03화 자막", ["예시 작품"], 1, 2), "none")
        self.assertEqual(matching_public_title("예시 작품 OVA 자막", ["예시 작품"], 1, 2), "none")


if __name__ == "__main__":
    unittest.main()

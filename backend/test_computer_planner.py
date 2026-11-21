import unittest

from computer_planner import build_action_proposal, parse_agent_candidate


class ComputerPlannerTests(unittest.TestCase):
    def test_agent_candidate_is_strictly_parsed_and_limited_to_allowed_operations(self):
        candidate = parse_agent_candidate(
            '{"operation":"desktop.click","target_element_id":"element-1",'
            '"rationale":"Unique visible button"}',
            {"desktop.click"},
        )
        self.assertEqual(candidate["target_element_id"], "element-1")
        for invalid in (
            '{"operation":"browser.submit","target_element_id":"element-1",'
            '"rationale":"Wrong source"}',
            '{"operation":"desktop.click","target_element_id":"element-1",'
            '"target_element_id":"element-2","rationale":"Duplicate"}',
            "```json\n{}",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_agent_candidate(invalid, {"desktop.click"})

    def test_click_proposal_maps_center_and_binds_observation(self):
        observation = {
            "observation_id": "observation-a",
            "device_id": "device-a",
            "source": "screen",
            "screen_dimensions": {"width": 100, "height": 80},
            "image_sha256": "a" * 64,
            "active_window_identity": "b" * 64,
            "timestamp": "2026-11-21T12:00:00+00:00",
            "expires_at": "2026-11-21T12:15:00+00:00",
            "elements": [
                {
                    "element_id": "element-1",
                    "type": "button",
                    "confidence": 0.95,
                    "ambiguous": False,
                    "bounding_box": {"x": 10, "y": 20, "width": 30, "height": 10},
                }
            ],
        }
        action, permission, confidence, payload = build_action_proposal(
            "desktop.click",
            instruction="Click Continue",
            observation=observation,
            target_element_id="element-1",
            expected_result={"kind": "element_visible", "text": "Done"},
        )
        self.assertEqual((action, permission), ("click", "click"))
        self.assertEqual(confidence, 0.95)
        self.assertEqual(
            payload,
            {
                "x": 25,
                "y": 25,
                "screen_width": 100,
                "screen_height": 80,
                "observation_id": "observation-a",
                "device_id": "device-a",
                "target_element_id": "element-1",
                "target_bbox": {"x": 10, "y": 20, "width": 30, "height": 10},
                "observation_captured_at": "2026-11-21T12:00:00+00:00",
                "observation_expires_at": "2026-11-21T12:15:00+00:00",
                "observation_image_sha256": "a" * 64,
                "active_window_identity": "b" * 64,
                "expected_result": {"kind": "element_visible", "text": "Done"},
            },
        )

    def test_low_confidence_and_stale_targets_are_rejected(self):
        observation = {
            "observation_id": "observation-a",
            "device_id": "device-a",
            "source": "screen",
            "screen_dimensions": {"width": 100, "height": 80},
            "image_sha256": "a" * 64,
            "active_window_identity": "b" * 64,
            "timestamp": "2026-11-21T12:00:00+00:00",
            "expires_at": "2026-11-21T12:15:00+00:00",
            "elements": [
                {
                    "element_id": "uncertain",
                    "type": "button",
                    "confidence": 0.7,
                    "ambiguous": True,
                    "bounding_box": {"x": 10, "y": 20, "width": 30, "height": 10},
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            build_action_proposal(
                "desktop.click",
                instruction="Click the uncertain button",
                observation=observation,
                target_element_id="uncertain",
                expected_result={"kind": "element_visible", "text": "Done"},
            )
        with self.assertRaisesRegex(ValueError, "not in the observation"):
            build_action_proposal(
                "desktop.click",
                instruction="Click a missing button",
                observation=observation,
                target_element_id="missing",
                expected_result={"kind": "element_visible", "text": "Done"},
            )

    def test_browser_operations_require_explicit_session_and_expected_result(self):
        with self.assertRaisesRegex(ValueError, "session and URL"):
            build_action_proposal(
                "browser.navigate",
                instruction="Open the approved page",
                url="https://example.com/",
            )
        with self.assertRaisesRegex(ValueError, "expected result"):
            build_action_proposal(
                "browser.submit",
                instruction="Submit the approved form",
                observation={
                    "source": "browser_dom",
                    "browser_session_id": "browser-session-a",
                    "page_identity": "12345678-1234-1234-1234-123456789abc",
                    "navigation_generation": 1,
                    "elements": [
                        {
                            "element_id": "control-a",
                            "type": "button",
                            "confidence": 1.0,
                            "ambiguous": False,
                        }
                    ],
                },
                session_id="browser-session-a",
                target_element_id="control-a",
            )


if __name__ == "__main__":
    unittest.main()

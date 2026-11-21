import io
import unittest
from unittest.mock import patch

from PIL import Image

from vision_engine import (
    MAX_SCREENSHOT_BYTES,
    MockVisionProvider,
    VisionEngine,
)


def screenshot_bytes(width=64, height=48, image_format="PNG"):
    image = Image.new("RGB", (width, height), color=(20, 40, 60))
    output = io.BytesIO()
    image.save(output, format=image_format)
    image.close()
    return output.getvalue()


class FixedVisionProvider:
    def __init__(self, elements):
        self.elements = elements
        self.image_bytes = None

    def analyze(self, image_bytes, _screen_width, _screen_height):
        self.image_bytes = image_bytes
        return self.elements


class VisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.provider_env = patch.dict("os.environ", {"NEX_VISION_PROVIDER": "mock"})
        self.provider_env.start()

    def tearDown(self):
        self.provider_env.stop()

    def test_mock_provider_returns_structured_owner_neutral_observation(self):
        with patch.dict("os.environ", {"NEX_VISION_PROVIDER": "mock"}):
            observation = VisionEngine().analyze_screenshot(
                screenshot_bytes(), "device-a"
            )

        self.assertEqual(observation["device_id"], "device-a")
        self.assertEqual(observation["screen_dimensions"], {"width": 64, "height": 48})
        self.assertEqual(observation["elements"], [])
        self.assertTrue(observation["observation_id"])
        self.assertTrue(observation["timestamp"])
        self.assertIsInstance(MockVisionProvider(), MockVisionProvider)

    def test_elements_are_bounded_and_low_confidence_is_ambiguous(self):
        provider = FixedVisionProvider(
            [
                {
                    "type": "button",
                    "description": "Continue",
                    "x": 4,
                    "y": 5,
                    "width": 20,
                    "height": 10,
                    "confidence": 0.6,
                }
            ]
        )
        observation = VisionEngine(provider).analyze_screenshot(
            screenshot_bytes(), "device-a"
        )
        element = observation["elements"][0]
        self.assertEqual(element["element_id"], "element-1")
        self.assertEqual(element["bounding_box"]["x"], 4)
        self.assertTrue(element["ambiguous"])

    def test_invalid_images_and_out_of_bounds_boxes_are_rejected(self):
        engine = VisionEngine()
        with self.assertRaises(ValueError):
            engine.analyze_screenshot(b"not an image", "device-a")
        with self.assertRaises(ValueError):
            engine.analyze_screenshot(b"x" * (MAX_SCREENSHOT_BYTES + 1), "device-a")

        invalid_provider = FixedVisionProvider(
            [
                {
                    "type": "button",
                    "description": "Outside image",
                    "x": 60,
                    "y": 0,
                    "width": 10,
                    "height": 10,
                    "confidence": 0.99,
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "out-of-bounds"):
            VisionEngine(invalid_provider).analyze_screenshot(
                screenshot_bytes(), "device-a"
            )

    def test_oversized_dimensions_and_unknown_provider_fail_closed(self):
        with patch.dict("os.environ", {"NEX_VISION_PROVIDER": "mock"}):
            with self.assertRaisesRegex(ValueError, "dimensions"):
                VisionEngine().analyze_screenshot(
                    screenshot_bytes(width=4097, height=1), "device-a"
                )
        with patch.dict("os.environ", {"NEX_VISION_PROVIDER": "unconfigured"}):
            with self.assertRaises(RuntimeError):
                VisionEngine().analyze_screenshot(screenshot_bytes(), "device-a")

    def test_remote_provider_requires_redaction_and_receives_redacted_pixels(self):
        provider = FixedVisionProvider([])
        with patch.dict("os.environ", {"NEX_VISION_PROVIDER": "openai"}):
            with self.assertRaisesRegex(ValueError, "redaction"):
                VisionEngine(provider).analyze_screenshot(
                    screenshot_bytes(), "device-a"
                )
            VisionEngine(provider).analyze_screenshot(
                screenshot_bytes(),
                "device-a",
                provider_name="openai",
                redaction_boxes=[
                    {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}
                ],
            )
        with Image.open(io.BytesIO(provider.image_bytes)) as redacted:
            redacted.load()
            self.assertLess(sum(redacted.getpixel((32, 24))), 40)


if __name__ == "__main__":
    unittest.main()

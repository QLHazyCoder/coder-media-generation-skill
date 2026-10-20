from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import media_catalog


class MediaCatalogTest(unittest.TestCase):
    def test_live_catalog_maps_groups_without_exposing_price_data(self) -> None:
        payload = {
            "data": [
                {
                    "model_name": "grok-imagine-video",
                    "supported_endpoint_types": ["openai-video"],
                    "enable_groups": ["视频模型", "新增公开视频组"],
                    "billing_expr": "must not be returned",
                },
                {
                    "model_name": "future-video-model",
                    "supported_endpoint_types": ["openai-video"],
                    "enable_groups": ["新增公开视频组"],
                    "billing_expr": "must not be returned",
                },
                {
                    "model_name": "gpt-image-2",
                    "supported_endpoint_types": ["image-generation"],
                    "enable_groups": ["image"],
                },
            ]
        }
        catalog = media_catalog.catalog_from_payload(payload, {"gpt-image-2"})
        groups = media_catalog.video_groups_by_name(catalog)
        self.assertEqual(groups["视频模型"]["models"], ["grok-imagine-video"])
        self.assertEqual(groups["新增公开视频组"]["models"], ["grok-imagine-video"])
        self.assertEqual(groups["新增公开视频组"]["unconfigured_models"], ["future-video-model"])
        self.assertEqual(catalog["image_models"], ["gpt-image-2"])
        self.assertNotIn("billing_expr", str(catalog))

    def test_bundled_snapshot_has_five_exact_video_group_profiles(self) -> None:
        capabilities = media_catalog.load_capabilities()
        catalog = media_catalog._fallback_catalog(capabilities, set(capabilities["image_models"]), "offline")
        groups = media_catalog.video_groups_by_name(catalog)
        self.assertEqual(len(groups), 5)
        self.assertEqual(
            set(groups),
            {
                "视频模型",
                "seedance官渠 720&1080p 国内",
                "视频-seedance官渠-全分辨率-国内",
                "seedance官渠 全分辨率 海外版",
                "seedance官渠 720p 海外版",
            },
        )
        self.assertEqual(len(groups["seedance官渠 720p 海外版"]["models"]), 4)

    def test_malformed_remote_group_names_are_ignored(self) -> None:
        payload = {
            "data": [{
                "model_name": "grok-imagine-video",
                "supported_endpoint_types": ["openai-video"],
                "enable_groups": ["valid", "", "x\ncommand"],
            }]
        }
        catalog = media_catalog.catalog_from_payload(payload, set())
        self.assertEqual([group["group"] for group in catalog["video_groups"]], ["valid"])


if __name__ == "__main__":
    unittest.main()

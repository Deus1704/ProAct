import unittest

from proactive_assistant_app import app as app_module
from proactive_assistant_app import uber_client


class LiveUberEstimateTests(unittest.TestCase):
    def test_parse_dom_product_candidate_extracts_type_price_and_eta(self) -> None:
        candidate = uber_client._parse_dom_product_candidate("Uber Go 6 mins ₹143")

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["ride_type"], "Uber Go")
        self.assertEqual(candidate["eta_minutes"], 6)
        self.assertEqual(candidate["high_estimate"], 143.0)

    def test_strict_live_recommendation_requires_real_live_fields(self) -> None:
        valid = {
            "recommended_option": {
                "live_data": True,
                "ride_type": "UberXL",
                "price": 320.0,
                "eta": 7,
            }
        }
        invalid = {
            "recommended_option": {
                "live_data": False,
                "ride_type": "UberXL",
                "price": 320.0,
                "eta": 7,
            }
        }

        self.assertTrue(app_module._has_strict_live_recommendation(valid))
        self.assertFalse(app_module._has_strict_live_recommendation(invalid))


if __name__ == "__main__":
    unittest.main()

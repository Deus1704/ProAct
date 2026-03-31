import unittest

from proactive_assistant_app import ola_client


class OlaClientParsingTests(unittest.TestCase):
    def test_parse_my_rides_response_maps_bookings_to_internal_rides(self) -> None:
        payload = {
            "bookings": [
                {
                    "status": "SUCCESS",
                    "booking_id": "CRN123456789",
                    "pickup_lat": 28.581828,
                    "pickup_lng": 77.1864273,
                    "drop_lat": 28.521828,
                    "drop_lng": 77.186427,
                    "booking_status": "COMPLETED",
                    "booking_time": "Thu, 01 Sep 2016 00:01:22 IST +05:30",
                    "category": "mini",
                    "merchant_txn_id": "Aabc-12123if-681gda1",
                    "pickup_address": "Cyber City, Gurugram",
                    "drop_address": "T3 Airport, Delhi",
                }
            ]
        }

        rides = ola_client.parse_my_rides_response(payload)

        self.assertEqual(len(rides), 1)
        self.assertEqual(rides[0]["external_ride_id"], "CRN123456789")
        self.assertEqual(rides[0]["source_platform"], "ola")
        self.assertEqual(rides[0]["pickup_address"], "Cyber City, Gurugram")
        self.assertEqual(rides[0]["dropoff_address"], "T3 Airport, Delhi")
        self.assertEqual(rides[0]["pickup_lat"], 28.581828)
        self.assertEqual(rides[0]["pickup_lng"], 77.1864273)
        self.assertEqual(rides[0]["dropoff_lat"], 28.521828)
        self.assertEqual(rides[0]["dropoff_lng"], 77.186427)
        self.assertEqual(rides[0]["ride_type"], "mini")
        self.assertEqual(rides[0]["raw_payload"]["booking_status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()

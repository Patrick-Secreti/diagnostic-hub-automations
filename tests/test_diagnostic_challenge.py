import unittest
from unittest.mock import patch
import diagnostic_challenge as d

class ChallengeTests(unittest.TestCase):
    def sample(self):
        return dict(vehicle="Example vehicle", concern="Intermittent misfire",
            codes="P0300", findings="Misfire on bank one",
            additional="Voltage drop on injector feed",
            root_cause="High resistance fuse block", diagnosis="Isolated voltage drop",
            resolution="Replaced block and verified repair", takeaway="Test under load")

    def test_validate(self):
        self.assertEqual(d.validate(self.sample()), self.sample())
        with self.assertRaises(ValueError):
            d.validate(dict(self.sample(),vehicle=""))

    def test_staged_payload(self):
        c=self.sample()
        r={"number":1,"case":c}
        first=d.payload(r,"case")["embeds"][0]["description"]
        hint=d.payload(r,"hint")["embeds"][0]["description"]
        final=d.payload(r,"resolution")["embeds"][0]["description"]
        self.assertNotIn(c["root_cause"],first)
        self.assertNotIn(c["additional"],first)
        self.assertIn(c["additional"],hint)
        self.assertNotIn(c["root_cause"],hint)
        self.assertIn(c["root_cause"],final)
        self.assertIn(c["resolution"],final)

    @patch("diagnostic_challenge.api")
    def test_review_retries(self,api):
        c=self.sample()
        api.side_effect=[c,{"valid":False,"reason":"Insufficient evidence"},c,{"valid":True}]
        self.assertEqual(d.generate([]),c)
        self.assertEqual(api.call_count,4)
        self.assertIn("Insufficient evidence",api.call_args_list[2].args[0])
        self.assertIn("Revise this previous candidate",api.call_args_list[2].args[0])

    @patch("diagnostic_challenge.api")
    def test_review_failure(self,api):
        api.side_effect=[self.sample(),{"valid":False}]*5
        with self.assertRaisesRegex(ValueError,"No case passed"):
            d.generate([])

import unittest
from unittest.mock import patch, Mock
import ase_question as ase

class ASETests(unittest.TestCase):
    def record(self):
        return {"date":"2026-11-24","number":1,"category":["A6","Electrical/Electronic Systems"],
                "question":{"stem":"A 5 V reference returns when a sensor is disconnected. What is the likely cause?",
                            "topic":"Reference voltage","options":["Open ground","Shorted sensor","Open fuse","Open signal"],
                            "answer":"B","explanation":"An internal short can pull down the shared reference."}}

    def test_question_hides_answer(self):
        p=ase.question_payload(self.record())
        self.assertNotIn("Correct answer",str(p))
        self.assertNotIn("internal short",str(p))
        self.assertEqual(len(p["poll"]["answers"]),4)
        self.assertEqual(p["allowed_mentions"],{"parse":[]})

    def test_answer_matches_saved_question(self):
        p=ase.answer_payload(self.record())
        self.assertIn("B. Shorted sensor",p["embeds"][0]["description"])

    def test_validation(self):
        q=self.record()["question"]
        self.assertEqual(ase.validate(q),q)
        with self.assertRaises(ValueError):
            ase.validate(dict(q,options=["same"]*4))

    @patch("ase_question.api")
    def test_blind_review_disagreement(self,api):
        api.side_effect=[self.record()["question"],{"valid":True,"answer":"A"}]
        with self.assertRaises(ValueError):
            ase.generate_once(ase.CATEGORIES[0],[])
        self.assertEqual(api.call_count,2)

    @patch("ase_question.requests.post")
    def test_delivery_requires_message_id(self,post):
        post.return_value=Mock(status_code=200)
        post.return_value.json.return_value={"id":"123"}
        self.assertEqual(ase.deliver("https://example.com",{}),"123")
        post.return_value.json.return_value={}
        with self.assertRaises(ValueError):
            ase.deliver("https://example.com",{})

    @patch("ase_question.api")
    def test_regenerates_after_review_rejection(self,api):
        q=self.record()["question"]
        api.side_effect=[q,{"valid":False,"answer":"A","reason":"Ambiguous conditions"},
                         q,{"valid":True,"answer":"B","reason":"Clear"}]
        self.assertEqual(ase.generate(ase.CATEGORIES[0],[]),q)
        self.assertEqual(api.call_count,4)

    @patch("ase_question.api")
    def test_never_delivers_unverified_question(self,api):
        q=self.record()["question"]
        api.side_effect=[q,{"valid":False,"answer":"A","reason":"Ambiguous"}]*3
        with self.assertRaisesRegex(ValueError,"after three attempts"):
            ase.generate(ase.CATEGORIES[0],[])
        self.assertEqual(api.call_count,6)

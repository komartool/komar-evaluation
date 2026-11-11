import json
import unittest

from evaluation import dep_labels as D


def inv(fields, value=1):
    return {"self": {"$t": "x.Svc", "$fields": fields}, "args": [], "value": value, "calls": [],
            "outcome": "return", "void": False}


DATA = {"$t": "x.Config", "$id": 2, "$fields": {"limit": 5}}
MOCK = {"$t": "x.Config$MockitoMock$abc", "$opaque": True}


class ParseTest(unittest.TestCase):
    def test_fenced_json(self):
        text = '```json\n{"fields": [{"name": "a", "label": "state", "reason": "r"}]}\n```'
        self.assertEqual(D.parse_response(text), {"a": {"label": "STATE", "reason": "r"}})

    def test_bad_label(self):
        with self.assertRaises(ValueError):
            D.parse_response('{"fields": [{"name": "a", "label": "MAYBE"}]}')

    def test_no_json(self):
        with self.assertRaises(ValueError):
            D.parse_response("no idea")


class AgreementTest(unittest.TestCase):
    def test_kappa(self):
        self.assertEqual(D.cohen_kappa(["STATE", "DEPENDENCY"], ["STATE", "DEPENDENCY"]), 1.0)
        self.assertAlmostEqual(D.cohen_kappa(["STATE", "STATE", "DEPENDENCY", "DEPENDENCY"],
                                             ["STATE", "DEPENDENCY", "STATE", "DEPENDENCY"]), 0.0)

    def test_majority(self):
        self.assertEqual(D.majority(["STATE", "DEPENDENCY", "STATE"]), "STATE")
        self.assertIsNone(D.majority(["STATE", "DEPENDENCY"]))
        self.assertIsNone(D.majority([None, None]))


class FieldsTest(unittest.TestCase):
    def test_receiver_fields(self):
        f = D.receiver_fields([inv({"config": DATA, "n": 3, "items": [1]})])
        self.assertTrue(f["config"]["object"])
        self.assertFalse(f["n"]["object"])
        self.assertFalse(f["items"]["object"])
        self.assertEqual(f["config"]["classes"], ["x.Config"])


class PromptTest(unittest.TestCase):
    def test_prompts_have_the_placeholders(self):
        from pathlib import Path
        d = Path(__file__).resolve().parents[1] / "evaluation" / "labelling-prompts"
        for phrasing in ("p1", "p2"):
            text = (d / f"user-{phrasing}.txt").read_text()
            for key in ("{target_class}", "{target_method}", "{class_source}", "{fields}", "{tests}"):
                self.assertIn(key, text, (phrasing, key))
            self.assertTrue((d / f"system-{phrasing}.txt").read_text().strip())


if __name__ == "__main__":
    unittest.main()

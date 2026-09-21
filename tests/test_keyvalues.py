"""Parsing must preserve the structure and reject partial or ambiguous input."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from keyvalues import TypedValue, parse, to_json


class KeyValuesTests(unittest.TestCase):
    def test_nested_types_and_scalars(self):
        data = parse(r"""<!-- kv3 encoding:text -->
        { quoted = "12" number = -1.25e2 flag = true nothing = null
          child = subclass:{ arr = [resource_name:"a.vpcf", 0, false] }
          // comments may contain { } = "quotes"
          /* block comment */ text = "a\\b\"c // text"
        }""")
        self.assertEqual(data["quoted"], "12")
        self.assertEqual(data["number"], -125)
        self.assertIs(data["flag"], True)
        self.assertIsNone(data["nothing"])
        self.assertEqual(
            data["child"],
            TypedValue(
                "subclass",
                {
                    "arr": [TypedValue("resource_name", "a.vpcf"), 0, False],
                },
            ),
        )
        self.assertEqual(json.loads(to_json(data))["child"]["$type"], "subclass")

    def test_localization_escapes_comments_and_multiple_pairs(self):
        data = parse(
            r""""lang" { "Tokens" {
          "a" "A \"quote\"" "b" "https://example.test/"
          // "ignored" "ignored"
          "c" "line\nnext" }
        }""",
            kv1=True,
        )
        self.assertEqual(
            data["lang"]["Tokens"],
            {
                "a": 'A "quote"',
                "b": "https://example.test/",
                "c": "line\nnext",
            },
        )

    def test_rejects_invalid_and_duplicate_definitions(self):
        for text in (
            "{ a = 1",
            "{a=1 a=2}",
            "{a=[1,2}",
            '{a="unterminated}',
            "{a=1} trailing",
            "{a=#[00 11]}",
            "{a=1 /* unterminated",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse(text)
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            parse('"lang" {"a" "1" "a" "2"}', kv1=True)

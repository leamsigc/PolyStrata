"""setup.py's conversation-cache flags: given on setup's command line they win over the one question setup asks, and
when neither names them nothing is written, so a start stays byte-identical to what it was (opt-in, recommend never
force). No GPU, no downloads, no prompts.

    python -m unittest tools.test_setup_conv_cache
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import setup  # noqa: E402


def asked(mib=None, slots=None) -> argparse.Namespace:
    """setup's parsed args (the two flags are the only ones this helper cares about)."""
    return argparse.Namespace(conversation_cache_mib=mib, conversation_cache_slots=slots)


class GivenOnTheCommandLine(unittest.TestCase):
    def test_nothing_given_writes_nothing(self):
        self.assertEqual(setup.conv_cache_args(asked()), [])
        self.assertEqual(setup.conv_cache_args(asked(), 0), [])          # the ask answered "n"

    def test_mib_alone(self):
        self.assertEqual(setup.conv_cache_args(asked(8192)), ["--conversation-cache-mib", "8192"])

    def test_mib_and_slots(self):
        self.assertEqual(setup.conv_cache_args(asked(8192, 8)),
                         ["--conversation-cache-mib", "8192", "--conversation-cache-slots", "8"])

    def test_slots_need_the_mib(self):
        # the engine's own default is 4 parked conversations, and a budget of 0 means the cache is off: slots alone do
        # nothing, so they are not written
        self.assertEqual(setup.conv_cache_args(asked(None, 8)), [])

    def test_the_command_line_wins_over_the_ask(self):
        self.assertEqual(setup.conv_cache_args(asked(12288), setup.AGENT_CACHE_MIB),
                         ["--conversation-cache-mib", "12288"])

    def test_the_ask_is_used_when_the_command_line_is_empty(self):
        self.assertEqual(setup.conv_cache_args(asked(), "auto"), ["--conversation-cache-mib", "auto"])
        self.assertEqual(setup.conv_cache_args(asked(slots=6), setup.AGENT_CACHE_MIB),
                         ["--conversation-cache-mib", str(setup.AGENT_CACHE_MIB), "--conversation-cache-slots", "6"])


if __name__ == "__main__":
    unittest.main()

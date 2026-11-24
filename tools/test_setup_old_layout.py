"""A run config kept on an outside data dir still names the pre-0.2.16 root-layout files: setup rebases a path
only when the old one is missing and the new one (inside terminal/) is there. No GPU, no downloads.

    python -m unittest tools.test_setup_old_layout
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "terminal"
sys.path.insert(0, str(ROOT))
import setup  # noqa: E402


class OldLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.checkout = self.tmp / "checkout"
        self.root = self.checkout / "terminal"
        (self.root / "engine").mkdir(parents=True)
        (self.root / "engine" / setup.EXE).write_text("engine")
        (self.root / "models").mkdir()
        patcher = patch.object(setup, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exe_is_rebased_from_the_root_engine(self):
        old = self.checkout / "engine" / setup.EXE
        cfg = {"exe": str(old), "args": ["--model", str(self.tmp / "model.gguf")]}
        new, changed = setup._rebase_old_layout_path(cfg)
        self.assertTrue(changed)
        self.assertEqual(new["exe"], str(self.root / "engine" / setup.EXE))
        self.assertFalse(old.exists())                       # nothing was copied or moved

    def test_a_path_that_exists_at_the_old_place_is_left_alone(self):
        old = self.checkout / "models" / "kept.gguf"
        old.parent.mkdir(exist_ok=True)
        old.write_text("kept")
        cfg = {"exe": str(self.checkout / "engine" / setup.EXE), "args": ["--model", str(old)]}
        new, changed = setup._rebase_old_layout_path(cfg)
        self.assertTrue(changed)
        self.assertEqual(new["args"][1], str(old))            # a file the user chose there keeps working

    def test_upgrade_config_persists_the_rebase(self):
        old = self.checkout / "engine" / setup.EXE
        cfg_path = self.tmp / "strata-iq2_xs.json"
        cfg = {"exe": str(old), "args": ["--model", "/data/model.gguf"]}
        cfg_path.write_text(json.dumps(cfg))
        setup.upgrade_config(cfg_path, cfg)
        self.assertEqual(json.loads(cfg_path.read_text())["exe"], str(self.root / "engine" / setup.EXE))

    def test_a_config_with_no_old_paths_is_unchanged(self):
        cfg = {"exe": str(self.root / "engine" / setup.EXE), "args": ["--model", "/data/model.gguf"]}
        new, changed = setup._rebase_old_layout_path(cfg)
        self.assertFalse(changed)
        self.assertEqual(new, cfg)


if __name__ == "__main__":
    unittest.main()

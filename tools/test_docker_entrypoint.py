"""docker-entrypoint.sh keeps the install config on the /data volume as the one that counts (#1244): with REINSTALL=0
and a config on the volume, /opt/strata/strata-<tag>.json is a link to it, even when a regular file is already there.
It also passes the setup env vars (PARALLEL, CONVERSATION_CACHE_*) only when they are set. Runs the real script in a
temporary tree with a stub for .venv/bin/python (POSIX sh and symlinks needed: skipped on Windows).

    python -m unittest tools.test_docker_entrypoint
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")


@unittest.skipIf(os.name == "nt" or SH is None, "needs POSIX sh and symlinks")
class Entrypoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.opt = self.tmp / "opt"
        self.data = self.tmp / "data"
        (self.opt / ".venv" / "bin").mkdir(parents=True)
        (self.data / "config").mkdir(parents=True)
        # the stub "python": setup.py --setup writes the config the way setup does (and prints the args it was given);
        # a plain start prints what it would load
        stub = self.opt / ".venv" / "bin" / "python"
        stub.write_text('#!/bin/sh\nif [ "$2" = "--setup" ]; then echo "SETUP ARGS: $*"; echo \'{"args": ["from-setup"]}\' > strata-iq3_s.json\n'
                        'else echo "STARTED WITH: $(cat strata-iq3_s.json)"; fi\n', encoding="utf-8")
        stub.chmod(0o755)
        script = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8").replace("/opt/strata", str(self.opt))
        self.script = self.tmp / "entry.sh"
        self.script.write_text(script, encoding="utf-8")

    def run_entry(self, reinstall, **extra):
        env = dict(os.environ, STRATA_DATA=str(self.data), MODEL="IQ3_S", REINSTALL=reinstall, **extra)
        r = subprocess.run([SH, str(self.script)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_edited_volume_config_wins_over_a_regular_file_in_opt(self):
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["edited", "--kv-resident", "32768"]}\n')
        (self.opt / "strata-iq3_s.json").write_text('{"args": ["stale"]}\n')      # left by an earlier setup
        out = self.run_entry("0")
        self.assertIn("edited", out)
        self.assertNotIn("stale", out)
        self.assertTrue((self.opt / "strata-iq3_s.json").is_symlink())

    def test_link_is_made_when_there_is_no_file(self):
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["on-volume"]}\n')
        self.assertIn("on-volume", self.run_entry("0"))

    def test_first_setup_copies_the_config_to_the_volume_and_the_next_start_links_it(self):
        out = self.run_entry("0")                                    # no config on the volume: setup runs
        self.assertIn("from-setup", out)
        cfg = self.data / "config" / "strata-iq3_s.json"
        self.assertIn("from-setup", cfg.read_text())
        cfg.write_text('{"args": ["edited-later"]}\n')
        self.assertIn("edited-later", self.run_entry("0"))           # the regular file setup left is replaced by the link

    def test_existing_link_into_the_data_config_dir_is_kept(self):      # a pod command that picked a config by linking
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["plain"]}\n')
        other = self.data / "config" / "strata-iq3_s.1x4.json"
        other.write_text('{"args": ["batch8"]}\n')
        os.symlink(other, self.opt / "strata-iq3_s.json")
        out = self.run_entry("0")
        self.assertIn("batch8", out)
        self.assertNotIn("plain", out)
        self.assertIn(f"Config: {other}", out)

    def test_link_to_somewhere_else_is_replaced(self):
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["on-volume"]}\n')
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text('{"args": ["elsewhere"]}\n')
        os.symlink(elsewhere, self.opt / "strata-iq3_s.json")
        self.assertIn("on-volume", self.run_entry("0"))

    def test_config_env_wins(self):
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["plain"]}\n')
        other = self.tmp / "mine.json"
        other.write_text('{"args": ["mine"]}\n')
        os.symlink(self.data / "config" / "strata-iq3_s.json", self.opt / "strata-iq3_s.json")
        out = self.run_entry("0", CONFIG=str(other))
        self.assertIn("mine", out)
        self.assertNotIn("plain", out)
        self.assertIn(f"Config: {other}", out)

    def test_model_names_the_config_and_it_is_printed(self):
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["by-model"]}\n')
        out = self.run_entry("0")
        self.assertIn("by-model", out)
        self.assertIn(f"Config: {self.data}/config/strata-iq3_s.json", out)

    def test_parallel_and_conversation_cache_reach_the_setup_pass(self):
        out = self.run_entry("1", PARALLEL="10", CONVERSATION_CACHE_MIB="8192", CONVERSATION_CACHE_SLOTS="8")
        self.assertIn("--parallel 10", out)
        self.assertIn("--conversation-cache-mib 8192", out)
        self.assertIn("--conversation-cache-slots 8", out)

    def test_an_unset_one_is_not_passed_so_setup_keeps_its_own_default(self):
        out = self.run_entry("1")                               # the engine's default: one request at a time
        self.assertNotIn("--parallel", out)
        self.assertNotIn("conversation-cache", out)             # empty: setup asks, and an existing config keeps it

    def test_they_are_not_repeated_on_a_later_start(self):
        # recorded in the config on the volume, so the start pass does not pass them again (REINSTALL is how they change)
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["on-volume"], "parallel": 10}\n')
        out = self.run_entry("0", PARALLEL="10")
        self.assertNotIn("--parallel", out)
        self.assertIn("on-volume", out)

    def test_model_env_reads_the_preset_inside_the_container(self):
        """A platform that builds the Dockerfile never reads docker-compose.yml: MODEL_ENV is applied here too, so
        the model it names is the one set up, not the entry point's defaults (qwen / IQ2_XS)."""
        (self.opt / ".env.coder").write_text("FAMILY=coder\nMODEL=IQ1_M\nPARALLEL=3\nREINSTALL=0\n", encoding="utf-8")
        out = self.run_entry("0", MODEL_ENV=".env.coder")
        self.assertIn("Preset: .env.coder (MODEL_ENV)", out)
        self.assertIn("--family coder --model IQ1_M", out)
        self.assertIn("--parallel 3", out)

    def test_a_missing_preset_stops_instead_of_serving_the_default_model(self):
        cfg = self.data / "config" / "strata-coder-iq1_m.json"
        cfg.write_text('{"args": ["coder"]}\n')
        r = subprocess.run([SH, str(self.script)],
                           env=dict(os.environ, STRATA_DATA=str(self.data), MODEL_ENV=".env.coder", REINSTALL="0"),
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MODEL_ENV=.env.coder: no such file", r.stderr)
        self.assertNotIn("coder", r.stdout)                    # it did not fall back to the default model

    def test_a_path_is_refused_and_a_preset_file_is_named(self):
        r = subprocess.run([SH, str(self.script)],
                           env=dict(os.environ, STRATA_DATA=str(self.data), MODEL_ENV="/data/config/.env",
                                    REINSTALL="0"), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a path", r.stderr)

    def test_reinstall_from_the_platform_still_asks_for_one_setup_pass(self):
        """The preset records REINSTALL=0; a REINSTALL=1 beside it is the platform's word, so it is kept."""
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["on-volume"]}\n')
        (self.opt / ".env.qwen").write_text("FAMILY=qwen\nMODEL=IQ2_XS\nREINSTALL=0\n", encoding="utf-8")
        out = self.run_entry("1", MODEL_ENV=".env.qwen")
        self.assertIn("SETUP ARGS", out)                       # the setup pass ran, as REINSTALL=1 asks
        self.assertNotIn("on-volume", out)

    def warning_for(self, flags):
        info = self.tmp / "cpuinfo"
        info.write_text(f"processor\t: 0\nflags\t\t: {flags}\n")
        (self.data / "config" / "strata-iq3_s.json").write_text('{"args": ["x"]}\n')
        env = dict(os.environ, STRATA_DATA=str(self.data), MODEL="IQ3_S", REINSTALL="0", STRATA_CPUINFO=str(info))
        r = subprocess.run([SH, str(self.script)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)     # a warning, never a stop (#1584)
        return r.stderr

    def test_a_cpu_without_avx2_is_warned_not_stopped(self):
        err = self.warning_for("fpu sse4_2 avx aes")
        self.assertIn("no AVX2", err)
        self.assertIn("docker build", err)

    def test_a_cpu_with_avx2_gets_no_warning(self):
        self.assertEqual(self.warning_for("fpu sse4_2 avx avx2 fma"), "")


if __name__ == "__main__":
    unittest.main()

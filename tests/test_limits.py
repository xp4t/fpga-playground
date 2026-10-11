"""Run real processes under kernel resource limits, without needing Bubblewrap."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import build_runner


@unittest.skipUnless(shutil.which('prlimit'), 'Linux prlimit required')
class LimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_python(self, code, **kwargs):
        return build_runner.run([sys.executable, '-c', code], self.cwd, **kwargs)

    def test_live_progress_and_bounded_output(self):
        chunks = []
        result = self.run_python("print('a' * 100000)", progress=chunks.append)
        self.assertEqual(len(result), 24000)
        self.assertIn('a' * 10, ''.join(chunks))

    def test_wall_timeout_kills_descendants(self):
        code = "import subprocess,time; subprocess.Popen(['sleep', '20']); print('started', flush=True); time.sleep(20)"
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'time limit'):
            self.run_python(code, timeout=0.2)
        self.assertLess(time.monotonic() - started, 2)

    def test_deadline_covers_multiple_tool_stages(self):
        deadline = time.monotonic() - 1
        with self.assertRaisesRegex(ValueError, 'time limit'):
            self.run_python('print(1)', deadline=deadline)

    def test_memory_allocation_is_limited(self):
        with mock.patch.object(build_runner, 'MEMORY_LIMIT', 96 * 1024 * 1024):
            with self.assertRaisesRegex(ValueError, 'MemoryError'):
                self.run_python("x = bytearray(200 * 1024 * 1024)")

    def test_cpu_bound_process_is_terminated(self):
        with mock.patch.object(build_runner, 'CPU_LIMIT', 1):
            with self.assertRaisesRegex(ValueError, 'limit'):
                self.run_python('while True: pass', timeout=3)

    def test_file_size_and_aggregate_disk_limits(self):
        with mock.patch.object(build_runner, 'FILE_LIMIT', 1024):
            with self.assertRaises(ValueError):
                self.run_python("f=open('large', 'wb');f.write(b'x' * 4096);f.flush()")
        (self.cwd / 'large').unlink(missing_ok=True)
        with mock.patch.object(build_runner, 'DISK_LIMIT', 2000):
            with self.assertRaisesRegex(ValueError, 'disk limit'):
                self.run_python("open('a', 'wb').write(b'x' * 1100); open('b', 'wb').write(b'x' * 1100)")

    def test_run_to_file_keeps_stdout_and_reports_stderr(self):
        result = build_runner.run_to_file([sys.executable, '-c', "import sys;print('frames');print('progress', file=sys.stderr)"],
                                         'frames', self.cwd)
        self.assertEqual((self.cwd / 'frames').read_text(), 'frames\n')
        self.assertEqual(result, 'progress\n')

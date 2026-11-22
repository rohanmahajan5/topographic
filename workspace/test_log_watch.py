"""log_watch.py: what a provider store's writes produce, natively (kqueue) and by metadata alone."""
import os
from pathlib import Path
import select
import shutil
import tempfile
import time
import unittest

from log_watch import LogWatch


def jsonl(parts, is_dir):
    return is_dir or parts[-1].endswith('.jsonl')


class LogWatchCases:
    native = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'projects'
        (self.root / 'p').mkdir(parents=True)
        self.old = self.root / 'p' / 'old.jsonl'
        self.old.write_text('{}\n')
        past = time.time() - 30 * 86400
        os.utime(self.old, (past, past))
        self.w = LogWatch({self.root: ('claude', jsonl)}, lambda c: None, native=self.native, cold_cycle=0.4, tick=0.05)
        self.w.start()
        self.w.stop_event.set(); self.w.thread.join()          # driven by hand below

    def tearDown(self):
        self.w.close()
        shutil.rmtree(self.tmp)

    def until(self, path, seconds=2.0):
        """Steps until `path` is reported; returns the seconds it took, or None."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            if path in self.w.step(0.05):
                return time.monotonic() - t0
        return None

    def test_new_file_and_append(self):
        f = self.root / 'p' / 'new.jsonl'
        f.write_text('{"a":1}\n')
        self.assertIsNotNone(self.until(f))
        with f.open('a') as h:
            h.write('{"a":2}\n')
        self.assertIsNotNone(self.until(f))

    def test_new_subagent_folder(self):
        f = self.root / 'p' / 'sess' / 'subagents' / 'agent-x.jsonl'
        f.parent.mkdir(parents=True)
        f.write_text('{}\n')
        self.assertIsNotNone(self.until(f))
        with f.open('a') as h:
            h.write('{}\n')
        self.assertIsNotNone(self.until(f))

    def test_other_files_ignored(self):
        f = self.root / 'p' / 'notes.txt'
        f.write_text('x')
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.3:
            self.assertNotIn(f, self.w.step(0.05))

    def test_cold_file_wakes_and_turns_hot(self):
        with self.old.open('a') as h:
            h.write('{}\n')
        self.assertIsNotNone(self.until(self.old))          # within about a cold cycle
        self.assertTrue(self.w.entries[self.old].hot)

    def test_replace_and_delete(self):
        f = self.root / 'p' / 'reg.jsonl'
        f.write_text('{}\n')
        self.assertIsNotNone(self.until(f))
        tmp = self.root / 'p' / '.reg.tmp'
        tmp.write_text('{"b":1}\n')
        os.replace(tmp, f)
        self.assertIsNotNone(self.until(f))
        f.unlink()
        self.assertIsNotNone(self.until(f))
        self.assertNotIn(f, self.w.entries)

    def test_store_created_later(self):
        root2 = self.tmp / 'later'
        w = LogWatch({root2: ('codex', jsonl)}, lambda c: None, native=self.native, cold_cycle=0.2, tick=0.05)
        w.start(); w.stop_event.set(); w.thread.join()
        try:
            root2.mkdir()
            (root2 / 'a.jsonl').write_text('{}\n')
            t0, seen = time.monotonic(), False
            while time.monotonic() - t0 < 2 and not seen:
                seen = root2 / 'a.jsonl' in w.step(0.05)
            self.assertTrue(seen)
        finally:
            w.close()


@unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue only')
class NativeLogWatch(LogWatchCases, unittest.TestCase):
    native = True

    def test_append_seen_within_milliseconds(self):
        f = self.root / 'p' / 'hot.jsonl'
        f.write_text('{}\n')
        self.assertIsNotNone(self.until(f))
        with f.open('a') as h:
            h.write('{}\n')
        took = self.until(f)
        self.assertIsNotNone(took)
        self.assertLess(took, 0.1)


class PolledLogWatch(LogWatchCases, unittest.TestCase):
    native = False


if __name__ == '__main__':
    unittest.main()

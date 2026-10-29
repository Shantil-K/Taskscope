import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from taskscope.sysinfo import datarec, perf, procs  # noqa: E402


class DataRecordingTest(unittest.TestCase):
    def test_record_and_replay(self):
        ps, pr = perf.PerfSampler(), procs.ProcessSampler()
        ps.sample()
        pr.sample()
        with tempfile.TemporaryDirectory() as d:
            rec = datarec.Recorder()
            path = rec.start(d)
            live = []
            for _ in range(3):
                snap = {"perf": ps.sample(), "procs": pr.sample()}
                live.append(snap)
                rec.add(snap)
            rec.stop()
            listed = datarec.list_recordings(d)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["frames"], 3)
            player = datarec.Player(path)
            self.assertEqual(len(player), 3)
            for i, snap in enumerate(live):
                f = player.frame(i)
                self.assertEqual(len(f["procs"]), len(snap["procs"]))
                want = {p.pid: (p.name, p.user, p.rss) for p in snap["procs"]}
                got = {p.pid: (p.name, p.user, p.rss) for p in f["procs"]}
                self.assertEqual(got, want)
                self.assertAlmostEqual(f["perf"]["cpu"]["total"], snap["perf"]["cpu"]["total"], places=1)

    def test_cut_off_recording_still_plays(self):
        with tempfile.TemporaryDirectory() as d:
            rec = datarec.Recorder()
            path = rec.start(d)
            rec.add({"perf": {"cpu": {"total": 1.0}}, "procs": []})
            rec.add({"perf": {"cpu": {"total": 2.0}}, "procs": []})
            rec.stop()
            import gzip
            data = gzip.open(path, "rb").read()
            with gzip.open(path, "wb") as f:              # chop the last line in half
                f.write(data[:-20])
            self.assertEqual(len(datarec.Player(path)), 1)


if __name__ == "__main__":
    unittest.main()

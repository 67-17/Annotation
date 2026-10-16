"""Offline screening checks; fake inference here is NOT GPU accuracy validation."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

import app as service
from batch import BatchManager, merge_state, parse_states, screening_result, windows
from test_annotation import request


class FakeDataset:
    episodes = {0: {}, 1: {}}

    def episode_meta(self, ep):
        return {"tasks": ["只判断目标插头"], "length": 4}

    def rows(self, ep):
        return [{"timestamp": i / 10, "frame_index": i} for i in range(4)]

    def samples(self, ep, side):
        return [{"time": i / 20} for i in range(7)]

    def cameras(self, ep):
        return [{"name": name, "key": name} for name in ("third_camera", "photon_left", "photon_right")]

    def frame(self, ep, camera, time):
        return np.zeros((2, 2, 3), dtype=np.uint8), round(time * 20)

    def curves(self, ep):
        return [{"side": side, "name": side + "/load", "unit": "uncalibrated", "points":
                 [[i / 20, v] for i, v in enumerate([0, 0, 1, 1, 1, 0, 0])]} for side in ("left", "right")]


class FakeModel:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.bad = False

    def infer(self, images, prompt):
        self.calls.append(images)
        if self.callback:
            self.callback()
        if self.bad:
            return {"text": "not JSON"}
        count = len(images) // 2
        return {"text": json.dumps({"states": ["no_contact"] * count})}


class AlgorithmTests(unittest.TestCase):
    def unit(self, states):
        return {"episode": 0, "side": "left", "times": list(range(len(states))), "states": states}

    def test_every_frame_and_overlap_including_last(self):
        for size in (2, 3, 6):
            for count in range(1, 40):
                slices = windows(count, size)
                self.assertEqual(set(i for a, b in slices for i in range(a, b)), set(range(count)))
                self.assertTrue(all(b-a <= size for a, b in slices))
                self.assertTrue(all(a[1]-1 == b[0] for a,b in zip(slices,slices[1:])))
                self.assertEqual(slices[-1][1], count)
        with self.assertRaises(ValueError):
            windows(5, 1)

    def test_invalid_json_is_not_no_contact(self):
        for raw in ("oops", '{"states":[]}', '{"states":["invalid"]}', 'null', '{"states":[null]}'):
            states, error = parse_states(raw, 1)
            self.assertEqual(states, ["uncertain"])
            self.assertTrue(error)
        self.assertEqual(parse_states('```json\n{"states":["contact"]}\n```',1), (["contact"],None))

    def test_overlap_conflict_remains_uncertain(self):
        self.assertEqual(merge_state(None,"contact"),"contact")
        self.assertEqual(merge_state("contact","no_contact"),"uncertain")
        self.assertEqual(merge_state("uncertain","contact"),"uncertain")

    def test_boundary_across_windows_and_episode_edges(self):
        result = screening_result(self.unit(["no_contact"]*6+["contact"]*3+["no_contact"]), [])
        boundaries = [e for e in result["events"] if e["kind"] in ("contact_start","contact_end")]
        self.assertEqual([e["focus"] for e in boundaries], [6,9])
        self.assertEqual(result["candidates"][0]["start"], 6)
        edges = screening_result(self.unit(["contact"]*3), [])
        self.assertEqual(len([e for e in edges["events"] if e["kind"] == "episode_edge"]),2)
        self.assertIsNone(edges["candidates"][0]["start"])
        self.assertIsNone(edges["candidates"][0]["end"])

    def test_curve_disagreement_missing_flat_and_unknown(self):
        curve = {"side":"left","name":"left/load","unit":"N","points":list(enumerate([0,0,1,1,1,0,0]))}
        result = screening_result(self.unit(["no_contact"]*7), [curve])
        self.assertTrue(any(e["kind"].startswith("disagreement") for e in result["events"]))
        self.assertTrue(any(e["kind"].startswith("curve_start") for e in result["events"]))
        curve["points"] = list(enumerate([0]*7))
        result = screening_result(self.unit([None]*7), [curve])
        self.assertTrue(any(e["kind"] == "uncertain" for e in result["events"]))
        self.assertTrue(any("no_curve" in e["kinds"] for e in result["events"]))
        self.assertFalse(result["machine_no_contact"])

    def test_same_boundary_needs_only_one_review_with_all_reasons(self):
        curve = {"side":"left","name":"left/load","unit":"N","points":list(enumerate([0,0,1,1,1,0,0]))}
        result = screening_result(self.unit(["no_contact","no_contact","contact","contact","contact","no_contact","no_contact"]),[curve])
        boundaries = [e for e in result["events"] if e["priority"] == "boundary"]
        self.assertEqual(len(boundaries),2)
        self.assertTrue(all("模型" in e["reason"] and "曲线" in e["reason"] for e in boundaries))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, self.old_batch = service.DB_PATH, service.BATCH
        service.DB_PATH = Path(self.tmp.name) / "state.sqlite3"
        self.ds, self.model = FakeDataset(), FakeModel()
        self.manager = BatchManager(service.db, lambda _:self.ds, self.model)
        service.BATCH = self.manager
        self.options = {"coverage":"native","frames":3,"camera":"third_camera","prompt":"target"}
        self.run = {"id":"test","dataset":"ds","state":"queued","options":self.options,
                    "done_windows":0,"total_windows":None,"done_units":0,"total_units":0,
                    "processing_seconds":0,"window_seconds":[],"failed_windows":0}
        self.manager.put_run(self.run)

    def tearDown(self):
        service.DB_PATH, service.BATCH = self.old_db, self.old_batch
        self.tmp.cleanup()

    def work(self):
        self.manager.active_id = "test"
        self.manager.work("test")
        return self.manager.details("test",0)

    def test_all_episodes_both_sides_and_eta_persistence(self):
        with service.db() as conn:
            conn.execute("INSERT INTO annotations VALUES (?,?,?,?)", ("ds",0,1,'{"intervals":[{"id":"human"}]}'))
        run = self.work()
        self.assertEqual(run["state"], "done")
        self.assertEqual(run["done_units"], 4)
        self.assertEqual(run["done_windows"], 12)
        self.assertEqual(run["eta_seconds"], 0)
        self.assertTrue(run["window_seconds"])
        self.assertEqual(len(self.model.calls),12)
        self.assertEqual(len(self.manager.units("test")),4)
        self.assertTrue(all(all(s is not None for s in u["states"]) for u in self.manager.units("test")))
        with service.db() as conn:
            self.assertEqual(json.loads(conn.execute("SELECT body FROM annotations").fetchone()[0])["intervals"][0]["id"],"human")
        reloaded = BatchManager(service.db,lambda _:self.ds,self.model)
        self.assertEqual(reloaded.details("test")["done_windows"],12)

    def test_stop_resume_does_not_reinfer_completed_windows(self):
        self.model.callback = lambda: self.manager.cancel("test")
        result = self.work()
        self.assertEqual(result["state"],"cancelled")
        self.assertEqual(result["done_windows"],1)
        self.model.callback = None
        # Simulate resumed worker synchronously, preserving the persisted checkpoint.
        run = self.manager.get("test")
        run["state"] = "queued"
        self.manager.put_run(run)
        result = self.work()
        self.assertEqual(result["state"],"done")
        self.assertEqual(len(self.model.calls),12)

    def test_three_failures_stop_and_recover_marks_interrupted(self):
        self.model.bad = True
        run = self.work()
        self.assertEqual(run["state"],"error")
        self.assertEqual(run["failed_windows"],3)
        self.assertFalse(self.manager.busy())
        run["state"] = "running"
        self.manager.put_run(run)
        self.manager.recover()
        self.assertEqual(self.manager.get("test")["state"],"interrupted")

    def test_review_requires_saved_version_and_invalidates_after_edit(self):
        run = self.work()
        event = run["results"][0]["events"][0]
        with self.assertRaises(ValueError):
            self.manager.review("test", event["id"], 0, 1, "checked")
        with service.db() as conn:
            conn.execute("INSERT INTO annotations VALUES (?,?,?,?)", ("ds",0,1,'{"intervals":[]}'))
        self.manager.review("test",event["id"],0,1,"checked")
        self.assertTrue(next(e for e in self.manager.details("test",0)["results"][0]["events"] if e["id"]==event["id"])["reviewed"])
        with service.db() as conn:
            conn.execute("UPDATE annotations SET revision=2 WHERE dataset='ds'")
        self.assertFalse(next(e for e in self.manager.details("test",0)["results"][0]["events"] if e["id"]==event["id"])["reviewed"])

    def test_routes_and_gpu_exclusion(self):
        self.work()
        status, result = asyncio.run(request("GET","/api/screening/test/episodes/0"))
        self.assertEqual(status,200)
        self.assertEqual(len(result["results"]),2)
        with patch.object(service,"dataset",return_value=self.ds):
            self.manager.active_id = "already-running"
            status, _ = asyncio.run(request("POST","/api/model/jobs",{"dataset":"ds","episode":0}))
            self.assertEqual(status,409)
            status, _ = asyncio.run(request("POST","/api/screening",{"dataset":"ds"}))
            self.assertEqual(status,409)
            self.manager.active_id = None
            status, _ = asyncio.run(request("POST","/api/screening",{"dataset":"ds","frames":1}))
            self.assertEqual(status,422)

    def test_data_mode_covers_tail_and_missing_native_is_flagged(self):
        self.ds.samples = lambda ep, side: []
        result = self.work()
        self.assertEqual(result["state"],"done")
        self.assertTrue(all(u["clock"] == "data" for u in self.manager.units("test")))
        self.assertTrue(all(u["warnings"] for u in self.manager.units("test")))

    def test_latest_and_invalid_resume(self):
        self.work()
        self.assertEqual(self.manager.latest("ds")["id"],"test")
        with self.assertRaises(ValueError):
            self.manager.launch("ds",self.options,resume="test")

    def test_rounding_at_episode_start_does_not_flag_whole_episode(self):
        self.ds.samples = lambda ep,side: [{"time":i/20 + 2e-16} for i in range(7)]
        self.work()
        self.assertTrue(all(not u["warnings"] for u in self.manager.units("test")))
        self.assertTrue(all(len(u["times"]) == 7 for u in self.manager.units("test")))

    def test_real_missing_clock_sections_are_preserved_and_flagged(self):
        self.ds.samples = lambda ep,side: [{"time":i/20} for i in range(1,6)]
        self.work()
        for unit in self.manager.units("test"):
            self.assertEqual(unit["times"][0],0)
            self.assertEqual(unit["times"][-1],.3)
            self.assertTrue(unit["warnings"])

    def test_threaded_launch_busy_stop_and_resume(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.manager.work
        def worker(identity):
            try:
                original(identity)
            finally:
                finished.set()
        def block():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker timeout")
        self.manager.work = worker
        self.model.callback = block
        run = self.manager.launch("ds",self.options)
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.manager.busy())
            self.manager.cancel(run["id"])
        finally:
            release.set()
            self.assertTrue(finished.wait(5))
        self.assertEqual(self.manager.get(run["id"])["state"],"cancelled")
        self.assertFalse(self.manager.busy())
        self.model.callback = None
        finished.clear()
        resumed = self.manager.launch("ds",self.options,resume=run["id"])
        self.assertEqual(resumed["id"],run["id"])
        self.assertTrue(finished.wait(5))
        self.assertEqual(self.manager.get(run["id"])["state"],"done")


if __name__ == "__main__":
    unittest.main()

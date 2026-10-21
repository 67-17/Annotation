"""Offline screening checks; fake inference here is NOT GPU accuracy validation."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

import backend.app as service
from backend.batch import BatchManager, curve_first_result, merge_state, parse_states, screening_result, unit_windows, windows
from tests.test_annotation import request


class FakeDataset:
    episodes = {0: {}, 1: {}}

    def summary(self):
        return {"id": "ds", "path": "/fake"}

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

    def test_curve_regions_never_infer_a_transition_across_unseen_gap(self):
        unit = {"episode":0,"side":"left","pipeline":"curve_first","duration":6,
                "times":[0,1,5,6],"states":["no_contact","no_contact","contact","contact"],
                "warnings":[],"curves":[],"regions":[
                    {"id":"a","first":0,"end_index":2,"boundaries":[{"curve":"load","kind":"contact_start","bracket_start":0,"bracket_end":1}]},
                    {"id":"b","first":2,"end_index":4,"boundaries":[{"curve":"load","kind":"contact_end","bracket_start":5,"bracket_end":6}]}]}
        self.assertEqual(unit_windows(unit,6),[(0,2),(2,4)])
        result = curve_first_result(unit)
        self.assertFalse(result["machine_no_contact"])
        self.assertEqual(len(result["events"]),2)
        self.assertTrue(all(not e["model_boundaries"] and e["priority"] == "high" for e in result["events"]))


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

    def test_window_timings_include_reads_and_model_and_survive_resume(self):
        clock = [0.0]
        frame, infer = self.ds.frame, self.model.infer

        def timed_frame(*args):
            clock[0] += 0.25
            current = self.manager.get("test")["current"]
            self.assertEqual(current["stage"], "read")
            return frame(*args)

        def timed_infer(*args):
            self.assertEqual(self.manager.get("test")["current"]["stage"], "model")
            clock[0] += 2
            return infer(*args)

        self.ds.frame, self.model.infer = timed_frame, timed_infer
        self.model.callback = lambda: self.manager.cancel("test")
        with patch("backend.batch.time.monotonic", side_effect=lambda: clock[0]):
            result = self.work()
        timing = result["last_window"]
        self.assertEqual(timing["read_seconds"], 1.5)  # Three scene/tactile pairs.
        self.assertEqual(timing["model_seconds"], 2)
        self.assertEqual(timing["total_seconds"], 3.5)
        self.assertEqual(result["processing_seconds"], 3.5)
        self.assertEqual(result["state"], "cancelled")

    def test_three_failures_stop_and_recover_marks_interrupted(self):
        self.model.bad = True
        run = self.work()
        self.assertEqual(run["state"],"error")
        self.assertEqual(run["failed_windows"],3)
        self.assertEqual(len(self.model.calls), 6)  # One bounded retry per window.
        self.assertTrue(all(s == "uncertain" for s in self.manager.units("test")[0]["states"]))
        self.assertFalse(self.manager.busy())
        run["state"] = "running"
        self.manager.put_run(run)
        self.manager.recover()
        self.assertEqual(self.manager.get("test")["state"],"interrupted")

    def test_wrong_state_count_retries_same_images_without_poisoning_states(self):
        infer = self.model.infer
        prompts, images_seen = [], []

        def first_bad(images, prompt):
            prompts.append(prompt)
            images_seen.append(images)
            if len(prompts) == 1:
                return {"text": '{"states":["contact"]}'}
            return infer(images, prompt)

        self.model.infer = first_bad
        run = self.work()
        self.assertEqual(run["state"], "done")
        self.assertEqual(run["failed_windows"], 0)
        self.assertEqual(run["format_retries"], 1)
        self.assertIs(images_seen[0], images_seen[1])
        self.assertIn("需要 3，返回 1", prompts[1])
        self.assertTrue(all(s == "no_contact" for u in self.manager.units("test") for s in u["states"]))

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

    def configure_curve_first(self):
        self.ds.rows = lambda ep:[{"timestamp":i/20} for i in range(200)]
        self.ds.samples = lambda ep,side:[{"time":i/20} for i in range(200)]
        self.ds.curves = lambda ep:[{"side":side,"name":side+"/load","unit":"N",
                                   "points":[[i/20, int(40 <= i < 60)] for i in range(200)]}
                                  for side in ("left","right")]
        self.options.update(pipeline="curve_first", frames=6)
        self.manager.put_run(self.run)

    def test_curve_first_only_candidate_frames_and_annotations_preserved(self):
        self.configure_curve_first()
        with service.db() as conn:
            conn.execute("INSERT INTO annotations VALUES (?,?,?,?)", ("ds",0,1,'{"intervals":[{"id":"human"}]}'))
        result = self.work()
        self.assertEqual(result["state"],"done")
        self.assertLess(result["total_windows"], result["full_scan_windows"] / 2)
        self.assertEqual(result["candidate_regions"],8)
        self.assertEqual(result["done_windows"],len(self.model.calls))
        for unit in self.manager.units("test"):
            self.assertFalse(unit["result"]["machine_no_contact"])
            self.assertEqual(unit["state"],"done")
            for first,end in unit_windows(unit,6):
                times=unit["times"][first:end]
                self.assertTrue(all(b-a < .051 for a,b in zip(times,times[1:])))
                self.assertTrue(all(1.5 < t < 3.5 for t in times))
            self.assertEqual(len(unit["result"]["events"]),2)
        with service.db() as conn:
            self.assertIn('"human"',conn.execute("SELECT body FROM annotations").fetchone()[0])

    def test_curve_first_stop_resume_keeps_completed_windows(self):
        self.configure_curve_first()
        self.model.callback=lambda:self.manager.cancel("test")
        result=self.work()
        self.assertEqual(result["state"],"cancelled")
        self.assertEqual(result["done_windows"],1)
        self.model.callback=None
        run=self.manager.get("test")
        run["state"]="queued"
        self.manager.put_run(run)
        result=self.work()
        self.assertEqual(result["state"],"done")
        self.assertEqual(len(self.model.calls),result["total_windows"])

    def test_curve_first_flat_curves_flagged_without_gpu_or_no_contact_claim(self):
        self.configure_curve_first()
        self.ds.curves=lambda ep:[]
        result=self.work()
        self.assertEqual(result["state"],"done")
        self.assertEqual(result["total_windows"],0)
        self.assertEqual(len(self.model.calls),0)
        for unit in self.manager.units("test"):
            self.assertFalse(unit["result"]["machine_no_contact"])
            self.assertEqual(unit["result"]["events"][0]["end"],9.95)

    def test_curve_first_current_episode_selection_and_api_default(self):
        self.configure_curve_first()
        self.options["episodes"]=[1]
        self.manager.put_run(self.run)
        result=self.work()
        self.assertEqual(result["total_units"],2)
        self.assertTrue(all(u["episode"] == 1 for u in self.manager.units("test")))
        with patch.object(service,"dataset",return_value=self.ds), patch.object(self.manager,"launch",return_value={"id":"new"}) as launch:
            status,_=asyncio.run(request("POST","/api/screening",{"dataset":"ds","episodes":[0]}))
            self.assertEqual(status,202)
            self.assertEqual(launch.call_args.args[1]["pipeline"],"curve_first")
            self.assertEqual(launch.call_args.args[1]["contact_mode"],"single")
            status,_=asyncio.run(request("POST","/api/screening",{
                "dataset":"ds","episodes":[0],"contact_mode":"multiple"}))
            self.assertEqual(status,202)
            self.assertEqual(launch.call_args.args[1]["contact_mode"],"multiple")
            status,_=asyncio.run(request("POST","/api/screening",{"dataset":"ds","episodes":[99]}))
            self.assertEqual(status,400)


if __name__ == "__main__":
    unittest.main()

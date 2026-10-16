"""Offline curve-first plan and API orchestration checks; no GPU inference."""
import json
from pathlib import Path
import tempfile
import unittest

from curve_first import Client, curve_boundaries, merge_regions, model_states, plan_dataset, run_model, save_report
from test_batch import FakeDataset


class Curves(unittest.TestCase):
    def curve(self, values, times=None):
        return {"name":"left/load", "side":"left", "unit":"N",
                "points":[[t,v] for t,v in zip(times or list(range(len(values))),values)]}

    def test_brackets_cover_both_rising_and_falling_ramps(self):
        candidates, thresholds = curve_boundaries(self.curve([0,0,.01,.04,.1,1,1,.1,.04,.01,0,0]),padding=.5)
        start = next(c for c in candidates if c["kind"] == "contact_start")
        end = next(c for c in candidates if c["kind"] == "contact_end")
        self.assertLessEqual(start["bracket_start"],3)
        self.assertGreaterEqual(start["bracket_end"],4)
        self.assertLessEqual(end["bracket_start"],7)
        self.assertGreaterEqual(end["bracket_end"],9)
        self.assertLess(thresholds["release_threshold"],thresholds["threshold"])
        self.assertLess(start["start"],start["bracket_start"])

    def test_multiple_contacts_and_open_episode_end(self):
        candidates,_ = curve_boundaries(self.curve([0,0,1,1,0,0,1,1]))
        self.assertEqual([c["kind"] for c in candidates],["contact_start","contact_end","contact_start","episode_end"])
        self.assertTrue(candidates[-1]["ends_after_possible"])

    def test_initial_contact_and_one_frame_pulse_not_dropped(self):
        candidates,_ = curve_boundaries(self.curve([1,1,0,0,0,0]))
        self.assertTrue(candidates[0]["starts_before_possible"])
        candidates,_ = curve_boundaries(self.curve([0,0,1,0,0,0]))
        self.assertTrue(any(c["kind"] == "short_excursion" for c in candidates))

    def test_flat_invalid_and_nonmonotonic_not_called_no_contact(self):
        for curve in [self.curve([0]*10),self.curve([1]*10),self.curve([0,1,float('nan')]),self.curve([0,1,0],[0,0,1])]:
            with self.assertRaises(ValueError):
                curve_boundaries(curve)

    def test_union_regions_preserves_curve_evidence(self):
        candidates = [{"start":0,"end":1,"curve":"left/load","kind":"contact_start"},
                      {"start":.5,"end":2,"curve":"left/motion","kind":"contact_start"},
                      {"start":5,"end":6,"curve":"left/load","kind":"contact_end"}]
        result = merge_regions(candidates)
        self.assertEqual([(r['start'],r['end']) for r in result],[(0,2),(5,6)])
        self.assertEqual(len(result[0]["boundaries"]),2)


class FakeClient:
    def __init__(self, report):
        self.report, self.calls, self.times, self.states = report, [], [], []
        self.active = None
        self.fail = False

    def call(self,path,body=None):
        self.calls.append(path)
        if path == "api/datasets":
            return self.report["dataset"]
        if path.endswith("/screening"):
            return self.active
        raise AssertionError("unexpected API call: " + path)

    def infer(self,report,unit,region,task,checkpoint,prompt,camera):
        if self.fail:
            raise RuntimeError("fake transport failure")
        self.times.extend(task["times"])
        return {"sample_times":task["times"], "text":json.dumps({"observations":[
            {"time":t,"state":"contact" if .1 <= t < .25 else "no_contact"} for t in task["times"]]})}


class Planner(unittest.TestCase):
    def plan(self):
        ds = FakeDataset()
        ds.summary = lambda:{"id":"fake","path":"/fake","episodes":[{"id":0},{"id":1}]}
        return plan_dataset(ds,padding=.01)

    def test_only_merged_candidate_frames_with_no_gaps(self):
        report = self.plan()
        self.assertEqual(len(report["units"]),4)
        for unit in report["units"]:
            for region in unit["regions"]:
                covered = {t for task in region["tasks"] for t in task["times"]}
                self.assertEqual(covered,set(region["times"]))
                self.assertTrue(all(len(task["times"]) <= 6 for task in region["tasks"]))
        self.assertEqual(report["total_windows"],sum(len(r["tasks"]) for u in report["units"] for r in u["regions"]))
        self.assertTrue(all("in_contact" not in c["name"] for u in report["units"] for c in u["curves"]))

    def test_flat_missing_and_broken_curves_require_manual_review(self):
        ds = FakeDataset()
        ds.summary = lambda:{"id":"fake","path":"/fake"}
        ds.curves = lambda ep:[]
        report = plan_dataset(ds)
        self.assertEqual(report["total_windows"],0)
        self.assertTrue(all(u["warnings"] for u in report["units"]))

    def test_model_and_curve_candidates_remain_unconfirmed_and_resume_skips(self):
        report = self.plan()
        client = FakeClient(report)
        snapshots=[]
        run_model(report,client,lambda:snapshots.append(report["done_windows"]),progress=lambda _:None)
        self.assertEqual(report["state"],"done")
        self.assertEqual(report["done_windows"],report["total_windows"])
        self.assertTrue(all(r["manual_review"] and not r["model_review"]["confirmed"] for u in report["units"] for r in u["regions"]))
        calls=len(client.times)
        run_model(report,client,lambda:None,progress=lambda _:None)
        self.assertEqual(len(client.times),calls)
        self.assertTrue(all("annotations" not in path for path in client.calls))

    def test_does_not_compete_with_current_full_frame_run(self):
        report=self.plan()
        client=FakeClient(report)
        client.active={"state":"running"}
        with self.assertRaisesRegex(RuntimeError,"仍在运行"):
            run_model(report,client,lambda:None)
        self.assertEqual(client.times,[])

    def test_failed_transport_does_not_advance_and_atomic_report_loads(self):
        report=self.plan()
        client=FakeClient(report)
        client.fail=True
        with self.assertRaises(RuntimeError):
            run_model(report,client,lambda:None)
        self.assertEqual(report["done_windows"],0)
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/"report.json"
            save_report(report,output)
            self.assertEqual(json.loads(output.read_text())["done_windows"],0)
            self.assertEqual(len(list(Path(folder).iterdir())),1)

    def test_prompt_change_on_resume_is_rejected(self):
        report=self.plan()
        client=FakeClient(report)
        run_model(report,client,lambda:None,prompt="plug only",progress=lambda _:None)
        with self.assertRaisesRegex(ValueError,"原来的"):
            run_model(report,client,lambda:None,prompt="table")


class Output(unittest.TestCase):
    def test_invalid_missing_and_conflicting_observations_are_uncertain(self):
        for text in ['bad', 'null', '{"observations":[]}', '{"observations":[{"time":0,"state":"bad"}]}']:
            states,error=model_states({"sample_times":[0],"text":text})
            self.assertEqual(states,["uncertain"])
            self.assertTrue(error)
        text=json.dumps({"observations":[{"time":0,"state":"contact"},{"time":0,"state":"no_contact"}]})
        self.assertEqual(model_states({"sample_times":[0],"text":text})[0],["uncertain"])


class JobRecovery(unittest.TestCase):
    def test_poll_failure_preserves_id_and_resume_reuses_it(self):
        client = Client("http://unused")
        task = {"times": [0, .02], "state": "pending"}
        calls, saved = [], []

        def call(path, body=None):
            calls.append((path, body))
            if body is not None:
                return {"id": "saved-job"}
            raise ConnectionError("interrupted")

        client.call = call
        with self.assertRaises(ConnectionError):
            client.infer({"dataset":{"id":"fake"}}, {"episode":0,"side":"left"}, {},
                         task, lambda:saved.append(dict(task)), "target", "scene")
        self.assertEqual(saved[0]["job_id"], "saved-job")
        self.assertEqual(calls[0][1]["frames"], 2)
        client.call = lambda path, body=None: {"state":"done", "result":{"text":"ok"}}
        self.assertEqual(client.infer({}, {}, {}, task, lambda:None, "target", "scene"), {"text":"ok"})
        self.assertEqual(task["job_id"], "saved-job")

    def test_model_error_keeps_window_pending_for_retry(self):
        client = Client("http://unused")
        task = {"times":[0], "state":"running", "job_id":"failed-job"}
        client.call = lambda path, body=None: {"state":"error", "error":"GPU error"}
        with self.assertRaisesRegex(RuntimeError, "GPU error"):
            client.infer({}, {}, {}, task, lambda:None, "", "scene")
        self.assertNotIn("job_id", task)
        self.assertEqual(task["state"], "pending")


if __name__ == "__main__":
    unittest.main()

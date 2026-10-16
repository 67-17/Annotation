"""Focused checks for frame/time alignment and annotation integrity.

Run: python -m unittest -v tests.test_annotation
Optional real-data checks: REAL_DATASET=/path/to/xarm7 python -m unittest -v
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import backend.app as service
from backend.dataset import Dataset
from backend.review import refine


async def request(method, path, body=None):
    """Exercise the real ASGI routes without a network socket or extra clients."""
    events = []
    payload = json.dumps(body).encode() if body is not None else b""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        events.append(message)

    parsed = urlsplit(path)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": parsed.path,
             "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(), "root_path": "",
             "headers": [(b"content-type", b"application/json")],
             "server": ("test", 80), "client": ("test", 1234)}
    # The development sandbox restricts socketpair wakeups from worker
    # threads. A timer keeps this in-process test loop responsive there.
    async def heartbeat():
        while True:
            await asyncio.sleep(.01)

    timer = asyncio.create_task(heartbeat())
    try:
        await asyncio.wait_for(service.app(scope, receive, send), timeout=60)
    finally:
        timer.cancel()
    status = next(e["status"] for e in events if e["type"] == "http.response.start")
    raw = b"".join(e.get("body", b"") for e in events if e["type"] == "http.response.body")
    try:
        return status, json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, raw


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_allowed = os.environ.get("DATASET_ALLOWED_ROOTS")
        os.environ["DATASET_ALLOWED_ROOTS"] = str(self.root)
        self.old_db = service.DB_PATH
        service.DB_PATH = self.root / "annotations.sqlite3"
        self.data = self.root / "dataset"
        (self.data / "meta").mkdir(parents=True)
        (self.data / "data").mkdir()
        info = {"codebase_version": "v2.1", "fps": 15, "chunks_size": 1000,
                "data_path": "data/episode_{episode_index:06d}.parquet",
                "video_path": "videos/{video_key}/{episode_index:06d}.mp4",
                "features": {"observation.images.photon_left": {
                    "dtype": "video", "video_info": {"video.fps": 60, "video.codec": "h264"}}}}
        (self.data / "meta/info.json").write_text(json.dumps(info))
        (self.data / "meta/episodes.jsonl").write_text(json.dumps({"episode_index": 0, "length": 3, "tasks": []}) + "\n")
        pq.write_table(pa.Table.from_pylist([
            {"episode_index": 0, "frame_index": i, "timestamp": i / 15,
             "observation.photon_left.tactile_range": [max(0, i * 4 - 3), i * 4 + 1]}
            for i in range(3)]), self.data / "data/episode_000000.parquet")
        folder = self.data / "tactile_streams/photon_left/episode_000000"
        folder.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([
            {"tactile_index": i, "capture_monotonic_s": 1 + i / 60,
             "video_frame_index": i, "video_timestamp_s": i / 60}
            for i in range(9)]), folder / "samples.parquet")
        (folder / "video.json").write_text(json.dumps({"from_timestamp": 10.0}))
        status, self.summary = self.call("POST", "/api/datasets", {"path": str(self.data)})
        self.assertEqual(status, 200)
        self.base = f"/api/datasets/{self.summary['id']}/episodes/0"

    def tearDown(self):
        service.DATASETS.clear()
        service.DB_PATH = self.old_db
        if self.old_allowed is None:
            os.environ.pop("DATASET_ALLOWED_ROOTS", None)
        else:
            os.environ["DATASET_ALLOWED_ROOTS"] = self.old_allowed
        Dataset.rows.cache_clear()
        Dataset.samples.cache_clear()
        Dataset.tactile_video.cache_clear()
        Dataset.curves.cache_clear()
        Dataset.frame.cache_clear()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        return asyncio.run(request(method, path, body))

    def interval(self, **changes):
        return {"id": "one", "side": "left", "clock": "left", "start": 1 / 60,
                "end": 6 / 60, "confirmed": True, **changes}

    def save(self, intervals, revision=0, no_contact=None):
        return self.call("PUT", self.base + "/annotations", {
            "revision": revision, "intervals": intervals, "no_contact": no_contact or []})

    def test_native_anchor_retains_subframe_precision(self):
        status, result = self.save([self.interval()])
        self.assertEqual(status, 200)
        anchor = result["intervals"][0]["start_anchor"]
        self.assertAlmostEqual(anchor["time"], 1 / 60)
        self.assertEqual(anchor["sample_index"], 1)
        self.assertEqual(anchor["frame_index"], 0)
        self.assertEqual(self.call("GET", self.base + "/annotations")[1], result)
        exported = self.call("GET", f"/api/datasets/{self.summary['id']}/export")[1]
        self.assertEqual(exported["episodes"][0]["left"][0]["clock"], "left")
        self.assertAlmostEqual(exported["episodes"][0]["left"][0]["contact_time_s"], 1 / 60)
        self.assertAlmostEqual(exported["episodes"][0]["left"][0]["release_time_s"], 6 / 60)
        self.assertEqual(exported["episodes"][0]["left"][0]["contact_frame"], 1)
        self.assertEqual(exported["episodes"][0]["left"][0]["release_frame"], 6)
        self.assertEqual(exported["episodes"][0]["right"], [])

    def test_export_omits_drafts_and_screening_details(self):
        self.save([self.interval(confirmed=False)])
        service.dataset(self.summary["id"]).episodes[1] = {"length": 0}
        exported = self.call("GET", f"/api/datasets/{self.summary['id']}/export")[1]
        self.assertEqual(exported, {"episodes": [
            {"episode": 0, "left": [], "right": []},
            {"episode": 1, "left": [], "right": []},
        ]})

    def test_episode_can_load_frames_before_curves(self):
        with patch.object(Dataset, "curves", side_effect=RuntimeError("slow curves")):
            status, episode = self.call("GET", self.base + "?include_curves=false")
            self.assertEqual(status, 200)
            self.assertEqual(episode["curves"], [])
            self.assertTrue(episode["clocks"]["data"])

    def test_save_reuses_episode_clocks_without_rereading_parquet(self):
        ds = service.dataset(self.summary["id"])
        ds.episode(0, include_curves=False)
        ds.rows.cache_clear()
        ds.samples.cache_clear()
        with patch.object(Dataset, "rows", side_effect=RuntimeError("Parquet read")), \
             patch.object(Dataset, "samples", side_effect=RuntimeError("sample read")):
            status, saved = self.save([self.interval()])
        self.assertEqual(status, 200)
        self.assertEqual(saved["intervals"][0]["start_anchor"]["sample_index"], 1)

    def test_stale_save_does_not_overwrite(self):
        self.assertEqual(self.save([self.interval()])[0], 200)
        self.assertEqual(self.save([], revision=0)[0], 409)
        self.assertEqual(len(self.call("GET", self.base + "/annotations")[1]["intervals"]), 1)

    def test_invalid_confirmed_intervals_rejected(self):
        for change in ({"end": None}, {"end": 1 / 60}, {"clock": "right"},
                       {"start": -1}, {"starts_before": True}):
            with self.subTest(change=change):
                self.assertEqual(self.save([self.interval(**change)])[0], 400)

    def test_overlapping_confirmed_intervals_rejected(self):
        status, _ = self.save([self.interval(), self.interval(id="two", start=2 / 60)])
        self.assertEqual(status, 400)

    def test_no_contact_conflict_rejected(self):
        self.assertEqual(self.save([self.interval()], no_contact=["left"])[0], 400)
        self.assertEqual(self.save([], no_contact=["left", "right"])[0], 200)

    def test_censored_episode_boundaries(self):
        status, result = self.save([self.interval(start=None, end=None, starts_before=True, ends_after=True)])
        self.assertEqual(status, 200)
        self.assertIsNone(result["intervals"][0]["start_anchor"])

    def test_pages_and_episode_api(self):
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/static/app.js")[0], 200)
        status, result = self.call("GET", self.base)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["clocks"]["left"]), 9)
        self.assertAlmostEqual(result["clocks"]["left"][0]["time"], 0)

    def test_tactile_local_frame_adds_video_offset(self):
        from unittest.mock import patch
        ds = service.dataset(self.summary["id"])
        with patch("backend.dataset.cv2.VideoCapture") as capture:
            capture.return_value.isOpened.return_value = True
            capture.return_value.read.return_value = (True, np.zeros((2, 2, 3), np.uint8))
            _, number = ds.frame(0, "observation.images.photon_left", 1 / 60)
        self.assertEqual(number, 601)

    def test_preview_jpeg_reuses_video_frame_and_route_returns_bytes(self):
        from unittest.mock import patch
        ds = service.dataset(self.summary["id"])
        with patch.object(ds, "_frame_reference", return_value=("test-video", 12, 60.0, "av1")), \
             patch.object(ds, "_ffmpeg_bytes", return_value=b"jpeg-bytes") as decode:
            first = ds.frame_jpeg(0, "observation.images.photon_left", 0.2)
            second = ds.frame_jpeg(0, "observation.images.photon_left", 0.2001)
            response = service.get_frame(self.summary["id"], 0, "observation.images.photon_left", 0.2)
        self.assertEqual(first, (b"jpeg-bytes", 12))
        self.assertEqual(second, first)
        self.assertEqual(response.body, b"jpeg-bytes")
        self.assertEqual(response.headers["x-video-frame"], "12")
        decode.assert_called_once()


class CurveTests(unittest.TestCase):
    def test_hysteresis_backtracks_to_first_changed_sample(self):
        values = [0, 0.9, 0, 0.9, 0.8, 0.4, 0.1, 0.05, 0.9, 1]
        result = refine(list(enumerate(values)), "left", 0, 9, .7, .2)
        self.assertEqual(result["intervals"], [
            {"side": "left", "start": 3.0, "end": 6.0},
            {"side": "left", "start": 8.0, "end": None}])

    def test_bad_thresholds_rejected(self):
        with self.assertRaises(ValueError):
            refine([[0, 0], [1, 1]], "left", 0, 1, .1, .2)


@unittest.skipUnless(os.environ.get("REAL_DATASET"), "set REAL_DATASET for integration checks")
class RealDataTests(unittest.TestCase):
    def test_first_second_last_episode_images_and_native_anchors(self):
        ds = Dataset(os.environ["REAL_DATASET"])
        ids = sorted(ds.episodes)
        for ep in sorted(set([ids[0], ids[min(1, len(ids) - 1)], ids[-1]])):
            episode = ds.episode(ep)
            for camera in episode["cameras"]:
                for time in (0.0, episode["duration"]):
                    pixels, frame = ds.frame(ep, camera["key"], time)
                    self.assertEqual(pixels.ndim, 3)
                    self.assertGreater(pixels.size, 0)
                    self.assertGreaterEqual(frame, round(camera["offset"] * camera["fps"]))
                    self.assertLess(frame, round(camera["end"] * camera["fps"]))
            for clock, points in episode["clocks"].items():
                sample = points[min(5, len(points) - 1)]
                anchor = ds.snap(ep, sample["time"], clock)
                self.assertEqual(anchor["sample_index"], sample["index"])
                self.assertAlmostEqual(anchor["time"], sample["time"])


if __name__ == "__main__":
    unittest.main()

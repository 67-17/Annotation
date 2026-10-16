"""Read LeRobot files directly. No dependency on, or writes to, Cleaner."""
import hashlib
import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq


def read_json(path):
    return json.loads(Path(path).read_text())


def ffmpeg_binary():
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def safe_child(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("数据文件路径超出数据集目录")
    return path


class Dataset:
    def __init__(self, path):
        self.root = Path(path).expanduser().resolve()
        allowed = os.environ.get(
            "DATASET_ALLOWED_ROOTS",
            "/home/liushiqi/datasets:/home/liushiqi/dataset_exports:/run/determined/NAS1/public",
        ).split(":")
        if not any(self.root.is_relative_to(Path(p).resolve()) for p in allowed if p):
            raise ValueError("数据集目录不在 DATASET_ALLOWED_ROOTS 内")
        self.info = read_json(self.root / "meta/info.json")
        self.id = hashlib.sha256(
            (str(self.root) + json.dumps(self.info, sort_keys=True)).encode()
        ).hexdigest()[:24]
        self.episodes = {}
        if str(self.info.get("codebase_version", "")).startswith("v3"):
            for path in sorted((self.root / "meta/episodes").rglob("*.parquet")):
                names = pq.ParquetFile(path).schema_arrow.names
                columns = [n for n in names if not n.startswith("stats/")]
                for row in pq.read_table(path, columns=columns).to_pylist():
                    self.episodes[int(row["episode_index"])] = row
        else:
            for line in (self.root / "meta/episodes.jsonl").read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.episodes[int(row["episode_index"])] = row
        if not self.episodes:
            raise ValueError("没有找到 Episode 元数据")
        self.tactile_meta = (
            read_json(self.root / "meta/tactile.json")
            if (self.root / "meta/tactile.json").exists() else {}
        )

    def summary(self):
        return {
            "id": self.id, "name": self.root.name, "path": str(self.root),
            "fps": self.info["fps"], "episodes": [
                {"id": k, "length": v.get("length"), "tasks": v.get("tasks", [])}
                for k, v in sorted(self.episodes.items())
            ], "tactile_metadata": self.tactile_meta,
        }

    def episode_meta(self, ep):
        if ep not in self.episodes:
            raise ValueError("Episode 不存在")
        return self.episodes[ep]

    @lru_cache(maxsize=4)
    def rows(self, ep):
        meta = self.episode_meta(ep)
        variables = {
            "episode_index": ep,
            "episode_chunk": ep // int(self.info.get("chunks_size", 1000)),
            "chunk_index": meta.get("data/chunk_index", 0),
            "file_index": meta.get("data/file_index", 0),
        }
        path = safe_child(self.root, self.info["data_path"].format(**variables))
        available = pq.ParquetFile(path).schema_arrow.names
        columns = [k for k in available if k in {
            "timestamp", "episode_index", "frame_index", "index", "observation.state", "action",
            "observation.photon_left.tactile_range", "observation.photon_right.tactile_range",
        }]
        rows = pq.read_table(path, columns=columns, filters=[("episode_index", "=", ep)]).to_pylist()
        rows.sort(key=lambda x: int(x["frame_index"]))
        if not rows or len(rows) != int(meta["length"]):
            raise ValueError("Episode 行数与元数据不一致")
        if [int(r["frame_index"]) for r in rows] != list(range(len(rows))):
            raise ValueError("Episode 帧序号不连续，不能可靠逐帧标注")
        ts = [float(r["timestamp"]) for r in rows]
        if any(not np.isfinite(t) for t in ts) or any(b <= a for a, b in zip(ts, ts[1:])):
            raise ValueError("原始时间戳无效或非严格递增")
        return rows

    def cameras(self, ep):
        meta = self.episode_meta(ep)
        result = []
        for key, feature in self.info["features"].items():
            if feature.get("dtype") != "video":
                continue
            base = "videos/" + key
            variables = {
                "video_key": key, "episode_index": ep,
                "episode_chunk": ep // int(self.info.get("chunks_size", 1000)),
                "chunk_index": meta.get(base + "/chunk_index", 0),
                "file_index": meta.get(base + "/file_index", 0),
            }
            path = safe_child(self.root, self.info["video_path"].format(**variables))
            vi = feature.get("info", feature.get("video_info", {}))
            result.append({
                "key": key, "name": key.removeprefix("observation.images."),
                "relative_path": str(path.relative_to(self.root)),
                "offset": float(meta.get(base + "/from_timestamp", 0)),
                "end": float(meta.get(base + "/to_timestamp", len(self.rows(ep)) / self.info["fps"])),
                "fps": float(vi.get("video.fps", self.info["fps"])),
            })
        return result

    def camera(self, ep, key):
        for camera in self.cameras(ep):
            if camera["key"] == key:
                return camera
        raise ValueError("相机不存在")

    @lru_cache(maxsize=8)
    def tactile_video(self, ep, side):
        path = self.root / f"tactile_streams/photon_{side}/episode_{ep:06d}/video.json"
        return read_json(path) if path.exists() else {}

    @lru_cache(maxsize=8)
    def samples(self, ep, side):
        if side not in ("left", "right"):
            raise ValueError("无效的触觉通道")
        path = self.root / f"tactile_streams/photon_{side}/episode_{ep:06d}/samples.parquet"
        if not path.exists():
            return []
        rows = pq.read_table(path).to_pylist()
        # Derive the common clock from explicit dataset range references.
        # Capture/simulation clocks may have a non-zero origin.
        anchors = []
        key = f"observation.photon_{side}.tactile_range"
        for r in self.rows(ep):
            bounds = r.get(key)
            if bounds and bounds[1] > bounds[0] and 0 < bounds[1] <= len(rows):
                sample = rows[int(bounds[1]) - 1]
                if sample.get("capture_monotonic_s") is not None:
                    anchors.append(float(sample["capture_monotonic_s"]) - float(r["timestamp"]))
        offset = float(np.median(anchors)) if anchors else None
        camera = next((c for c in self.cameras(ep) if c["name"] == f"photon_{side}"), None)
        for row in rows:
            if offset is not None and row.get("capture_monotonic_s") is not None:
                row["time"] = float(row["capture_monotonic_s"]) - offset
            elif camera and row.get("video_timestamp_s") is not None and self.tactile_video(ep, side):
                # These exports store episode-local video_timestamp_s, even
                # when several episodes share a single video file.
                row["time"] = float(row["video_timestamp_s"])
            else:
                raise ValueError("触觉采样缺少可映射的时间信息")
        if any(b["time"] <= a["time"] for a, b in zip(rows, rows[1:])):
            raise ValueError("触觉采样时间非严格递增")
        return rows

    @lru_cache(maxsize=64)
    def frame(self, ep, key, time):
        camera = self.camera(ep, key)
        path = str(safe_child(self.root, camera["relative_path"]))
        side = camera["name"].removeprefix("photon_")
        samples = self.samples(ep, side) if side in ("left", "right") else []
        if samples:
            sample = min(samples, key=lambda x: abs(x["time"] - time))
            video_meta = self.tactile_video(ep, side)
            path = str(safe_child(self.root, sample.get("video_path") or camera["relative_path"]))
            offset = float(video_meta.get("from_timestamp", camera["offset"]))
            number = round(offset * camera["fps"]) + int(sample["video_frame_index"])
        else:
            number = max(0, round((camera["offset"] + time) * camera["fps"]))
            # A native tactile clock can extend beyond the last scene frame.
            # Never read the next episode from a shared video file.
            first = round(camera["offset"] * camera["fps"])
            last = max(first, round(camera["end"] * camera["fps"]) - 1)
            number = min(last, max(first, number))
        feature = self.info["features"][key]
        codec = feature.get("info", feature.get("video_info", {})).get("video.codec", "")
        # OpenCV wheels may ship an AV1 decoder that requires unavailable
        # hardware. System ffmpeg's libdav1d provides reliable CPU decoding.
        if "av1" in codec and ffmpeg_binary():
            return self._ffmpeg_frame(path, number, camera["fps"]), number
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise ValueError("视频无法解码")
            cap.set(cv2.CAP_PROP_POS_FRAMES, number)
            ok, bgr = cap.read()
            if not ok:
                return self._ffmpeg_frame(path, number, camera["fps"]), number
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), number
        finally:
            cap.release()

    @staticmethod
    def _ffmpeg_frame(path, number, fps):
        binary = ffmpeg_binary()
        if not binary:
            raise ValueError("该视频需要 ffmpeg 解码；请安装 ffmpeg")
        result = subprocess.run(
            # Seek just before the target PTS to avoid microsecond rounding
            # skipping the intended frame in constant-frame-rate video.
            [binary, "-v", "error", "-threads", "1", "-ss", f"{max(0, (number - .25) / fps):.9f}",
             "-i", path, "-frames:v", "1", "-threads", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            capture_output=True, timeout=60,
        )
        if result.returncode or not result.stdout:
            raise ValueError("ffmpeg 解码失败：" + result.stderr.decode(errors="replace")[-500:])
        frame = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("解码帧为空")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    @lru_cache(maxsize=4)
    def curves(self, ep):
        result = []
        rows = self.rows(ep)
        for key in ("observation.state", "action"):
            feature = self.info["features"].get(key, {})
            names = feature.get("names") or []
            if isinstance(names, dict):
                names = next(iter(names.values()), [])
            for i, name in enumerate(names):
                if "gripper" in name.lower():
                    result.append({"name": f"{key}/{name}", "unit": "原始单位", "side": None,
                                   "points": [[float(r["timestamp"]), float(r[key][i])] for r in rows]})
        for side in ("left", "right"):
            samples = self.samples(ep, side)
            folder = self.root / f"tactile_streams/photon_{side}/episode_{ep:06d}"
            if samples and (folder / "mechanics.npz").exists():
                with np.load(folder / "mechanics.npz", allow_pickle=False) as mechanics:
                    for key, unit in (("normal_load_N", "N"), ("in_contact", "布尔值 / 仿真参考")):
                        if key in mechanics:
                            values = mechanics[key]
                            result.append({"name": f"{side}/{key}", "unit": unit, "side": side,
                                           "points": [[float(s["time"]), float(values[int(s.get("mechanics_index", j))])]
                                                      for j, s in enumerate(samples)]})
            mesh_path = folder / "mesh3dflow.npy"
            if samples and mesh_path.exists():
                mesh = np.load(mesh_path, mmap_mode="r", allow_pickle=False)
                norms = np.linalg.norm(mesh, axis=-1)
                values = np.quantile(norms.reshape(len(mesh), -1), .9, axis=1)
                mesh_meta = read_json(folder / "mesh3dflow.json") if (folder / "mesh3dflow.json").exists() else {}
                result.append({"name": f"{side}/形变幅度P90", "unit": mesh_meta.get("unit", "原始单位"), "side": side,
                               "points": [[float(s["time"]), float(values[int(s.get("mesh_frame_index", j))])]
                                          for j, s in enumerate(samples)]})
            elif f"observation.photon_{side}.mesh_motion_3d" in self.info["features"]:
                # Older exports keep the tactile mesh directly in main parquet.
                meta = self.episode_meta(ep)
                path = safe_child(self.root, self.info["data_path"].format(
                    episode_index=ep, episode_chunk=ep // int(self.info.get("chunks_size", 1000)),
                    chunk_index=meta.get("data/chunk_index", 0), file_index=meta.get("data/file_index", 0)))
                key = f"observation.photon_{side}.mesh_motion_3d"
                table = pq.read_table(path, columns=["timestamp", "frame_index", key],
                                      filters=[("episode_index", "=", ep)]).sort_by("frame_index")
                shape = self.info["features"][key]["shape"]
                mesh = np.asarray(table[key].to_pylist(), dtype=np.float32).reshape((len(table), *shape))
                values = np.quantile(np.linalg.norm(mesh, axis=-1).reshape(len(mesh), -1), .9, axis=1)
                result.append({"name": f"{side}/形变幅度P90", "unit": "原始单位（未标定）", "side": side,
                               "points": [[float(t), float(v)] for t, v in zip(table["timestamp"].to_pylist(), values)]})
        return result

    def episode(self, ep):
        rows = self.rows(ep)
        clocks = {"data": [{"index": int(r["frame_index"]), "time": float(r["timestamp"])} for r in rows]}
        for side in ("left", "right"):
            samples = self.samples(ep, side)
            if samples:
                clocks[side] = [{"index": int(r["tactile_index"]), "time": float(r["time"])} for r in samples]
        cameras = self.cameras(ep)
        return {"id": ep, "frames": clocks["data"], "clocks": clocks, "cameras": cameras,
                "duration": max(float(rows[-1]["timestamp"]), max(
                    (v[-1]["time"] for v in clocks.values()), default=0)),
                "tasks": self.episode_meta(ep).get("tasks", []), "curves": self.curves(ep)}

    def snap(self, ep, time, clock="data"):
        if not np.isfinite(time):
            raise ValueError("标注时间必须为有限数值")
        if clock == "data":
            points = [{"time": float(r["timestamp"]), "index": int(r["frame_index"])} for r in self.rows(ep)]
        elif clock in ("left", "right"):
            points = [{"time": r["time"], "index": int(r["tactile_index"])} for r in self.samples(ep, clock)]
        else:
            raise ValueError("无效的标注时钟")
        if not points or time < points[0]["time"] - .02 or time > points[-1]["time"] + .02:
            raise ValueError("标注时间超出采样范围")
        p = min(points, key=lambda p: abs(p["time"] - time))
        row = min(self.rows(ep), key=lambda r: abs(float(r["timestamp"]) - p["time"]))
        return {"time": p["time"], "clock": clock, "sample_index": p["index"],
                "frame_index": int(row["frame_index"]), "dataset_timestamp": float(row["timestamp"])}

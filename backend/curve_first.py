"""先用曲线圈定接触/离开候选段，再通过现有网页 API 检查这些段的连续帧。"""
import argparse
from bisect import bisect_left, bisect_right
import hashlib
import json
import math
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np

from .batch import merge_state, windows
from .dataset import Dataset

NOTICE = "仅筛曲线候选段，未查看的帧不代表无接触；曲线未标定，最终起止帧仍需人工确认。"


def curve_boundaries(curve, padding=.35, on_ratio=.1, off_ratio=.02):
    """Bracket a change from the lower crossing to the confirmed upper crossing.

    A low contact threshold targets onset, rather than the peak grip force.
    Neither relative thresholds nor padding guarantee physical first contact.
    """
    points = np.asarray(curve["points"], dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError("有效曲线采样不足或含无效数值")
    times, raw = points[:, 0], points[:, 1]
    if np.any(np.diff(times) <= 0):
        raise ValueError("曲线时钟必须严格递增")
    if not math.isfinite(padding) or padding < 0 or not 0 < off_ratio < on_ratio < 1:
        raise ValueError("需要 padding >= 0 且 0 < off_ratio < on_ratio < 1")
    # Median of three removes single-sample noise; short raw excursions are
    # explicitly added below so that smoothing never silently discards them.
    padded = np.pad(raw, (1, 1), mode="edge")
    values = np.median(np.stack([padded[:-2], padded[1:-1], padded[2:]]), axis=0)
    base = float(np.quantile(values, .1))
    upper = max(float(np.quantile(values, .99)), base + .4 * (float(raw.max()) - base))
    span = upper - base
    if span <= max(1e-10, abs(base) * 1e-6):
        raise ValueError("曲线近乎平坦，无法区分无接触与全程接触")
    low_values = values[values <= np.quantile(values, .2)]
    noise = float(np.median(np.abs(low_values - np.median(low_values))))
    on = base + max(span * on_ratio, noise * 6)
    off = base + max(span * off_ratio, noise * 2)
    if on >= float(raw.max()) or on <= off:
        raise ValueError("信号变化不足以区分基线噪声")
    candidates = []

    def add(kind, first, last, **extra):
        candidates.append({"kind": kind, "curve": curve["name"],
                           "bracket_start": float(times[first]), "bracket_end": float(times[last]),
                           "start": max(float(times[0]), float(times[first]) - padding),
                           "end": min(float(times[-1]), float(times[last]) + padding), **extra})

    active, run = False, 0
    for i, value in enumerate(values):
        changed = value <= off if active else value >= on
        run = run + 1 if changed else 0
        if run < 2:
            continue
        boundary = i - 1
        first = boundary
        if active:
            while first > 0 and values[first] < on:
                first -= 1
            add("contact_end", first, boundary)
        else:
            while first > 0 and values[first] > off:
                first -= 1
            add("contact_start", first, boundary, starts_before_possible=first == 0 and values[0] >= on)
        active, run = not active, 0
    if active:
        add("episode_end", len(times) - 1, len(times) - 1, ends_after_possible=True)
    transient = (raw >= on) & (values < on)
    begin = None
    for i, marked in enumerate([*transient.tolist(), False]):
        if marked and begin is None:
            begin = i
        elif not marked and begin is not None:
            add("short_excursion", max(0, begin - 1), min(len(times) - 1, i))
            begin = None
    return candidates, {"name": curve["name"], "unit": curve.get("unit"),
                        "baseline": base, "threshold": on, "release_threshold": off,
                        "noise_mad": noise, "method": "relative_hysteresis_2_samples"}


def merge_regions(candidates):
    merged = []
    for candidate in sorted(candidates, key=lambda c: (c["start"], c["end"], c["curve"], c["kind"])):
        if merged and candidate["start"] <= merged[-1]["end"] + 1e-6:
            merged[-1]["end"] = max(merged[-1]["end"], candidate["end"])
            merged[-1]["boundaries"].append(candidate)
        else:
            merged.append({"start": candidate["start"], "end": candidate["end"], "boundaries": [candidate]})
    return merged


def plan_dataset(ds, episodes=None, sides=("left", "right"), padding=.35, on_ratio=.1, off_ratio=.02, progress=None):
    units, full_windows, candidate_windows = [], 0, 0
    for ep in sorted(ds.episodes if episodes is None else episodes):
        ds.episode_meta(ep)
        try:
            curves, curve_error = ds.curves(ep), None
        except Exception as exc:
            curves, curve_error = [], str(exc)
        for side in sides:
            warnings, audits, candidates = [], [], []
            data_times = [float(r["timestamp"]) for r in ds.rows(ep)]
            try:
                native = [float(s["time"]) for s in ds.samples(ep, side)]
            except Exception as exc:
                native = []
                warnings.append(f"触觉时钟不可用：{exc}")
            times = [max(0.0, t) for t in (native or data_times)]
            times = sorted(set(times))
            if not times or not all(math.isfinite(t) for t in times):
                raise ValueError(f"EP {ep} {side} 缺少有效时钟")
            if not native:
                warnings.append("缺少本侧原始触觉时钟，候选段使用数据帧")
            if times[0] > data_times[0] + 1e-6 or times[-1] < data_times[-1] - 1e-6:
                warnings.append("触觉时钟未覆盖完整数据时间轴，缺失区段需人工检查")
            full_windows += len(windows(len(times), 6))
            if curve_error:
                warnings.append(f"曲线读取失败：{curve_error}")
            selected = [c for c in curves if c.get("side") == side and "in_contact" not in c["name"]]
            if not selected:
                warnings.append("没有本侧模拟量曲线；不自动推断全程无接触")
            for curve in selected:
                try:
                    found, audit = curve_boundaries(curve, padding, on_ratio, off_ratio)
                    candidates.extend(found)
                    audits.append(audit)
                    if not found:
                        warnings.append(f"{curve['name']} 没有可靠边界候选，需人工检查")
                    ct = curve["points"]
                    if float(ct[0][0]) > times[0] + 1e-6 or float(ct[-1][0]) < times[-1] - 1e-6:
                        warnings.append(f"{curve['name']} 没有覆盖整个采样时间轴")
                except ValueError as exc:
                    warnings.append(f"{curve['name']}：{exc}")
            unit = {"episode": ep, "side": side, "clock": side if native else "data", "duration": times[-1],
                    "warnings": warnings, "curves": audits, "full_frames": len(times), "regions": []}
            for number, region in enumerate(merge_regions(candidates)):
                # Include the neighboring native frames so a boundary falling
                # between two curve timestamps is not rounded out of the crop.
                first = max(0, bisect_left(times, region["start"]) - 1)
                end = min(len(times), bisect_right(times, region["end"]) + 1)
                native_times = times[first:end]
                tasks = [{"times": native_times[a:b], "state": "pending"} for a, b in windows(len(native_times), 6)]
                identity = f"{ep}:{side}:{number}:{native_times[0]}:{native_times[-1]}"
                unit["regions"].append({**region, "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                                        "start": native_times[0], "end": native_times[-1], "times": native_times,
                                        "states": [None] * len(native_times), "tasks": tasks, "manual_review": True})
                candidate_windows += len(tasks)
            units.append(unit)
        if progress:
            progress(ep, candidate_windows)
    return {"schema_version": 1, "pipeline": "curve_first", "dataset": ds.summary(),
            "created_at": time.time(), "state": "planned", "notice": NOTICE,
            "config": {"padding": padding, "on_ratio": on_ratio, "off_ratio": off_ratio, "frames_per_window": 6},
            "total_windows": candidate_windows, "full_scan_windows": full_windows,
            "done_windows": 0, "failed_windows": 0, "window_seconds": [], "units": units}


def model_states(result):
    """Missing, duplicate or unrecognized observations stay uncertain."""
    times = result["sample_times"]
    states = [None] * len(times)
    text = result["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        observations = json.loads(text)["observations"]
        if not isinstance(observations, list):
            raise ValueError("observations 不是数组")
        for item in observations:
            if not isinstance(item, dict):
                continue
            t, state = item.get("time"), item.get("state")
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
                continue
            index = min(range(len(times)), key=lambda i: abs(times[i] - t))
            tolerance = min(.002, min((abs(t-b) for b in times if b != times[index]), default=.02) / 3)
            if abs(times[index] - t) > tolerance or not isinstance(state, str) or state not in {"contact", "no_contact", "uncertain"}:
                continue
            states[index] = merge_state(states[index], state)
        if any(s is None for s in states):
            return [s or "uncertain" for s in states], "模型漏报时间点或返回无效状态"
        return states, None
    except (ValueError, TypeError, KeyError) as exc:
        return ["uncertain"] * len(times), f"模型 JSON 无效：{exc}"


def summarize_region(region, clock):
    times = region["times"]
    states = [s or "uncertain" for s in region["states"]]
    boundaries = []
    for i in range(1, len(times)):
        before, after = states[i - 1], states[i]
        if "uncertain" not in (before, after) and before != after:
            boundaries.append({"kind": "contact_start" if after == "contact" else "contact_end",
                               "bracket_start": times[i - 1], "bracket_end": times[i],
                               "candidate_time": times[i], "clock": clock, "confirmed": False})
    return {"boundaries": boundaries, "uncertain_frames": states.count("uncertain"),
            "status": "uncertain" if "uncertain" in states else "boundary_candidate" if boundaries else "no_transition_in_crop",
            "confirmed": False,
            "warning": "模型候选只在此裁剪段内有效；未发现变化可能意味着曲线误报或真实边界在段外，请人工确认／扩大范围。"}


class Client:
    def __init__(self, url, timeout=600):
        self.base, self.timeout = url.rstrip("/"), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(self.base + "/" + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc

    def infer(self, report, unit, region, task, checkpoint, prompt, camera):
        times = task["times"]
        if "job_id" not in task:
            body = {"dataset": report["dataset"]["id"], "episode": unit["episode"], "side": unit["side"],
                    "camera": camera, "mode": "frame" if len(times) == 1 else "window", "time": times[0],
                    "start": times[0], "end": times[-1], "frames": max(2, len(times)),
                    "prompt": (prompt + "\n当前片段由曲线圈定为可能接触/离开的边界段，曲线不是接触真值。"
                               "严格按每个提供的采样时间输出 JSON："
                               '{"observations":[{"time":秒数,"state":"uncertain"}],"intervals":[],"limitations":"..."}。'
                               "state 每项只能是 contact、no_contact 或 uncertain。不确定不能强行给出边界。")}
            task["job_id"] = self.call("api/model/jobs", body)["id"]
            task["state"] = "running"
            checkpoint()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            job = self.call("api/model/jobs/" + task["job_id"])
            if job["state"] == "done":
                return job["result"]
            if job["state"] == "error":
                task.update(state="pending", last_error=job.get("error", "模型任务失败"))
                task.pop("job_id", None)
                checkpoint()
                raise RuntimeError(job.get("error", "模型任务失败"))
            time.sleep(1)
        raise TimeoutError(f"窗口超时，保留 job_id 供 --resume 查询：{task['job_id']}")


def save_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        temporary = Path(stream.name)
    temporary.replace(path)


def run_model(report, client, checkpoint, prompt="", camera="observation.images.third_camera", progress=print):
    dataset = client.call("api/datasets", {"path": report["dataset"]["path"]})
    if dataset["id"] != report["dataset"]["id"]:
        raise ValueError("服务端数据集与曲线计划不一致，请重新规划")
    active = client.call(f"api/datasets/{dataset['id']}/screening")
    if active and active["state"] in {"queued", "planning", "running", "stopping"}:
        raise RuntimeError("网页全帧任务仍在运行，请先点停止并等待状态变为已停止")
    config = {"prompt": prompt, "camera": camera}
    if report.get("model_config", config) != config:
        raise ValueError("继续任务时须使用原来的 --prompt 和 --camera，避免混合不同目标的模型结果")
    report["model_config"] = config
    report.update(state="running", error=None)
    checkpoint()
    consecutive_errors = 0
    for unit in report["units"]:
        for region in unit["regions"]:
            for task in region["tasks"]:
                if task["state"] in {"done", "invalid_output"}:
                    continue
                started = time.monotonic()
                report["current"] = {"episode": unit["episode"], "side": unit["side"], "start": task["times"][0], "end": task["times"][-1]}
                checkpoint()
                result = client.infer(report, unit, region, task, checkpoint, prompt, camera)
                if len(result.get("sample_times", [])) != len(task["times"]):
                    raise ValueError("模型返回时间点数量与请求不一致")
                states, error = model_states(result)
                # The existing API spaces times uniformly. Each requested
                # crop is at most six consecutive native samples, so ensure
                # spacing actually maps to those same frames before using it.
                for expected, returned in zip(task["times"], result["sample_times"]):
                    nearest = min(region["times"], key=lambda t: abs(t - returned))
                    if abs(nearest - expected) > 1e-6:
                        raise ValueError("本段采样不均匀，现有 API 无法保证逐帧覆盖；请人工检查该段")
                for t, state in zip(task["times"], states):
                    index = region["times"].index(t)
                    region["states"][index] = merge_state(region["states"][index], state)
                task.update(state="invalid_output" if error else "done", result=result, error=error)
                region["model_review"] = summarize_region(region, unit["clock"])
                report["done_windows"] += 1
                report["failed_windows"] += bool(error)
                report["window_seconds"] = [*report["window_seconds"], time.monotonic() - started][-20:]
                recent = report["window_seconds"]
                report["eta_seconds"] = round(sum(recent) / len(recent) * (report["total_windows"] - report["done_windows"])) if len(recent) >= 2 else None
                checkpoint()
                eta = "校准中" if report["eta_seconds"] is None else f"约 {math.ceil(report['eta_seconds']/60)} 分钟"
                progress(f"EP {unit['episode']} {unit['side']} {task['times'][0]:.3f}–{task['times'][-1]:.3f}s · {report['done_windows']}/{report['total_windows']} 窗口 · 剩余 {eta}")
                consecutive_errors = consecutive_errors + 1 if error else 0
                if consecutive_errors >= 3:
                    raise RuntimeError("连续三个窗口输出无效，已保存进度；这些窗口保持不确定，请检查后继续")
    report.update(state="done", eta_seconds=0)
    checkpoint()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help="LeRobot 目录；与网页服务共享可读路径")
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("--episodes", help="逗号分隔的 EP 编号；默认全部")
    parser.add_argument("--side", choices=["both", "left", "right"], default="both")
    parser.add_argument("--padding", type=float, default=.35, help="曲线边界段前后各扩展的秒数")
    parser.add_argument("--on-ratio", type=float, default=.1)
    parser.add_argument("--off-ratio", type=float, default=.02)
    parser.add_argument("--prompt", default="", help="明确接触目标")
    parser.add_argument("--camera", default="observation.images.third_camera")
    parser.add_argument("--output", default="state/curve_first.json")
    parser.add_argument("--timeout", type=int, default=600, help="每窗推理等待秒数")
    parser.add_argument("--plan-only", action="store_true", help="只圈定区段、统计窗口，不调用模型")
    parser.add_argument("--resume", action="store_true", help="从 --output 继续，或执行已生成的计划")
    args = parser.parse_args(argv)
    if not math.isfinite(args.padding) or args.padding < 0 or not 0 < args.off_ratio < args.on_ratio < 1 or args.timeout < 1:
        parser.error("需要 padding >= 0，0 < off-ratio < on-ratio < 1，timeout >= 1")
    output = Path(args.output).resolve()
    if args.resume:
        report = json.loads(output.read_text())
        if report.get("pipeline") != "curve_first":
            parser.error("不是 curve_first 计划文件")
        if args.dataset and Path(args.dataset).resolve() != Path(report["dataset"]["path"]):
            parser.error("--dataset 与原计划不同")
        if args.episodes or args.side != "both":
            parser.error("--resume 使用原计划的 EP/侧选择，不接受新的选择")
    else:
        if not args.dataset:
            parser.error("首次运行需要 --dataset")
        if output.exists():
            parser.error("输出已存在；使用 --resume 继续，或指定新的 --output")
        ds = Dataset(args.dataset)
        episodes = [int(e) for e in args.episodes.split(",")] if args.episodes else None
        sides = ("left", "right") if args.side == "both" else (args.side,)
        report = plan_dataset(ds, episodes, sides, args.padding, args.on_ratio, args.off_ratio,
                              progress=lambda ep, count: print(f"曲线规划 EP {ep}：累计 {count} 个模型窗口", flush=True))
    checkpoint = lambda: save_report(report, output)
    checkpoint()
    regions = sum(len(u["regions"]) for u in report["units"])
    flagged = sum(bool(u["warnings"]) for u in report["units"])
    ratio = report["total_windows"] / max(1, report["full_scan_windows"])
    print(f"候选段 {regions}；模型窗口 {report['total_windows']} / 全帧 {report['full_scan_windows']}（{ratio:.1%}）；需要额外人工检查的侧/EP {flagged}。", flush=True)
    print(f"{NOTICE}\n计划/结果：{output}", flush=True)
    if args.plan_only:
        return
    try:
        run_model(report, Client(args.url, args.timeout), checkpoint, args.prompt, args.camera,
                  progress=lambda text: print(text, flush=True))
    except KeyboardInterrupt:
        report["state"] = "interrupted"
        checkpoint()
        print("已停止提交后续窗口，当前模型窗口可能仍在完成；--resume 将复用已保存的 job_id。", flush=True)
        return 130
    except Exception as exc:
        report.update(state="error", error=str(exc))
        checkpoint()
        raise SystemExit(f"已保存进度：{exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())

"""Persistent, frame-complete screening. Suggestions never write annotations."""
import hashlib
import json
import math
import threading
import time
import uuid

import numpy as np

from review import refine

STATES = {"contact", "no_contact", "uncertain"}
ACTIVE = {"queued", "planning", "running", "stopping"}


def windows(count, size):
    """One-frame overlap preserves transitions, including across window edges."""
    if size < 2:
        raise ValueError("窗口必须至少包含两个时间点")
    if count < 1:
        return []
    result = []
    start = 0
    while True:
        end = min(count, start + size)
        result.append((start, end))
        if end == count:
            return result
        start = end - 1


def parse_states(text, count):
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        states = json.loads(clean)["states"]
        if not isinstance(states, list) or len(states) != count:
            raise ValueError("states 数量与输入帧数不符")
        if any(not isinstance(s, str) or s not in STATES for s in states):
            raise ValueError("states 包含无效状态")
        return states, None
    except (ValueError, KeyError, TypeError) as exc:
        return ["uncertain"] * count, f"模型 JSON 无效：{exc}"


def merge_state(previous, value):
    return value if previous is None or previous == value else "uncertain"


def screening_result(unit, curves):
    """Conservative review queue: uncertainty, disagreement and ALL boundaries."""
    times, side = unit["times"], unit["side"]
    states = [s or "uncertain" for s in unit["states"]]
    events, curve_results, warnings = [], [], []

    def event(kind, first, last, reason, priority="high"):
        start = times[max(0, first - 1)]
        end = times[min(len(times) - 1, last + 1)]
        identity = f"{unit['episode']}:{side}:{kind}:{first}:{last}"
        events.append({"id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                       "side": side, "kind": kind, "start": start, "end": end,
                       "focus": times[first], "priority": priority, "reason": reason})

    def runs(mask, kind, reason):
        begin = None
        for i, value in enumerate([*mask, False]):
            if value and begin is None:
                begin = i
            elif not value and begin is not None:
                event(kind, begin, i - 1, reason)
                begin = None

    runs([s == "uncertain" for s in states], "uncertain", "模型不确定、窗口意见冲突或推理失败；需人工检查这一段")
    for i in range(1, len(states)):
        if states[i] != states[i - 1] and "uncertain" not in (states[i], states[i - 1]):
            event("contact_start" if states[i] == "contact" else "contact_end", i, i,
                  "模型接触状态发生变化：请逐帧选择精确边界", "boundary")
    for i in sorted({0, len(states) - 1}):
        if states[i] == "contact":
            event("episode_edge", i, i, "首尾帧已接触：确认是否需要开头已接触／结束仍接触", "boundary")

    for curve in curves:
        if curve.get("side") != side or "in_contact" in curve["name"]:
            continue  # Simulated boolean contact is not an independent sensor.
        points = np.asarray(curve["points"], dtype=float)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
            warnings.append(f"{curve['name']}：无效／不足采样")
            continue
        low, high = np.quantile(points[:, 1], [.1, .9])
        span = high - low
        if span <= max(1e-10, abs(high) * 1e-6) or low < 0:
            warnings.append(f"{curve['name']}：平坦或有负基线，无法自动设置接触阈值")
            continue
        on, off = float(low + span * .55), float(low + span * .3)
        reviewed = refine(curve["points"], side, float(points[0, 0]), float(points[-1, 0]), on, off)
        intervals = reviewed["intervals"]
        curve_results.append({"name": curve["name"], "unit": curve["unit"],
                              "threshold": on, "release_threshold": off, "intervals": intervals})
        mask = []
        for t, state in zip(times, states):
            active = any(i["start"] <= t and (i["end"] is None or t < i["end"]) for i in intervals)
            mask.append(state != "uncertain" and (state == "contact") != active
                        and points[0, 0] - 1e-6 <= t <= points[-1, 0] + 1e-6)
        runs(mask, "disagreement:" + curve["name"], f"模型与 {curve['name']} 自动阈值结果不一致（曲线未标定，不是真值）")
        runs([not points[0, 0] - 1e-6 <= t <= points[-1, 0] + 1e-6 for t in times],
             "curve_gap:" + curve["name"], f"{curve['name']} 未覆盖此段")
        for interval in intervals:
            for boundary in ("start", "end"):
                if interval[boundary] is not None:
                    i = min(range(len(times)), key=lambda j: abs(times[j] - interval[boundary]))
                    event("curve_" + boundary + ":" + curve["name"], i, i,
                          f"{curve['name']} 曲线候选边界；即使与模型一致，也需人工选择帧", "boundary")
    if not curve_results:
        event("no_curve", 0, len(times) - 1, "没有可自动复筛的有效模拟量曲线；不能据此判定全程无接触")
    warnings.extend(unit.get("warnings", []))
    if warnings:
        event("data_warning", 0, len(times) - 1, "；".join(warnings))
    candidates, begin = [], None
    for i, state in enumerate([*states, "end"]):
        if state == "contact" and begin is None:
            begin = i
        elif state != "contact" and begin is not None:
            candidates.append({"side": side, "start": times[begin] if begin > 0 and states[begin - 1] == "no_contact" else None,
                               "end": times[i] if i < len(times) and state == "no_contact" else None,
                               "observed_start": times[begin], "observed_end": times[i - 1]})
            begin = None
    # Identical review spans share one action, while retaining every reason.
    grouped = {}
    for item in events:
        key = (round(item["start"], 9), round(item["end"], 9), item["priority"])
        if key not in grouped:
            grouped[key] = {**item, "related_ids": [item["id"]], "reasons": [item["reason"]], "kinds": [item["kind"]]}
        else:
            grouped[key]["related_ids"].append(item["id"])
            grouped[key]["reasons"].append(item["reason"])
            grouped[key]["kinds"].append(item["kind"])
    events = list(grouped.values())
    for item in events:
        item["id"] = hashlib.sha256(":".join(sorted(item.pop("related_ids"))).encode()).hexdigest()[:20]
        item["reason"] = "；".join(dict.fromkeys(item.pop("reasons")))
        item["kinds"] = sorted(set(item["kinds"]))
    events.sort(key=lambda e: (e["priority"] != "high", e["start"], e["kind"]))
    return {"events": events, "candidates": candidates, "curve_results": curve_results,
            "warnings": warnings, "machine_no_contact": all(s == "no_contact" for s in states),
            "notice": "自动阈值只用于相对变化复筛，非物理接触真值；所有候选均未人工确认"}


class BatchManager:
    def __init__(self, connect, get_dataset, model):
        self.connect, self.get_dataset, self.model = connect, get_dataset, model
        self.active_id = None
        self.lock = threading.RLock()

    def tables(self, conn):
        conn.execute("CREATE TABLE IF NOT EXISTS screening_runs (id TEXT PRIMARY KEY, dataset TEXT, body TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS screening_units (run TEXT, episode INTEGER, side TEXT, body TEXT, PRIMARY KEY(run,episode,side))")
        conn.execute("CREATE TABLE IF NOT EXISTS screening_reviews (run TEXT, event TEXT, episode INTEGER, revision INTEGER, note TEXT, PRIMARY KEY(run,event))")

    def recover(self):
        # A stopped process never silently resumes GPU work on server startup.
        with self.connect() as conn:
            self.tables(conn)
            for identity, body in conn.execute("SELECT id,body FROM screening_runs").fetchall():
                run = json.loads(body)
                if run["state"] in ACTIVE:
                    run["state"] = "interrupted"
                    conn.execute("UPDATE screening_runs SET body=? WHERE id=?", (json.dumps(run), identity))

    def busy(self):
        return self.active_id is not None

    def get(self, identity):
        with self.connect() as conn:
            self.tables(conn)
            row = conn.execute("SELECT body FROM screening_runs WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("筛查任务不存在")
        return json.loads(row[0])

    def latest(self, dataset):
        with self.connect() as conn:
            self.tables(conn)
            row = conn.execute("SELECT body FROM screening_runs WHERE dataset=? ORDER BY rowid DESC LIMIT 1", (dataset,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_run(self, run):
        with self.connect() as conn:
            self.tables(conn)
            conn.execute("INSERT OR REPLACE INTO screening_runs VALUES (?,?,?)", (run["id"], run["dataset"], json.dumps(run, ensure_ascii=False)))

    def units(self, identity):
        with self.connect() as conn:
            self.tables(conn)
            rows = conn.execute("SELECT body FROM screening_units WHERE run=? ORDER BY episode,side", (identity,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_unit(self, identity, unit):
        with self.connect() as conn:
            self.tables(conn)
            conn.execute("INSERT OR REPLACE INTO screening_units VALUES (?,?,?,?)", (identity, unit["episode"], unit["side"], json.dumps(unit, ensure_ascii=False)))

    def launch(self, dataset, options, resume=None):
        with self.lock:
            if self.busy():
                raise ValueError("已有全量筛查正在运行")
            if resume:
                run = self.get(resume)
                if run["dataset"] != dataset or run["state"] not in {"cancelled", "interrupted", "error"}:
                    raise ValueError("仅可继续已停止／中断／失败的同一数据集任务")
                run.pop("error", None)
            else:
                run = {"id": uuid.uuid4().hex, "dataset": dataset, "options": options,
                       "created_at": time.time(), "done_windows": 0, "total_windows": None,
                       "done_units": 0, "total_units": 0, "processing_seconds": 0,
                       "window_seconds": [], "failed_windows": 0}
            run.update(state="queued", eta_seconds=None)
            self.put_run(run)
            self.active_id = run["id"]
            threading.Thread(target=self.work, args=(run["id"],), daemon=True).start()
            return run

    def cancel(self, identity):
        with self.lock:
            run = self.get(identity)
            if run["state"] in ACTIVE:
                run["state"] = "stopping"
                self.put_run(run)
            return run

    def progress(self, run):
        # Call under lock; do not overwrite a concurrent stop request.
        if self.get(run["id"])["state"] == "stopping":
            run["state"] = "stopping"
        samples = run["window_seconds"]
        run["eta_seconds"] = (round(sum(samples) / len(samples) * max(0, (run["total_windows"] or 0) - run["done_windows"]))
                              if len(samples) >= 2 and run["total_windows"] is not None else None)
        self.put_run(run)

    def details(self, identity, episode=None):
        run = self.get(identity)
        with self.connect() as conn:
            self.tables(conn)
            reviews = {r[0]: {"revision": r[1], "note": r[2]} for r in conn.execute(
                "SELECT event,revision,note FROM screening_reviews WHERE run=?", (identity,))}
            revisions = dict(conn.execute("SELECT episode,revision FROM annotations WHERE dataset=?", (run["dataset"],)))
        units = self.units(identity)
        summaries, results = [], []
        for unit in units:
            result = unit.get("result", {})
            events = [{**e, "reviewed": reviews.get(e["id"], {}).get("revision") == revisions.get(unit["episode"], 0),
                       "review_note": reviews.get(e["id"], {}).get("note", "")} for e in result.get("events", [])]
            summaries.append({"episode": unit["episode"], "side": unit["side"], "state": unit["state"],
                              "unreviewed": sum(not e["reviewed"] for e in events), "events": len(events),
                              "frames": len(unit["times"]), "done_windows": unit["done_windows"],
                              "total_windows": len(windows(len(unit["times"]), run["options"]["frames"]))})
            if episode is not None and unit["episode"] == episode:
                results.append({"episode": episode, "side": unit["side"], "clock": unit["clock"],
                                "state": unit["state"], "frames": len(unit["times"]),
                                "errors": unit["errors"], **result, "events": events})
        return {**run, "units": summaries, "results": results}

    def review(self, identity, event_id, episode, revision, note):
        run = self.get(identity)
        details = self.details(identity, episode)
        if not any(e["id"] == event_id for r in details["results"] for e in r["events"]):
            raise ValueError("复核位置不存在或此侧尚未筛查完成")
        with self.connect() as conn:
            self.tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision FROM annotations WHERE dataset=? AND episode=?", (run["dataset"], episode)).fetchone()
            if not row or row[0] != revision:
                raise ValueError("请先保存本 Episode 标注，再记录复核；保存版本必须一致")
            conn.execute("INSERT OR REPLACE INTO screening_reviews VALUES (?,?,?,?,?)", (identity, event_id, episode, revision, note))
        return {"ok": True}

    def plan(self, run, ds):
        existing = {(u["episode"], u["side"]): u for u in self.units(run["id"])}
        total = done = done_units = 0
        for ep in sorted(ds.episodes):
            for side in ("left", "right"):
                if self.get(run["id"])["state"] == "stopping":
                    return False
                unit = existing.get((ep, side))
                if unit is None:
                    warnings = []
                    try:
                        data = [float(r["timestamp"]) for r in ds.rows(ep)]
                        native = [float(r["time"]) for r in ds.samples(ep, side)]
                        clock = side if native and run["options"]["coverage"] == "native" else "data"
                        times = native if clock == side else data
                        if clock == side:
                            outside = [t for t in data if t < native[0] - 1e-6 or t > native[-1] + 1e-6]
                            if outside:
                                times = sorted([*times, *outside])
                                warnings.append("触觉采样没有覆盖完整数据时间轴；缺失区段使用场景数据帧，需人工检查")
                        if not native:
                            warnings.append("没有本侧原始触觉采样时钟，已退回数据帧；不能声称触觉全帧覆盖")
                        if clock == "data" and native and native[-1] > times[-1] + 1e-6:
                            times = [*times, native[-1]]  # Include native tail without claiming native full coverage.
                        if not times or any(not math.isfinite(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
                            raise ValueError("无有效、严格递增的采样时钟")
                    except Exception as exc:
                        times, clock = [0.0], "data"
                        warnings.append(f"读取采样时钟失败：{exc}")
                    unit = {"episode": ep, "side": side, "clock": clock, "times": times,
                            "states": [None] * len(times), "done_windows": 0, "errors": [],
                            "warnings": warnings, "state": "queued"}
                    self.put_unit(run["id"], unit)
                total += len(windows(len(unit["times"]), run["options"]["frames"]))
                done += unit["done_windows"]
                done_units += unit["state"] == "done"
        run.update(total_windows=total, done_windows=done, total_units=len(ds.episodes) * 2, done_units=done_units)
        return True

    def work(self, identity):
        run = self.get(identity)
        try:
            with self.lock:
                run["state"] = "planning"
                self.progress(run)
            ds = self.get_dataset(run["dataset"])
            planned = self.plan(run, ds)
            with self.lock:
                run["state"] = "running"
                self.progress(run)
            if not planned:
                run["state"] = "stopping"
            for unit in self.units(identity):
                if unit["state"] == "done":
                    continue
                if run["state"] == "stopping":
                    break
                ep, side = unit["episode"], unit["side"]
                cameras = ds.cameras(ep)
                scene = next((c for c in cameras if c["key"] == run["options"]["camera"]), None)
                scene = scene or next((c for c in cameras if c["name"] == "third_camera"), None)
                scene = scene or next((c for c in cameras if not c["name"].startswith("photon_")), None)
                tactile = next((c for c in cameras if c["name"] == f"photon_{side}"), None)
                if not tactile and "没有本侧触觉图像" not in unit["warnings"]:
                    unit["warnings"].append("没有本侧触觉图像")
                if not scene and "没有场景相机" not in unit["warnings"]:
                    unit["warnings"].append("没有场景相机")
                for number, (first, end) in enumerate(windows(len(unit["times"]), run["options"]["frames"])):
                    if number < unit["done_windows"]:
                        continue
                    with self.lock:
                        run.update(current={"episode": ep, "side": side, "start": unit["times"][first], "end": unit["times"][end - 1]})
                        self.progress(run)
                    if run["state"] == "stopping":
                        break
                    started = time.monotonic()
                    error = None
                    try:
                        if not scene and not tactile:
                            raise ValueError("没有可用图像")
                        images = []
                        for index in range(first, end):
                            t = unit["times"][index]
                            for cam in (scene, tactile):
                                if cam:
                                    pixels, frame = ds.frame(ep, cam["key"], t)
                                    images.append((f"index={index-first} time={t:.6f}s camera={cam['name']} frame={frame}", pixels))
                        prompt = (
                            f"你是双侧机器人物理接触筛查助手。仅判断 {side} 侧夹爪与任务目标的接触。"
                            f"任务上下文：{ds.episode_meta(ep).get('tasks', [])}。"
                            "场景图像提供上下文，photon 为本侧触觉图像。闭爪不等于接触，不要把桌面接触当成目标接触。"
                            f"按 index=0 到 {end-first-1} 逐帧判断；不要漏掉任何 index。"
                            "只能输出 JSON 对象 {\"states\":[...]}。每项只允许 contact、no_contact、uncertain，"
                            "states 数量必须等于时间点数量，而非图片数量。无法判断填 uncertain，不编造边界。\n"
                            + run["options"]["prompt"])
                        output = self.model.infer(images, prompt)
                        states, error = parse_states(output["text"], end - first)
                        unit["last_output"] = output["text"][:8000]
                    except Exception as exc:
                        states, error = ["uncertain"] * (end - first), str(exc)
                    if error:
                        run["failed_windows"] += 1
                        unit["errors"] = [*unit["errors"], {"window": number, "error": error[:1000]}][-12:]
                    for index, value in enumerate(states, first):
                        unit["states"][index] = merge_state(unit["states"][index], value)
                    unit.update(done_windows=number + 1, state="running")
                    self.put_unit(identity, unit)
                    seconds = time.monotonic() - started
                    with self.lock:
                        run["done_windows"] += 1
                        run["processing_seconds"] += seconds
                        run["window_seconds"] = [*run["window_seconds"], seconds][-20:]
                        self.progress(run)
                    # Repeated failures should not burn the whole dataset/GPU allocation.
                    if len(unit["errors"]) >= 3 and all(e["window"] == number - 2 + i for i, e in enumerate(unit["errors"][-3:])):
                        raise RuntimeError(f"连续三个窗口失败，已保留进度并停止。最近错误：{error}。检查后可继续；失败窗口保留为重点人工复核。")
                if unit["done_windows"] == len(windows(len(unit["times"]), run["options"]["frames"])):
                    try:
                        curves = ds.curves(ep)
                    except Exception as exc:
                        curves = []
                        unit["warnings"].append(f"读取曲线失败：{exc}")
                    unit["result"] = screening_result(unit, curves)
                    unit["state"] = "done"
                    self.put_unit(identity, unit)
                    with self.lock:
                        run["done_units"] += 1
                        self.progress(run)
            with self.lock:
                run["state"] = "cancelled" if run["state"] == "stopping" else "done"
                run["eta_seconds"] = None if run["state"] == "cancelled" else 0
                self.put_run(run)
        except Exception as exc:
            with self.lock:
                run.update(state="error", error=str(exc), eta_seconds=None)
                self.put_run(run)
        finally:
            with self.lock:
                self.active_id = None

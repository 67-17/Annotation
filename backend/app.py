import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .dataset import Dataset, safe_child
from .batch import BatchManager
from .model_service import LocalModel
from .review import refine

BASE = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("ANNOTATION_STATE_DIR", str(BASE / "state")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = STATE_DIR / "annotations.sqlite3"
DATASETS = {}
JOBS = {}
JOBS_LOCK = threading.Lock()
MODEL = LocalModel()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS datasets (id TEXT PRIMARY KEY, path TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS dataset_settings (dataset TEXT PRIMARY KEY, annotation_mode TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS annotations (dataset TEXT, episode INTEGER, revision INTEGER, body TEXT, PRIMARY KEY(dataset, episode))")
            yield conn
    finally:
        conn.close()


def dataset(identity):
    if identity not in DATASETS:
        with db() as conn:
            row = conn.execute("SELECT path FROM datasets WHERE id=?", (identity,)).fetchone()
        if not row:
            raise HTTPException(404, "请先导入数据集")
        loaded = Dataset(row[0])
        if loaded.id != identity:
            raise HTTPException(409, "数据集元数据已改变，请重新导入")
        DATASETS[identity] = loaded
    return DATASETS[identity]


@asynccontextmanager
async def lifespan(app):
    BATCH.recover()
    if os.environ.get("PRELOAD_MODEL") == "1":
        def preload():
            try:
                MODEL.preload()
                smoke_path = os.environ.get("SMOKE_DATASET")
                if smoke_path:
                    loaded = Dataset(smoke_path)
                    ep = min(loaded.episodes)
                    cameras = loaded.cameras(ep)
                    images = []
                    for name in ("third_camera", "photon_left"):
                        camera = next((c for c in cameras if c["name"] == name), None)
                        if camera:
                            pixels, number = loaded.frame(ep, camera["key"], 0.0)
                            images.append((f"{name} frame={number} episode_time=0s", pixels))
                    if not images:
                        raise ValueError("启动推理测试没有可用图像")
                    result = MODEL.infer(images, "描述这些机器人场景/触觉图像。判断是否能确认左侧接触物体；证据不足时明确说无法判断。请简洁回答。")
                    (STATE_DIR / "model_smoke.json").write_text(json.dumps(
                        {"dataset": smoke_path, "episode": ep, **result}, ensure_ascii=False, indent=2))
                    print("MODEL_SMOKE_OK " + json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as e:
                print(f"MODEL_STARTUP_ERROR {e}", flush=True)
        threading.Thread(target=preload, daemon=True).start()
    yield


app = FastAPI(title="Contact Annotation", lifespan=lifespan)


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(FileNotFoundError)
async def missing_file(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={"detail": f"找不到数据文件：{exc.filename or str(exc)}"})


class ImportBody(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


class Interval(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    side: Literal["left", "right"]
    start: float | None = Field(default=None, allow_inf_nan=False)
    end: float | None = Field(default=None, allow_inf_nan=False)
    clock: Literal["data", "left", "right"] = "data"
    starts_before: bool = False
    ends_after: bool = False
    confirmed: bool = False
    note: str = Field(default="", max_length=2000)
    source: Literal["manual", "model", "curve"] = "manual"


class SaveBody(BaseModel):
    revision: int = Field(ge=0)
    mode: Literal["single", "multiple"] = "single"
    intervals: list[Interval] = Field(max_length=200)
    no_contact: list[Literal["left", "right"]] = Field(default_factory=list)


class ModelBody(BaseModel):
    dataset: str
    episode: int = Field(ge=0)
    side: Literal["left", "right"] = "left"
    mode: Literal["frame", "window"] = "window"
    time: float = Field(default=0, ge=0, allow_inf_nan=False)
    start: float = Field(default=0, ge=0, allow_inf_nan=False)
    end: float = Field(default=2, ge=0, allow_inf_nan=False)
    camera: str = "observation.images.third_camera"
    frames: int = Field(default=6, ge=2, le=12)
    prompt: str = Field(default="", max_length=3000)


class CurveBody(BaseModel):
    side: Literal["left", "right"]
    name: str
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    threshold: float = Field(allow_inf_nan=False)
    release_threshold: float = Field(allow_inf_nan=False)


class BatchBody(BaseModel):
    dataset: str
    pipeline: Literal["curve_first", "full"] = "curve_first"
    contact_mode: Literal["single", "multiple"] = "single"
    episodes: list[int] | None = None
    coverage: Literal["native", "data"] = "native"
    frames: int = Field(default=6, ge=2, le=6)
    camera: str = "observation.images.third_camera"
    prompt: str = Field(default="", max_length=3000)


class ReviewBody(BaseModel):
    episode: int = Field(ge=0)
    revision: int = Field(ge=1)
    note: str = Field(default="人工已查看此处并保存标注／排除误报", max_length=2000)


BATCH = BatchManager(db, dataset, MODEL)


@app.post("/api/screening", status_code=202)
def start_screening(body: BatchBody):
    ds = dataset(body.dataset)
    if body.episodes is not None and (not body.episodes or len(set(body.episodes)) != len(body.episodes)
                                      or any(ep not in ds.episodes for ep in body.episodes)):
        raise ValueError("请选择有效且不重复的 Episode")
    if body.pipeline == "full" and body.episodes is not None:
        raise ValueError("全帧旧模式不支持选择 Episode")
    with JOBS_LOCK:
        if BATCH.busy() or any(j["state"] in ("queued", "running") for j in JOBS.values()):
            raise HTTPException(409, "模型已有任务，请等待完成或停止全量筛查")
        return BATCH.launch(body.dataset, body.model_dump(exclude={"dataset"}))


@app.get("/api/datasets/{identity}/screening")
def latest_screening(identity: str):
    dataset(identity)
    latest = BATCH.latest(identity)
    return BATCH.details(latest["id"]) if latest else None


@app.get("/api/screening/{job_id}")
def screening_status(job_id: str):
    return BATCH.details(job_id)


@app.get("/api/screening/{job_id}/episodes/{ep}")
def screening_episode(job_id: str, ep: int):
    return BATCH.details(job_id, ep)


@app.post("/api/screening/{job_id}/stop")
def stop_screening(job_id: str):
    return BATCH.cancel(job_id)


@app.post("/api/screening/{job_id}/resume", status_code=202)
def resume_screening(job_id: str):
    with JOBS_LOCK:
        if BATCH.busy() or any(j["state"] in ("queued", "running") for j in JOBS.values()):
            raise HTTPException(409, "模型已有任务")
        run = BATCH.get(job_id)
        return BATCH.launch(run["dataset"], run["options"], resume=job_id)


@app.put("/api/screening/{job_id}/reviews/{event_id}")
def review_screening(job_id: str, event_id: str, body: ReviewBody):
    return BATCH.review(job_id, event_id, body.episode, body.revision, body.note)


@app.get("/api/health")
def health():
    return {"ok": True, "model": MODEL.status(), "screening_pipeline": "curve_first"}


def exit_for_reload():
    time.sleep(.2)  # Let the HTTP response reach the caller first.
    os._exit(75)


@app.post("/api/admin/reload")
def reload_service(x_annotation_reload_token: str = Header(default="")):
    token = os.environ.get("ANNOTATION_RELOAD_TOKEN", "")
    if not token or not secrets.compare_digest(token, x_annotation_reload_token):
        raise HTTPException(403, "无效的重载令牌")
    with JOBS_LOCK:
        batch_stopping = BATCH.busy() and BATCH.get(BATCH.active_id)["state"] == "stopping"
        if ((BATCH.busy() and not batch_stopping)
                or any(j["state"] in ("queued", "running") for j in JOBS.values())):
            raise HTTPException(409, "请先停止筛查或模型任务，等待状态变为已停止后再更新")
    threading.Thread(target=exit_for_reload, daemon=True).start()
    return {"ok": True, "message": "服务将重新加载共享目录中的代码；标注和筛查进度保留"}


@app.post("/api/datasets")
def import_dataset(body: ImportBody):
    loaded = Dataset(body.path)
    DATASETS[loaded.id] = loaded
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO datasets VALUES (?,?)", (loaded.id, str(loaded.root)))
    return loaded.summary()


@app.get("/api/datasets")
def list_datasets():
    with db() as conn:
        rows = conn.execute("SELECT id,path FROM datasets ORDER BY rowid DESC").fetchall()
    return [{"id": k, "path": p, "name": Path(p).name} for k, p in rows]


@app.get("/api/datasets/{identity}/episodes/{ep}")
def get_episode(identity: str, ep: int, include_curves: bool = True):
    return dataset(identity).episode(ep, include_curves=include_curves)


@app.get("/api/datasets/{identity}/episodes/{ep}/curves")
def get_curves(identity: str, ep: int):
    return dataset(identity).curves(ep)


@app.get("/api/datasets/{identity}/episodes/{ep}/frame")
def get_frame(identity: str, ep: int, camera: str, time: float = Query(ge=0, allow_inf_nan=False)):
    encoded, number = dataset(identity).frame_jpeg(ep, camera, time)
    return Response(encoded, media_type="image/jpeg", headers={"X-Video-Frame": str(number), "Cache-Control": "private, max-age=300"})


@app.get("/api/datasets/{identity}/episodes/{ep}/video")
def get_video(identity: str, ep: int, camera: str):
    ds = dataset(identity)
    cam = ds.camera(ep, camera)
    return FileResponse(safe_child(ds.root, cam["relative_path"]), media_type="video/mp4")


@app.get("/api/datasets/{identity}/episodes/{ep}/annotations")
def annotations(identity: str, ep: int):
    dataset(identity).episode_meta(ep)
    with db() as conn:
        row = conn.execute("SELECT revision,body FROM annotations WHERE dataset=? AND episode=?", (identity, ep)).fetchone()
        setting = conn.execute("SELECT annotation_mode FROM dataset_settings WHERE dataset=?", (identity,)).fetchone()
    if not row:
        return {"revision": 0, "mode": setting[0] if setting else "single", "intervals": [], "no_contact": []}
    saved = json.loads(row[1])
    if "mode" not in saved:
        counts = {side: sum(item.get("side") == side for item in saved.get("intervals", []))
                  for side in ("left", "right")}
        saved["mode"] = "multiple" if max(counts.values(), default=0) > 1 else "single"
    return {"revision": row[0], **saved}


@app.put("/api/datasets/{identity}/episodes/{ep}/annotations")
def save_annotations(identity: str, ep: int, body: SaveBody):
    ds = dataset(identity)
    ds.episode_meta(ep)
    records = []
    identities = set()
    if body.mode == "single" and any(sum(item.side == side for item in body.intervals) > 1
                                      for side in ("left", "right")):
        raise ValueError("单段模式下每侧最多保留一个接触区间")
    for item in body.intervals:
        record = item.model_dump()
        if record["id"] in identities:
            if not record["id"].startswith("screening-slot-"):
                raise ValueError("标注 ID 重复")
            while record["id"] in identities:
                record["id"] = uuid.uuid4().hex
        identities.add(record["id"])
        if item.side in body.no_contact:
            raise ValueError("同一侧不能同时存在接触区间和全程无接触标记")
        if item.clock not in ("data", item.side):
            raise ValueError("请使用数据时钟或对应侧触觉时钟")
        if (item.start is not None and item.starts_before) or (item.end is not None and item.ends_after):
            raise ValueError("边界帧和越界状态不能同时设置")
        # Complete boundaries are exportable automatically. Incomplete pairs
        # remain saved drafts without requiring a separate confirmation box.
        record["confirmed"] = ((item.start is not None or item.starts_before)
                               and (item.end is not None or item.ends_after))
        record["start_anchor"] = ds.snap(ep, item.start, item.clock) if item.start is not None else None
        record["end_anchor"] = ds.snap(ep, item.end, item.clock) if item.end is not None else None
        for name in ("start", "end"):
            if record[name + "_anchor"]:
                record[name] = record[name + "_anchor"]["time"]
        if record["start"] is not None and record["end"] is not None and record["end"] <= record["start"]:
            raise ValueError("离开第一帧必须晚于接触第一帧")
        records.append(record)
    for side in ("left", "right"):
        ordered = sorted((r for r in records if r["side"] == side and r["confirmed"]), key=lambda r: r["start"] if r["start"] is not None else float("-inf"))
        for previous, current in zip(ordered, ordered[1:]):
            if previous["end"] is None or current["start"] is None or previous["end"] > current["start"]:
                raise ValueError("同一侧已确认的接触区间不能重叠")
    records.sort(key=lambda item: (item["side"], item["start"] is None,
                                   item["start"] if item["start"] is not None else
                                   item["end"] if item["end"] is not None else float("inf")))
    result = {"mode": body.mode, "intervals": records,
              "no_contact": sorted(set(body.no_contact)), "updated_at": time.time()}
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM annotations WHERE dataset=? AND episode=?", (identity, ep)).fetchone()
        revision = row[0] if row else 0
        if body.revision != revision:
            raise HTTPException(409, "标注已被其他窗口修改，请重新加载后再保存")
        conn.execute("INSERT OR REPLACE INTO dataset_settings VALUES (?,?)", (identity, body.mode))
        conn.execute("INSERT OR REPLACE INTO annotations VALUES (?,?,?,?)", (identity, ep, revision + 1, json.dumps(result, ensure_ascii=False)))
    return {"revision": revision + 1, **result}


@app.get("/api/datasets/{identity}/export")
def export(identity: str):
    ds = dataset(identity)
    with db() as conn:
        rows = conn.execute("SELECT episode,body FROM annotations WHERE dataset=?", (identity,)).fetchall()
    saved = {episode: json.loads(body) for episode, body in rows}
    episodes = []
    for episode in sorted(ds.episodes):
        result = {"episode": episode, "left": [], "right": []}
        for item in saved.get(episode, {}).get("intervals", []):
            # Export every interval with at least one manually marked boundary.
            # An unfinished draft still contains useful information: for
            # example, a contact first frame may be marked before the release
            # first frame is found.  Empty screening slots remain omitted.
            if not any((item.get("start") is not None,
                        item.get("end") is not None,
                        item.get("start_anchor") is not None,
                        item.get("end_anchor") is not None,
                        item.get("starts_before"),
                        item.get("ends_after"))):
                continue
            contact = item.get("start_anchor")
            release = item.get("end_anchor")
            result[item["side"]].append({
                "clock": item["clock"],
                "contact_time_s": contact["time"] if contact else None,
                "contact_frame": contact["sample_index"] if contact else None,
                "release_time_s": release["time"] if release else None,
                "release_frame": release["sample_index"] if release else None,
            })
        result["left"].sort(key=lambda item: item["contact_time_s"] if item["contact_time_s"] is not None else float("-inf"))
        result["right"].sort(key=lambda item: item["contact_time_s"] if item["contact_time_s"] is not None else float("-inf"))
        episodes.append(result)
    result = {"episodes": episodes}
    return Response(json.dumps(result, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="contact_annotations.json"'})


@app.post("/api/datasets/{identity}/episodes/{ep}/refine")
def refine_curve(identity: str, ep: int, body: CurveBody):
    if body.end <= body.start:
        raise ValueError("范围结束必须晚于开始")
    curves = dataset(identity).curves(ep)
    curve = next((c for c in curves if c["name"] == body.name and c["side"] == body.side), None)
    if not curve:
        raise ValueError("请选择对应侧的触觉曲线")
    return refine(curve["points"], body.side, body.start, body.end, body.threshold, body.release_threshold)


def inference_job(job_id, body):
    with JOBS_LOCK:
        JOBS[job_id]["state"] = "running"
    try:
        ds = dataset(body.dataset)
        episode = ds.episode(body.episode)
        if ((body.mode == "frame" and body.time > episode["duration"]) or
                (body.mode == "window" and body.end > episode["duration"])):
            raise ValueError("初筛范围超出 Episode")
        import numpy as np
        times = [body.time] if body.mode == "frame" else np.linspace(body.start, body.end, body.frames).tolist()
        cameras = ds.cameras(body.episode)
        keys = {c["key"] for c in cameras}
        selected = [body.camera]
        tactile = f"observation.images.photon_{body.side}"
        if tactile in keys and tactile not in selected:
            selected.append(tactile)
        if body.camera not in keys:
            raise ValueError("请选择数据集中存在的相机")
        images = []
        for t in times:
            for key in selected:
                pixels, number = ds.frame(body.episode, key, float(t))
                images.append((f"episode_time={t:.6f}s camera={key} video_frame={number}", pixels))
        prompt = (
            f"你是机器人接触标注助手。任务：{episode['tasks']}。仅判断 {body.side} 侧接触面与目标物体的物理接触。"
            "图像按时间排序；photon 是触觉图像，其他相机用于场景上下文。闭爪、夹紧和物体抬起不等于接触第一帧。"
            "输出 JSON 对象，包含 observations（每个采样时间的 contact/no_contact/uncertain 状态及依据）、"
            "intervals（多个候选区间，每项含 start、end；只能使用提供的时间，未知边界填 null）、limitations。"
            "稀疏采样只能缩小范围，不能声称确定未展示的第一帧。不确定时明确说明。\n" + body.prompt
        )
        result = MODEL.infer(images, prompt)
        with JOBS_LOCK:
            JOBS[job_id].update(state="done", result={**result, "sample_times": times,
                "side": body.side, "model_path": MODEL.status()["path"]})
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update(state="error", error=str(exc))


@app.post("/api/model/jobs", status_code=202)
def start_model(body: ModelBody):
    dataset(body.dataset).episode_meta(body.episode)
    if body.mode == "window" and body.end <= body.start:
        raise ValueError("初筛范围结束必须晚于开始")
    with JOBS_LOCK:
        if BATCH.busy() or any(j["state"] in ("queued", "running") for j in JOBS.values()):
            raise HTTPException(409, "模型已有任务，请等待完成")
        # Keep bounded job history in memory; raw outputs remain available in UI.
        while len(JOBS) >= 50:
            JOBS.pop(next(iter(JOBS)))
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {"id": job_id, "state": "queued", "request": body.model_dump()}
    threading.Thread(target=inference_job, args=(job_id, body), daemon=True).start()
    return {"id": job_id}


@app.get("/api/model/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        if job_id not in JOBS:
            raise HTTPException(404, "任务不存在")
        return dict(JOBS[job_id])


@app.get("/")
def index():
    return FileResponse(BASE / "static/index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

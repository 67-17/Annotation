"""Verify real image decoding and an actual GPU model response after deployment."""
import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DATASET = "/run/determined/NAS1/public/xihang/data-mechanism/r2s2r-runs/plug_insert_v3/prod50_s1000/export_tmp_20261014T234707/xarm7"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8088/")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--report", default="verification/model_response.json")
    args = parser.parse_args()
    base = args.url.rstrip("/") + "/"

    def call(path, body=None, binary=False):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read()
                return raw if binary else json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc

    health = call("api/health")
    dataset = call("api/datasets", {"path": args.dataset})
    ep = min(e["id"] for e in dataset["episodes"])
    prefix = f"api/datasets/{dataset['id']}/episodes/{ep}"
    episode = call(prefix)
    camera = next((c for c in episode["cameras"] if c["name"] == "third_camera"), episode["cameras"][0])
    image = call(prefix + "/frame?" + urllib.parse.urlencode({"camera": camera["key"], "time": 0}), binary=True)
    if not image.startswith(b"\xff\xd8"):
        raise RuntimeError("Frame endpoint did not return a JPEG")
    print(f"DATA_OK episode={ep} frames={len(episode['frames'])} curves={len(episode['curves'])} image_bytes={len(image)}", flush=True)
    job = call("api/model/jobs", {"dataset": dataset["id"], "episode": ep, "mode": "frame",
                                 "camera": camera["key"], "time": 0, "side": "left"})
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        result = call("api/model/jobs/" + job["id"])
        if result["state"] == "error":
            raise RuntimeError(result["error"])
        if result["state"] == "done":
            if not result["result"].get("text", "").strip():
                raise RuntimeError("Model returned empty output")
            report = {"dataset": args.dataset, "episode": ep, "health_before": health,
                      "health_after": call("api/health"), "job": result}
            path = Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print("MODEL_INFERENCE_OK " + json.dumps(result["result"], ensure_ascii=False))
            print(f"Report: {path.resolve()}")
            return
        time.sleep(2)
    raise TimeoutError(f"Model job did not finish within {args.timeout} seconds: {job['id']}")


if __name__ == "__main__":
    main()

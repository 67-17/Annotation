"""Lazy local Qwen inference. Readiness never implies a successful inference."""
import importlib.metadata
import os
import threading
import time

MODEL_PATH = os.environ.get("MODEL_PATH", "/run/determined/NAS1/public/HuggingFace/Qwen/Qwen3-VL-8B-Instruct")


class LocalModel:
    def __init__(self):
        self.lock = threading.Lock()
        self.model = None
        self.processor = None
        self.state = "not_loaded"
        self.error = None
        self.last_inference = None

    def status(self):
        return {"state": self.state, "error": self.error, "path": MODEL_PATH,
                "model": "Qwen3-VL-8B-Instruct", "last_inference": self.last_inference,
                "transformers": self.version("transformers")}

    @staticmethod
    def version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def _load(self):
        if self.model is not None:
            return
        self.state = "loading"
        self.error = None
        try:
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA 不可用；请在 amp-48g GPU 任务中运行模型服务")
            from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
            self.processor = AutoProcessor.from_pretrained(MODEL_PATH, local_files_only=True)
            self.model = Qwen3VLForConditionalGeneration.from_pretrained(
                MODEL_PATH, torch_dtype=torch.bfloat16, device_map={"": 0},
                attn_implementation="sdpa", local_files_only=True,
            ).eval()
            self.state = "ready"
        except Exception as e:
            self.state = "error"
            self.error = str(e)
            raise

    def preload(self):
        with self.lock:
            self._load()

    def infer(self, images, prompt):
        from PIL import Image
        with self.lock:
            self._load()
            import torch
            started = time.monotonic()
            content = []
            for label, image in images:
                image = Image.fromarray(image)
                image.thumbnail((672, 672))
                content.extend([{"type": "text", "text": label}, {"type": "image", "image": image}])
            content.append({"type": "text", "text": prompt})
            inputs = self.processor.apply_chat_template(
                [{"role": "user", "content": content}], tokenize=True,
                add_generation_prompt=True, return_dict=True, return_tensors="pt",
            ).to(self.model.device)
            try:
                with torch.inference_mode():
                    outputs = self.model.generate(**inputs, max_new_tokens=768, do_sample=False)
                text = self.processor.batch_decode(
                    outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                )[0]
                result = {"text": text, "seconds": round(time.monotonic() - started, 2),
                          "gpu": torch.cuda.get_device_name(0), "images": len(images)}
                self.last_inference = {k: v for k, v in result.items() if k != "text"}
                return result
            finally:
                del inputs

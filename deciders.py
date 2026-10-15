import base64
import io
import json
import os
import threading
import time
import uuid

import httpx
from PIL import Image

MIN_SIDE = 300
MAX_SIDE = 1024
MAX_ASPECT = 3


def fit(img):
    w, h = img.size
    w2, h2 = max(w, MIN_SIDE, h // MAX_ASPECT + 1), max(h, MIN_SIDE, w // MAX_ASPECT + 1)
    if (w2, h2) != (w, h):
        canvas = Image.new("RGB", (w2, h2), "white")
        canvas.paste(img, ((w2 - w) // 2, (h2 - h) // 2))
        img = canvas
    if max(img.size) > MAX_SIDE:
        img = img.copy()
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def jpeg_base64(img):
    if isinstance(img, str):
        return img
    buf = io.BytesIO()
    fit(img).save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def swap_placeholders(state, images):
    ids = {name: uuid.uuid4().hex for name in images}

    def walk(v):
        if isinstance(v, str):
            return ids.get(v, v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(state), {ids[name]: img for name, img in images.items()}


class Decider:
    usd_per_million_input = 0.0

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.lock = threading.Lock()

    def yes(self, state, question, images):
        p = self.many(state, {"q": question}, images)["q"]
        return p >= 0.5, p

    def many(self, state, questions, images):
        state, images = swap_placeholders(state, images)
        answers, tokens = self.probabilities(state, questions, images)
        with self.lock:
            self.calls += 1
            self.input_tokens += tokens
        return answers

    @property
    def usd(self):
        return self.input_tokens * self.usd_per_million_input / 1_000_000

    def probabilities(self, state, questions, images):
        raise NotImplementedError


class TypeSafeJev(Decider):
    usd_per_million_input = 0.042
    endpoint = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, model="jev-preview", api_key=None):
        super().__init__()
        self.model = model
        self.http = httpx.Client(timeout=300, limits=httpx.Limits(max_connections=64), headers={
            "Authorization": f"Bearer {api_key or os.environ['TYPESAFE_API_KEY']}",
            "TypeSafe-Danger-Zone-Images": "true",
            "TypeSafe-Danger-Zone-Long-Context": "true",
        })

    def probabilities(self, state, questions, images):
        body = {
            "model": self.model,
            "state": state,
            "questions": {name: {"type": "noul", "instructions": q} for name, q in questions.items()},
        }
        if images:
            body["assets"] = {name: {"type": "image", "format": "jpeg", "data_base64": jpeg_base64(img)} for name, img in images.items()}
        for attempt in range(5):
            r = self.http.post(self.endpoint, json=body)
            if r.status_code < 500 and r.status_code != 429:
                break
            time.sleep(2 ** attempt)
        r.raise_for_status()
        data = r.json()
        return {name: a["noul"] for name, a in data["answers"].items()}, data.get("usage", {}).get("input_tokens", 0)


class OpenAIDecisions(Decider):
    usd_per_million_input = 0.10

    def __init__(self, model="gpt-6-luna", api_key=None):
        super().__init__()
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(api_key=api_key)

    def probabilities(self, state, questions, images):
        content = [{"type": "input_text", "text": "state = " + json.dumps(state, ensure_ascii=False)}]
        for name, img in images.items():
            content.append({"type": "input_text", "text": f"image {name}:"})
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{jpeg_base64(img)}", "detail": "high"})
        decision = self.client.decisions.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
            questions=[{"type": "predicate", "name": name, "instructions": q} for name, q in questions.items()],
        )
        return {a.name: a.probability for a in decision.answers}, decision.usage.input_tokens

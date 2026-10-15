import base64
import io
import json
import os
import time

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
    buf = io.BytesIO()
    fit(img).save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


class Decider:
    def __init__(self):
        self.calls = 0

    def yes(self, state, question, images):
        self.calls += 1
        p = self.probability(state, question, images)
        return p >= 0.5, p

    def probability(self, state, question, images):
        raise NotImplementedError


class TypeSafeJev(Decider):
    endpoint = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, model="jev-preview", api_key=None):
        super().__init__()
        self.model = model
        self.http = httpx.Client(timeout=300, headers={
            "Authorization": f"Bearer {api_key or os.environ['TYPESAFE_API_KEY']}",
            "TypeSafe-Danger-Zone-Images": "true",
            "TypeSafe-Danger-Zone-Long-Context": "true",
        })

    def probability(self, state, question, images):
        body = {
            "model": self.model,
            "state": state,
            "questions": {"q": {"type": "noul", "instructions": question}},
            "assets": {name: {"type": "image", "format": "jpeg", "data_base64": jpeg_base64(img)} for name, img in images.items()},
        }
        for attempt in range(5):
            r = self.http.post(self.endpoint, json=body)
            if r.status_code < 500 and r.status_code != 429:
                break
            time.sleep(2 ** attempt)
        r.raise_for_status()
        return r.json()["answers"]["q"]["noul"]


class OpenAIDecisions(Decider):
    def __init__(self, model="gpt-6-luna", api_key=None):
        super().__init__()
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(api_key=api_key)

    def probability(self, state, question, images):
        content = [{"type": "input_text", "text": "state = " + json.dumps(state, ensure_ascii=False)}]
        for name, img in images.items():
            content.append({"type": "input_text", "text": f"image {name}:"})
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{jpeg_base64(img)}", "detail": "high"})
        decision = self.client.decisions.create(
            model=self.model,
            input=[{"role": "user", "content": content}],
            questions=[{"type": "predicate", "name": "q", "instructions": question}],
        )
        return decision.answers[0].probability

"""Thin wrapper around the OpenAI API (chat in JSON mode, plain chat, embeddings)."""
import json
from functools import lru_cache

import numpy as np
from openai import OpenAI

from . import config


@lru_cache(maxsize=1)
def client() -> OpenAI:
    return OpenAI()  # reads OPENAI_API_KEY


def chat_json(system: str, user: str) -> dict:
    r = client().chat.completions.create(
        model=config.OPENAI_MODEL, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return json.loads(r.choices[0].message.content)


def chat_text(system: str, user: str) -> str:
    r = client().chat.completions.create(
        model=config.OPENAI_MODEL, temperature=0.2,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return r.choices[0].message.content.strip()


def embed(texts: list[str]) -> np.ndarray:
    r = client().embeddings.create(model=config.EMBED_MODEL, input=texts)
    v = np.array([d.embedding for d in r.data], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)

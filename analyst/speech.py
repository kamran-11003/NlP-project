"""Local speech: faster-whisper for speech recognition, Piper for text to speech. No APIs."""
import io
import re
import wave
from functools import lru_cache

import numpy as np
import soundfile as sf

from . import config


@lru_cache(maxsize=1)
def _whisper():
    config.add_cuda_dlls()
    from faster_whisper import WhisperModel
    if config.WHISPER_DEVICE in ("auto", "cuda"):
        try:
            m = WhisperModel(config.WHISPER_MODEL, device="cuda", compute_type="float16")
            m.transcribe(np.zeros(16000, dtype=np.float32))  # warm-up, fails here if CUDA libs are missing
            return m
        except Exception:
            if config.WHISPER_DEVICE == "cuda":
                raise
    return WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")


def load_audio(data: bytes) -> np.ndarray:
    """Any WAV/FLAC/OGG bytes -> mono float32 at 16 kHz."""
    a, sr = sf.read(io.BytesIO(data), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


def transcribe(data: bytes) -> str:
    segments, _ = _whisper().transcribe(load_audio(data), language="en", beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


@lru_cache(maxsize=1)
def _voice():
    from piper import PiperVoice
    return PiperVoice.load(config.PIPER_VOICE)


def speakable(text: str) -> str:
    """Small clean-ups so numbers and symbols are read naturally."""
    text = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    text = text.replace("%", " percent").replace("&", " and ").replace("_", " ")
    text = re.sub(r"R\$\s?([\d.]+(\s(thousand|million|billion))?)", r"\1 reais", text)
    text = re.sub(r"\$\s?([\d.]+(\s(thousand|million|billion))?)", r"\1 dollars", text)
    return text


def synthesize(text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        _voice().synthesize_wav(speakable(text), w)
    return buf.getvalue()


def warm_up():
    _whisper()
    _voice()

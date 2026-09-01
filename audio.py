"""Captura de áudio em push-to-talk e transcrição local com faster-whisper."""

import sys
import threading

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


class PushToTalkRecorder:
    """Grava áudio enquanto a tecla está pressionada; ao soltar, devolve o buffer."""

    def __init__(self, sample_rate: int = 16000, device=None):
        self.sample_rate = sample_rate
        self.device = device
        self._frames: list[np.ndarray] = []
        self._recording = False
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[audio] aviso: {status}", file=sys.stderr)
        with self._lock:
            if self._recording:
                self._frames.append(indata.copy())

    def start(self) -> None:
        with self._lock:
            self._frames = []
            self._recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            if not self._frames:
                return np.zeros((0,), dtype=np.float32)
            return np.concatenate(self._frames, axis=0).flatten()


class Transcriber:
    """Transcrição local via faster-whisper. Nenhum áudio sai da máquina."""

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "pt",
    ):
        self.language = language
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if audio.size == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=1,
            vad_filter=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

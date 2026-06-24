"""Small NumPy/CuPy backend adapter."""

from __future__ import annotations

import numpy as np


class Backend:
    def __init__(self, device: str = "auto"):
        self.requested = device
        self.is_cuda = False
        self.xp = np
        self.cp = None
        self.name = "numpy"
        if device in {"auto", "cuda"}:
            try:
                import cupy as cp  # type: ignore

                cp.cuda.runtime.getDeviceCount()
                self.cp = cp
                self.xp = cp
                self.is_cuda = True
                self.name = "cupy"
            except Exception as exc:
                if device == "cuda":
                    raise SystemExit(f"CUDA requested but CuPy is unavailable: {exc}") from exc

    def to_numpy(self, value):
        if self.is_cuda:
            return self.cp.asnumpy(value)
        return np.asarray(value)

    def synchronize(self) -> None:
        if self.is_cuda:
            self.cp.cuda.Stream.null.synchronize()

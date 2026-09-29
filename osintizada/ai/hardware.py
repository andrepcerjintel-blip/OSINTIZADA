"""Status de hardware (somente informativo): CPU, RAM, GPU/VRAM quando acessível.

Nunca escolhe modelo sozinho — serve para o operador decidir (ex.: com pouca VRAM, preferir modelos
menores/quantizados; o runtime pode fazer offload para CPU, só fica mais lento).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess


def _ram_total_bytes() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        pass
    if platform.system() == "Windows":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys)
        except Exception:  # noqa: BLE001
            return None
    return None


def _nvidia_gpus() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        name, _, mem = line.partition(",")
        try:
            gpus.append({"name": name.strip(), "vram_mb": int(float(mem.strip()))})
        except ValueError:
            gpus.append({"name": name.strip(), "vram_mb": None})
    return gpus


def hardware_status() -> dict:
    ram = _ram_total_bytes()
    gpus = _nvidia_gpus()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "ram_gb": round(ram / 1024 ** 3, 1) if ram else None,
        "gpus": gpus,
        "note": ("Somente informativo. Com pouca VRAM, prefira modelos menores/quantizados; o runtime pode "
                 "usar a CPU (offload), mais devagar. O RINO não escolhe modelo sozinho."),
    }

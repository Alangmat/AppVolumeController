import ctypes
import ctypes.wintypes as wt
import threading
import time
from enum import Enum

import win32con
import comtypes
from pycaw.pycaw import AudioUtilities


# ================== CONFIG ==================
TARGET_EXES = ["Яндекс Музыка.exe"]  # если браузер: ["chrome.exe"] / ["msedge.exe"]
STEP = 0.02          # 2% за шаг роллера
TICK_SEC = 0.015     # 15мс
CACHE_TTL = 1.0      # перескан сессий раз в 1с
# ===========================================


# ---- WinAPI typedefs (compat) ----
if ctypes.sizeof(ctypes.c_void_p) == 8:
    WPARAM = ctypes.c_uint64
    LPARAM = ctypes.c_int64
    LRESULT = ctypes.c_int64
    ULONG_PTR = ctypes.c_uint64
else:
    WPARAM = ctypes.c_uint32
    LPARAM = ctypes.c_int32
    LRESULT = ctypes.c_int32
    ULONG_PTR = ctypes.c_uint32
# ----------------------------------


class Mode(Enum):
    STANDARD = 1
    YANDEX = 2


mode = Mode.STANDARD


def clamp(x: float, a: float = 0.0, b: float = 1.0) -> float:
    return max(a, min(b, x))


# ================== Audio session cache + coalescing ==================
_cached_vols = []
_cached_at = 0.0
_target_set = set(e.lower() for e in TARGET_EXES)

_pending = 0.0
_pending_lock = threading.Lock()


def refresh_target_sessions():
    """Пересканировать аудио-сессии и закэшировать SimpleAudioVolume для нужных процессов."""
    global _cached_vols, _cached_at
    sessions = AudioUtilities.GetAllSessions()
    vols = []
    for s in sessions:
        if s.Process and s.Process.name().lower() in _target_set:
            vols.append(s.SimpleAudioVolume)

    _cached_vols = vols
    _cached_at = time.time()


def get_cached_vols():
    """Взять кэш, при необходимости обновить."""
    if (time.time() - _cached_at) > CACHE_TTL or not _cached_vols:
        refresh_target_sessions()
    return _cached_vols


def push_target_volume_delta(delta: float):
    """Скопить дельту (чтобы не было очереди из событий)."""
    global _pending
    with _pending_lock:
        _pending += delta


def volume_worker():
    """
    Фоновый воркер:
    - Инициализирует COM (обязательно!)
    - Раз в TICK_SEC применяет суммарную дельту одним/несколькими SetMasterVolume.
    """
    global _pending

    # !!! ВАЖНО: COM init именно внутри потока
    comtypes.CoInitialize()
    try:
        while True:
            time.sleep(TICK_SEC)

            with _pending_lock:
                delta = _pending
                _pending = 0.0

            if delta == 0.0:
                continue

            try:
                vols = get_cached_vols()
                if not vols:
                    continue

                for v in vols:
                    cur = v.GetMasterVolume()
                    v.SetMasterVolume(clamp(cur + delta), None)

            except Exception:
                # Если сессия умерла/COM глюкнул — обновим кэш и не падаем
                try:
                    refresh_target_sessions()
                except Exception:
                    pass
    finally:
        comtypes.CoUninitialize()


# запускаем воркер
threading.Thread(target=volume_worker, daemon=True).start()
# ======================================================================


def toggle_mode():
    global mode
    mode = Mode.YANDEX if mode == Mode.STANDARD else Mode.STANDARD
    print(f"[MODE] -> {mode.name}")


# ================== Low-level keyboard hook ==================
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

hook_id = None


# --- WinAPI prototypes (важно для ctypes) ---
user32.SetWindowsHookExW.restype = wt.HANDLE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wt.HINSTANCE, wt.DWORD]

user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]

user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, WPARAM, LPARAM]

user32.GetMessageW.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]

user32.TranslateMessage.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]

user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]

kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

kernel32.GetLastError.restype = wt.DWORD


def win_error_text(err_code: int) -> str:
    return ctypes.FormatError(err_code).strip()


@LowLevelKeyboardProc
def hook_callback(nCode, wParam, lParam):
    if nCode == win32con.HC_ACTION and (wParam == WM_KEYDOWN or wParam == WM_SYSKEYDOWN):
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode

        # Нажатие роллера -> переключение режима
        if vk == VK_VOLUME_MUTE:
            toggle_mode()
            return 1  # блокируем системный mute

        if mode == Mode.YANDEX:
            if vk == VK_VOLUME_UP:
                push_target_volume_delta(+STEP)
                return 1
            if vk == VK_VOLUME_DOWN:
                push_target_volume_delta(-STEP)
                return 1

    return user32.CallNextHookEx(hook_id, nCode, wParam, lParam)


def install_hook():
    global hook_id
    h_instance = kernel32.GetModuleHandleW(None)

    hook_id = user32.SetWindowsHookExW(
        WH_KEYBOARD_LL,
        ctypes.cast(hook_callback, ctypes.c_void_p),
        h_instance,
        0
    )

    if not hook_id:
        err = kernel32.GetLastError()
        raise RuntimeError(f"Не удалось поставить хук. GetLastError={err} ({win_error_text(err)})")


def message_loop():
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    print("Knob hook started.")
    print(f"TARGET_EXES = {TARGET_EXES}")
    print("Press the knob (Mute) to toggle режимы: STANDARD <-> YANDEX")
    install_hook()
    try:
        message_loop()
    finally:
        if hook_id:
            user32.UnhookWindowsHookEx(hook_id)

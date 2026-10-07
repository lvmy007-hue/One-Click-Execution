# -*- coding: utf-8 -*-
"""把匹配、核验、一键执行控制台收到系统托盘。一个图标，右键能单独显示。"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
PID_FILE = HERE / "tray.pid"
LOG_FILE = HERE / "tray.log"
MUTEX_NAME = "Local\\DZ-yijian-tray-apps"
CREATE_NO_WINDOW = 0x08000000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
WM_CLOSE = 0x0010

SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_POPUP = 0x00000010
MF_GRAYED = 0x00000001

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
TH32CS_SNAPPROCESS = 0x00000002
GW_OWNER = 4
IDI_APPLICATION = 32512

ID_MATCH_SHOW, ID_MATCH_HIDE, ID_MATCH_CLOSE = 1, 2, 3
ID_DETECT_SHOW, ID_DETECT_HIDE, ID_DETECT_CLOSE = 4, 5, 6
ID_PIPE_SHOW, ID_PIPE_HIDE, ID_PIPE_CLOSE = 10, 11, 12
ID_ALL_SHOW, ID_ALL_HIDE, ID_QUIT = 7, 8, 9

APPS = (
    {
        "key": "match",
        "label": "数据匹配",
        "exes": ("datamatching.exe",),
        "titles": ("数据匹配系统", "数据匹配"),
        "show": ID_MATCH_SHOW,
        "hide": ID_MATCH_HIDE,
        "close": ID_MATCH_CLOSE,
    },
    {
        "key": "detect",
        "label": "联系方式核验",
        "exes": ("detectsuite.exe",),
        "titles": ("联系方式核验", "核验系统"),
        "show": ID_DETECT_SHOW,
        "hide": ID_DETECT_HIDE,
        "close": ID_DETECT_CLOSE,
    },
    {
        "key": "pipe",
        "label": "一键执行",
        "exes": (),
        "pid_file": HERE / "pipeline.pid",
        "titles": ("一键执行",),
        "show": ID_PIPE_SHOW,
        "hide": ID_PIPE_HIDE,
        "close": ID_PIPE_CLOSE,
    },
)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
        ("lPrivate", wintypes.DWORD),
    ]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def log(*args) -> None:
    line = " ".join(str(a) for a in args)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    alt = exe.with_name("pythonw.exe")
    return alt if alt.is_file() else exe


def python_console_path() -> Path:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.is_file():
            return alt
    return exe


def launch_script(script: str, args: list[str] | None = None) -> None:
    """单独开一个控制台跑脚本，默认藏起来，托盘可再显示。"""
    py = python_console_path()
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    CREATE_NEW_CONSOLE = 0x00000010
    env = os.environ.copy()
    env.pop("WT_SESSION", None)
    env.pop("TERM_PROGRAM", None)
    subprocess.Popen(
        [str(py), str(HERE / script), *(args or [])],
        cwd=str(HERE),
        env=env,
        close_fds=True,
        startupinfo=si,
        creationflags=CREATE_NEW_CONSOLE,
    )
    ensure_running()


def launch_daily() -> None:
    launch_script("run_pipeline.py", ["--daily"])


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def write_pid(path: Path, pid: int | None = None) -> None:
    path.write_text(str(pid or os.getpid()), encoding="utf-8")


def clear_pid(path: Path, pid: int | None = None) -> None:
    cur = read_pid(path)
    if pid is not None and cur and cur != pid:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_running() -> None:
    old = read_pid(PID_FILE)
    if old and pid_alive(old):
        return
    subprocess.Popen(
        [str(pythonw_path()), str(Path(__file__).resolve())],
        cwd=str(HERE),
        close_fds=True,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def launch_exe(exe: str) -> None:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    subprocess.Popen(
        [exe],
        cwd=str(Path(exe).parent),
        close_fds=True,
        startupinfo=si,
    )
    ensure_running()


def _exe_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(32768)
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(handle)
    if not ok:
        return ""
    return Path(buf.value).name.lower()


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _classname(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


SKIP_CLASS = ("baidu", "ime", "pyinstaller", "pseudoconsole", "gdi+")
HOST_CLASS = ("cascadia_hosting_window_class", "consolewindowclass")


def _window_ok(hwnd: int) -> bool:
    if not user32.IsWindow(hwnd):
        return False
    if user32.GetWindow(hwnd, GW_OWNER):
        return False
    cls = _classname(hwnd).lower()
    if any(s in cls for s in SKIP_CLASS):
        return False
    rc = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return False
    return (rc.right - rc.left) > 8 and (rc.bottom - rc.top) > 8


def app_pids(app: dict) -> list[int]:
    out: list[int] = []
    exes = tuple(app.get("exes") or ())
    if exes:
        out.extend(pids_for_exes(exes))
    pf = app.get("pid_file")
    if pf:
        p = read_pid(Path(pf))
        if pid_alive(p) and p not in out:
            out.append(p)
    return out


def _match_app(hwnd: int, app: dict) -> bool:
    cls = _classname(hwnd).lower()
    if not (any(c in cls for c in HOST_CLASS) or cls.startswith("console")):
        return False
    title = _title(hwnd)
    if any(k in title for k in app.get("titles") or ()):
        return True
    pid = _pid_of(hwnd)
    return bool(pid) and pid in app_pids(app)


def enum_app_hwnds() -> dict[str, list[int]]:
    found = {app["key"]: [] for app in APPS}

    def cb(hwnd, _lparam):
        hwnd = int(hwnd)
        if not _window_ok(hwnd):
            return True
        for app in APPS:
            if _match_app(hwnd, app):
                found[app["key"]].append(hwnd)
                break
        return True

    fn = WNDENUMPROC(cb)
    user32.EnumWindows(fn, 0)
    return found


def pids_for_exes(exes: tuple[str, ...]) -> list[int]:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out = []
    try:
        if not kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            return []
        while True:
            name = (pe.szExeFile or "").lower()
            if name in exes and pe.th32ProcessID:
                out.append(int(pe.th32ProcessID))
            if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return out


def show_hwnd(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)


def hide_hwnd(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_HIDE)


def dismiss_ime_toasts() -> int:
    """清掉因反复 ShowWindow 顶出来的百度输入法气泡，不关输入法本身。"""
    n = 0

    def cb(hwnd, _lparam):
        nonlocal n
        hwnd = int(hwnd)
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _title(hwnd)
        cls = _classname(hwnd)
        if title == "BaiduImeUIPMsgWindow" or "PyInstaller Onefile" in title:
            user32.ShowWindow(hwnd, SW_HIDE)
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            n += 1
        elif "BaiduImeUIP" in cls or "PyInstallerOnefile" in cls:
            user32.ShowWindow(hwnd, SW_HIDE)
            n += 1
        return True

    fn = WNDENUMPROC(cb)
    user32.EnumWindows(fn, 0)
    return n


def close_hwnd(hwnd: int) -> None:
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def kill_pids(pids: list[int]) -> None:
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not handle:
            continue
        kernel32.TerminateProcess(handle, 0)
        kernel32.CloseHandle(handle)


def load_icon() -> int:
    for p in (
        Path(r"E:\数据匹配系统\DataMatching.exe"),
        Path(r"E:\数据核验系统\DetectSuite.exe"),
    ):
        if not p.is_file():
            continue
        icon = shell32.ExtractIconW(0, str(p), 0)
        if icon and icon not in (0, 1, -1):
            return int(icon)
    return int(user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)))


class TrayApps:
    def __init__(self):
        self.hwnd = None
        self.icon = None
        self.nid = None
        self.want_show = {app["key"]: False for app in APPS}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self._wndproc = WNDPROC(self._wndproc_impl)
        self._hidden: set[int] = set()
        self._no_hide: set[int] = set()

    def _status(self) -> dict[str, str]:
        hwnds = enum_app_hwnds()
        out = {}
        for app in APPS:
            key = app["key"]
            wins = [h for h in hwnds.get(key, []) if user32.IsWindow(h)]
            live = bool(wins) or bool(app_pids(app))
            if not live:
                out[key] = "未运行"
            elif any(user32.IsWindowVisible(h) for h in wins):
                out[key] = "窗口在"
            else:
                out[key] = "已隐藏"
        return out

    def _tip(self) -> str:
        st = self._status()
        parts = [f"{app['label']} {st[app['key']]}" for app in APPS]
        return "  |  ".join(parts)[:127]

    def _apply(self) -> None:
        hwnds = enum_app_hwnds()
        with self.lock:
            want = dict(self.want_show)
        for app in APPS:
            for hwnd in hwnds.get(app["key"], []):
                if want[app["key"]]:
                    self._hidden.discard(hwnd)
                    self._no_hide.discard(hwnd)
                    if not user32.IsWindowVisible(hwnd):
                        show_hwnd(hwnd)
                    continue
                if hwnd in self._no_hide:
                    continue
                if hwnd in self._hidden and not user32.IsWindowVisible(hwnd):
                    continue
                if not (user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd)):
                    self._hidden.add(hwnd)
                    continue
                hide_hwnd(hwnd)
                time.sleep(0.05)
                if user32.IsWindowVisible(hwnd):
                    self._no_hide.add(hwnd)
                    log("窗口不肯藏，不再重复操作", _title(hwnd))
                else:
                    self._hidden.add(hwnd)

    def set_show(self, key: str, show: bool) -> None:
        with self.lock:
            self.want_show[key] = show
        self._apply()
        self._modify_tip()

    def set_all(self, show: bool) -> None:
        with self.lock:
            for key in self.want_show:
                self.want_show[key] = show
        self._apply()
        self._modify_tip()

    def toggle_all(self) -> None:
        st = self._status()
        any_hidden = any(v == "已隐藏" for v in st.values())
        any_live = any(v != "未运行" for v in st.values())
        if not any_live:
            return
        self.set_all(any_hidden)

    def close_app(self, key: str) -> None:
        app = next(a for a in APPS if a["key"] == key)
        with self.lock:
            self.want_show[key] = False
        hwnds = enum_app_hwnds().get(key, [])
        for hwnd in hwnds:
            close_hwnd(hwnd)
        time.sleep(1.2)
        left = app_pids(app)
        if left:
            kill_pids(left)
            log("已结束", app["label"], left)
        self._modify_tip()

    def restore_all(self) -> None:
        self.set_all(True)

    def _modify_tip(self) -> None:
        if not self.nid or not self.hwnd:
            return
        self.nid.szTip = self._tip()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def _popup(self) -> None:
        st = self._status()
        popup = user32.CreatePopupMenu()
        for app in APPS:
            sub = user32.CreatePopupMenu()
            live = st[app["key"]] != "未运行"
            flags = MF_STRING if live else MF_STRING | MF_GRAYED
            user32.AppendMenuW(sub, flags, app["show"], "显示窗口")
            user32.AppendMenuW(sub, flags, app["hide"], "隐藏窗口")
            user32.AppendMenuW(sub, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(sub, flags, app["close"], "退出程序")
            user32.AppendMenuW(
                popup, MF_POPUP, sub,
                f"{app['label']}  ·  {st[app['key']]}",
            )
        user32.AppendMenuW(popup, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(popup, MF_STRING, ID_ALL_SHOW, "全部显示")
        user32.AppendMenuW(popup, MF_STRING, ID_ALL_HIDE, "全部隐藏")
        user32.AppendMenuW(popup, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(popup, MF_STRING, ID_QUIT, "退出托盘（还原窗口）")

        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            popup,
            TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x, pt.y, 0, self.hwnd, None,
        )
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(popup)
        self._on_cmd(int(cmd or 0))

    def _on_cmd(self, cmd: int) -> None:
        for app in APPS:
            if cmd == app["show"]:
                self.set_show(app["key"], True)
                return
            if cmd == app["hide"]:
                self.set_show(app["key"], False)
                return
            if cmd == app["close"]:
                self.close_app(app["key"])
                return
        if cmd == ID_ALL_SHOW:
            self.set_all(True)
        elif cmd == ID_ALL_HIDE:
            self.set_all(False)
        elif cmd == ID_QUIT:
            user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)

    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = int(lparam) & 0xFFFF
            if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup()
            elif ev == WM_LBUTTONUP:
                self.toggle_all()
            return 0
        if msg == WM_COMMAND:
            self._on_cmd(int(wparam) & 0xFFFF)
            return 0
        if msg == WM_DESTROY:
            self._teardown()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _poll(self) -> None:
        while not self.stop.wait(5.0):
            try:
                self._apply()
                self._modify_tip()
            except Exception as e:
                log("同步窗口失败", e)

    def _teardown(self) -> None:
        self.stop.set()
        try:
            self.restore_all()
        except Exception:
            pass
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None
        clear_pid(PID_FILE, os.getpid())
        log("托盘已退出，窗口已还原")

    def run(self) -> None:
        write_pid(PID_FILE)
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "DZTrayAppsHidden"
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hIcon = load_icon()
        wc.lpszClassName = cls_name
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise RuntimeError("RegisterClassExW 失败")
        WS_POPUP = 0x80000000
        self.hwnd = user32.CreateWindowExW(
            0, cls_name, "店表托盘", WS_POPUP, 0, 0, 0, 0,
            None, None, hinst, None,
        )
        if not self.hwnd:
            raise RuntimeError("CreateWindowExW 失败")
        self.icon = wc.hIcon
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.icon
        nid.szTip = "匹配 / 核验 / 一键"
        self.nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise RuntimeError("Shell_NotifyIcon 失败")
        n = dismiss_ime_toasts()
        if n:
            log("已清掉输入法气泡", n)
        self._apply()
        self._modify_tip()
        threading.Thread(target=self._poll, daemon=True).start()
        log("托盘已启动 PID", os.getpid())
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def _single_instance() -> bool:
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        return False
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def main() -> None:
    if "--launch-daily" in sys.argv:
        launch_daily()
        return
    log("正在启动")
    if os.name != "nt":
        sys.exit("仅 Windows 可用")
    if not _single_instance():
        return
    old = read_pid(PID_FILE)
    if old and old != os.getpid() and pid_alive(old):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DZ.TrayApps")
    except Exception:
        pass
    TrayApps().run()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("托盘异常", e)
        raise

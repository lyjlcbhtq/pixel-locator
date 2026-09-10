#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winctl.py —— Windows 窗口控制工具 v1.0（纯标准库，ctypes 调 Win32 API）
=====================================================================

列出可见窗口、置顶/激活、最小化到后台、恢复、查矩形、窗口截图（前台激活截/
后台免激活截，供 findtext OCR 用）。stdout 一律输出 JSON；退出码 0=成功，
1=窗口未找到，2=参数/用法错误，3=截图失败。

用法
----
  python winctl.py --list                 # 列出所有可见窗口（含句柄/标题/进程/矩形）
  python winctl.py --top "记事本"          # 置顶并激活
  python winctl.py --untop "记事本"        # 取消置顶
  python winctl.py --hide "记事本"         # 最小化到后台（不抢焦点）
  python winctl.py --restore "记事本"      # 恢复
  python winctl.py --rect "记事本"         # 查询窗口矩形
  python winctl.py --shot "记事本" --out a.png    # 激活后截图（最可靠）
  python winctl.py --background "ZCode" --out b.png  # 免激活后台截图（不抢焦点）

关键词匹配标题子串（不区分大小写）；纯数字视为窗口句柄；--proc 同时匹配进程名；
多个命中默认取 Z 序最上面的一个，--index N 另选，动作加 --all 对全部命中生效。
截图坐标系：图内 (x,y) → 屏幕 (rect[0]+x, rect[1]+y)（输出 JSON 里已给 rect）。
"""
import argparse
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# 平台守卫：本工具完全建立在 Win32 API 之上（user32 / gdi32 / gdiplus），
# 仅支持 Windows。在其他平台上给出明确可读的 JSON 错误并退出，
# 而不是让使用者看到晦涩的 ImportError 或 WinDLL 失败。
# ---------------------------------------------------------------------------
if sys.platform != "win32":
    print(json.dumps({
        "ok": False,
        "tool": "winctl",
        "error": "winctl 仅支持 Windows（依赖 user32 / gdi32 / gdiplus 的 Win32 API）",
        "error_en": "winctl is Windows-only: it calls Win32 APIs through ctypes.WinDLL.",
        "platform": sys.platform,
        "hint": "在 Linux / macOS 上请改用 vlocate、findtext、tmatch —— 这三个工具的核心定位能力跨平台。",
    }, ensure_ascii=False))
    sys.exit(2)

import ctypes  # noqa: E402
from ctypes import wintypes  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VERSION = "1.0"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)

HWND = wintypes.HWND
BOOL = wintypes.BOOL
DWORD = wintypes.DWORD
LONG = wintypes.LONG
WORD = wintypes.WORD
UINT = wintypes.UINT


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
                ("biPlanes", WORD), ("biBitCount", WORD), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", LONG),
                ("biYPelsPerMeter", LONG), ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", ctypes.c_ubyte * 8)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_int), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", BOOL), ("ExternalSupervisor", ctypes.c_void_p)]


# PNG 编码器 CLSID {557CF406-1A04-11D3-9A73-0000F81EF32E}
PNG_CLSID = GUID(0x557CF406, 0x1A04, 0x11D3,
                 (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))

SW_MINIMIZE, SW_RESTORE, SW_SHOW = 6, 9, 5
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x1, 0x2, 0x40
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x8
PW_RENDERFULLCONTENT = 0x2
WHITENESS = 1
DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS = 14, 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(BOOL, HWND, ctypes.c_void_p), ctypes.c_void_p]
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
user32.PrintWindow.argtypes = [HWND, wintypes.HDC, UINT]
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, UINT]
user32.SetWindowPos.restype = BOOL
user32.SetForegroundWindow.restype = BOOL
user32.SetForegroundWindow.argtypes = [HWND]
user32.GetForegroundWindow.restype = HWND
user32.GetForegroundWindow.argtypes = []
user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
user32.GetWindowThreadProcessId.restype = DWORD
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
user32.GetWindowDC.restype = wintypes.HDC
user32.GetWindowDC.argtypes = [HWND]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [HWND]
user32.ReleaseDC.argtypes = [HWND, wintypes.HDC]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.IsWindow.argtypes = [HWND]
user32.IsWindow.restype = BOOL
user32.IsWindowVisible.restype = BOOL
user32.IsWindowVisible.argtypes = [HWND]
user32.IsIconic.restype = BOOL
user32.IsIconic.argtypes = [HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.BringWindowToTop.argtypes = [HWND]
kernel32.GetCurrentThreadId.restype = DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(DWORD)]
dwmapi.DwmGetWindowAttribute.argtypes = [HWND, DWORD, ctypes.c_void_p, DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, DWORD]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.PatBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, DWORD]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_ulonglong),
                                   ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p]
gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [wintypes.HBITMAP, wintypes.HANDLE,
                                                ctypes.POINTER(ctypes.c_void_p)]
gdiplus.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                        ctypes.POINTER(GUID), ctypes.c_void_p]


class WinError(Exception):
    pass


class CaptureError(Exception):
    pass


def emit(obj, code=0):
    print(json_dumps(obj))
    sys.exit(code)


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 窗口枚举与信息
# ---------------------------------------------------------------------------
def get_window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_process_name(hwnd):
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    if pid.value:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if h:
            size = DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
            kernel32.CloseHandle(h)
    return name, pid.value


def get_rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def dwm_attr_rect(hwnd, attr):
    r = RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, attr, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return [r.left, r.top, r.right, r.bottom]
    return None


def is_cloaked(hwnd):
    v = DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return bool(v.value)


def get_ex_style(hwnd):
    try:
        return user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    except AttributeError:
        return user32.GetWindowLongW(hwnd, GWL_EXSTYLE)


def window_info(hwnd):
    proc, pid = get_process_name(hwnd)
    r = get_rect(hwnd)
    return {
        "hwnd": hwnd,
        "title": get_window_text(hwnd),
        "class": get_class_name(hwnd),
        "process": proc,
        "pid": pid,
        "rect": r,
        "size": [r[2] - r[0], r[3] - r[1]],
        "topmost": bool(get_ex_style(hwnd) & WS_EX_TOPMOST),
        "minimized": bool(user32.IsIconic(hwnd)),
        "cloaked": is_cloaked(hwnd),
    }


ENUM_PROC = ctypes.WINFUNCTYPE(BOOL, HWND, ctypes.c_void_p)


def enum_windows():
    out = []
    try:
        @ENUM_PROC
        def _cb(hwnd, _lparam):
            out.append(hwnd)
            return True
        user32.EnumWindows(_cb, None)
    except Exception:
        pass
    return out  # EnumWindows 顺序即 Z 序（上→下）


def visible_windows(include_empty=False):
    result = []
    for hwnd in enum_windows():
        if not user32.IsWindowVisible(hwnd) or is_cloaked(hwnd):
            continue
        w = window_info(hwnd)
        if not include_empty and not w["title"]:
            continue
        result.append(w)
    return result


def find_windows(keyword, use_proc=False, include_empty=False):
    """关键词匹配：纯数字→句柄精确匹配；否则标题子串（--proc 加进程名）"""
    if keyword.isdigit():
        hwnd = int(keyword)
        if user32.IsWindow(hwnd):
            return [window_info(hwnd)]
        return []
    kw = keyword.lower()
    out = []
    for w in visible_windows(include_empty=include_empty):
        if kw in w["title"].lower() or (use_proc and kw in (w["process"] or "").lower()):
            out.append(w)
    return out


def pick(matches, index, what):
    if not matches:
        emit({"ok": False, "error": f"未找到匹配窗口: {what}",
              "hint": "用 python winctl.py --list 查看现有窗口标题"}, 1)
    if index >= len(matches):
        emit({"ok": False, "error": f"--index={index} 超出匹配数({len(matches)})",
              "matches": matches}, 1)
    return matches[index]


# ---------------------------------------------------------------------------
# 焦点 / 置顶 / 截图
# ---------------------------------------------------------------------------
def force_foreground(hwnd):
    """绕过前台锁激活窗口：AttachThreadInput + ALT 键兜底"""
    if user32.GetForegroundWindow() == hwnd:
        return True
    tid_this = kernel32.GetCurrentThreadId()
    attached = []
    try:
        for target in (user32.GetForegroundWindow(), hwnd):
            if not target:
                continue
            pid = DWORD(0)
            tid = user32.GetWindowThreadProcessId(target, ctypes.byref(pid))
            if tid and tid != tid_this and tid not in attached:
                if user32.AttachThreadInput(tid_this, tid, True):
                    attached.append(tid)
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        ok = user32.GetForegroundWindow() == hwnd
        if not ok:
            user32.keybd_event(0x12, 0, 0, 0)       # ALT down 解除前台锁
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)       # ALT up
            ok = user32.GetForegroundWindow() == hwnd
        return ok
    finally:
        for tid in attached:
            user32.AttachThreadInput(tid_this, tid, False)


def capture_window(hwnd, out_path):
    """PrintWindow 截图 → GDI+ 存 PNG。图内(x,y) → 屏幕(rect[0]+x, rect[1]+y)"""
    out_path = os.path.abspath(out_path)
    d = os.path.dirname(out_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise CaptureError(f"窗口矩形无效 {w}x{h}")
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    if not mem:
        raise CaptureError("CreateCompatibleDC 失败")
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 负高 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    ppv = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(ppv), None, 0)
    if not hbm or not ppv:
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)
        raise CaptureError("CreateDIBSection 失败")
    old = gdi32.SelectObject(mem, hbm)
    gdi32.PatBlt(mem, 0, 0, w, h, WHITENESS)
    ok = user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT) or user32.PrintWindow(hwnd, mem, 0)
    if not ok:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)
        raise CaptureError("PrintWindow 失败（窗口拒绝渲染）")
    gdi32.SelectObject(mem, old)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, hdc)

    token = ctypes.c_ulonglong(0)
    inp = GdiplusStartupInput()
    inp.GdiplusVersion = 1
    st = gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None)
    if st != 0:
        gdi32.DeleteObject(hbm)
        raise CaptureError(f"GdiplusStartup 失败 status={st}")
    img = ctypes.c_void_p()
    try:
        if gdiplus.GdipCreateBitmapFromHBITMAP(hbm, None, ctypes.byref(img)) != 0 or not img:
            raise CaptureError("GdipCreateBitmapFromHBITMAP 失败")
        st = gdiplus.GdipSaveImageToFile(img, out_path, ctypes.byref(PNG_CLSID), None)
        if st != 0:
            raise CaptureError(f"GdipSaveImageToFile 失败 status={st}")
    finally:
        if img:
            gdiplus.GdipDisposeImage(img)
        gdiplus.GdiplusShutdown(token)
        gdi32.DeleteObject(hbm)
    return {"path": out_path, "width": w, "height": h,
            "bytes": os.path.getsize(out_path),
            "coord_map": "屏幕坐标 = rect[0:2] + 图内坐标"}


def set_topmost(hwnd, topmost):
    return bool(user32.SetWindowPos(hwnd, HWND_TOPMOST if topmost else HWND_NOTOPMOST,
                                    0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW))


# ---------------------------------------------------------------------------
# 各动作
# ---------------------------------------------------------------------------
def act_list(opts):
    wins = visible_windows(include_empty=opts.include_empty)
    emit({"ok": True, "action": "list", "count": len(wins),
          "note": "按Z序(上→下)排列；--include-empty 可含无标题窗口",
          "windows": wins})


def act_top(opts, topmost=True):
    matches = find_windows(opts.keyword, opts.proc)
    sel = pick(matches, opts.index, opts.keyword)
    was_min = sel["minimized"]
    if was_min:
        user32.ShowWindow(sel["hwnd"], SW_RESTORE)
        time.sleep(0.15)
    set_topmost(sel["hwnd"], topmost)
    activated = force_foreground(sel["hwnd"]) if topmost else None
    info = window_info(sel["hwnd"])
    emit({"ok": True, "action": "top" if topmost else "untop",
          "matched_count": len(matches), "selected": info,
          "was_minimized": was_min, "activated": activated,
          "matches": [{"hwnd": m["hwnd"], "title": m["title"], "process": m["process"]}
                      for m in matches],
          "hint": "取消置顶用 --untop"} if topmost else
         {"ok": True, "action": "untop", "matched_count": len(matches),
          "selected": info, "matches": [{"hwnd": m["hwnd"], "title": m["title"],
                                         "process": m["process"]} for m in matches]})


def act_hide(opts):
    matches = find_windows(opts.keyword, opts.proc)
    targets = matches if opts.all else [pick(matches, opts.index, opts.keyword)]
    results = []
    for w in targets:
        user32.ShowWindow(w["hwnd"], SW_MINIMIZE)
        time.sleep(0.1)
        results.append({"hwnd": w["hwnd"], "title": w["title"],
                        "minimized": bool(user32.IsIconic(w["hwnd"]))})
    emit({"ok": True, "action": "hide", "matched_count": len(matches),
          "affected": results, "note": "最小化不抢焦点；恢复用 --restore"})


def act_restore(opts):
    matches = find_windows(opts.keyword, opts.proc)
    targets = matches if opts.all else [pick(matches, opts.index, opts.keyword)]
    results = []
    for w in targets:
        if user32.IsIconic(w["hwnd"]):
            user32.ShowWindow(w["hwnd"], SW_RESTORE)
            time.sleep(0.1)
        results.append({"hwnd": w["hwnd"], "title": w["title"],
                        "minimized": bool(user32.IsIconic(w["hwnd"])),
                        "activated": force_foreground(w["hwnd"])})
    emit({"ok": True, "action": "restore", "matched_count": len(matches), "affected": results})


def act_rect(opts):
    matches = find_windows(opts.keyword, opts.proc)
    sel = pick(matches, opts.index, opts.keyword)
    info = window_info(sel["hwnd"])
    info["frame_bounds"] = dwm_attr_rect(sel["hwnd"], DWMWA_EXTENDED_FRAME_BOUNDS)
    emit({"ok": True, "action": "rect", "matched_count": len(matches), "selected": info,
          "coord_note": "截图图内(x,y)→屏幕(rect[0]+x, rect[1]+y)"})


def act_shot(opts, activate):
    matches = find_windows(opts.keyword, opts.proc)
    sel = pick(matches, opts.index, opts.keyword)
    hwnd = sel["hwnd"]
    out_path = opts.out or time.strftime("winctl_截图_%Y%m%d_%H%M%S.png")
    warnings = []
    fg_before = user32.GetForegroundWindow()
    if activate:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.2)
        force_foreground(hwnd)
        time.sleep(0.25)   # 等渲染稳定
    else:
        if sel["topmost"]:
            set_topmost(hwnd, False)   # 切到后台：去掉置顶
            warnings.append("已取消置顶（切到后台）")
        if user32.IsIconic(hwnd):
            warnings.append("窗口已最小化，后台截图可能不完整")
    try:
        cap = capture_window(hwnd, out_path)
    except CaptureError as e:
        emit({"ok": False, "action": "shot" if activate else "background",
              "error": str(e), "selected": sel}, 3)
    info = window_info(hwnd)
    result = {"ok": True, "action": "shot" if activate else "background",
              "matched_count": len(matches), "selected": info,
              "activated": bool(activate),
              "focus_changed": user32.GetForegroundWindow() != fg_before and bool(activate),
              "screenshot": cap}
    if warnings:
        result["warnings"] = warnings
    emit(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    # 物理像素坐标（多 DPI 屏下 GetWindowRect/截图尺寸一致，OCR 坐标映射才准）
    try:
        if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            user32.SetProcessDPIAware()
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    p = argparse.ArgumentParser(
        prog="winctl.py", description="Windows 窗口控制（JSON输出）",
        epilog='示例: python winctl.py --shot "记事本" --out shot.png')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出所有可见窗口")
    g.add_argument("--top", metavar="关键词", help="置顶并激活窗口")
    g.add_argument("--untop", metavar="关键词", help="取消置顶")
    g.add_argument("--hide", metavar="关键词", help="最小化到后台（不抢焦点）")
    g.add_argument("--restore", metavar="关键词", help="恢复窗口")
    g.add_argument("--rect", metavar="关键词", help="查询窗口矩形")
    g.add_argument("--shot", metavar="关键词", help="激活后截图保存 PNG（最可靠）")
    g.add_argument("--background", metavar="关键词", help="免激活后台截图（不抢焦点，供OCR）")
    p.add_argument("--index", type=int, default=0, help="多个命中时选第几个（0起，默认Z序最上）")
    p.add_argument("--all", action="store_true", help="动作对全部匹配窗口生效（hide/restore）")
    p.add_argument("--proc", action="store_true", help="关键词同时匹配进程名")
    p.add_argument("--out", default=None, help="截图保存路径（--shot/--background）")
    p.add_argument("--include-empty", action="store_true", help="--list 时包含无标题窗口")
    p.add_argument("--version", action="version", version=f"winctl {VERSION}")
    opts = p.parse_args()

    if (opts.top is not None or opts.untop is not None or opts.hide is not None
            or opts.restore is not None or opts.rect is not None or opts.shot is not None
            or opts.background is not None) and opts.index < 0:
        emit({"ok": False, "error": "--index 必须 >= 0"}, 2)
    opts.keyword = next((getattr(opts, k) for k in
                         ("top", "untop", "hide", "restore", "rect", "shot", "background")
                         if getattr(opts, k) is not None), None)
    if opts.keyword is not None and not opts.keyword.strip():
        emit({"ok": False, "error": "关键词不能为空（空关键词会匹配所有窗口，已拒绝）",
              "hint": "用 --list 先查看窗口"}, 2)

    if opts.list:
        act_list(opts)
    elif opts.top:
        act_top(opts, topmost=True)
    elif opts.untop:
        act_top(opts, topmost=False)
    elif opts.hide:
        act_hide(opts)
    elif opts.restore:
        act_restore(opts)
    elif opts.rect:
        act_rect(opts)
    elif opts.shot:
        act_shot(opts, activate=True)
    elif opts.background:
        act_shot(opts, activate=False)


if __name__ == "__main__":
    main()

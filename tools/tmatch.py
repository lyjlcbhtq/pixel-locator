#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tmatch.py —— 模板匹配工具 v1.0（找没有文字的元素，与 findtext 找字互补）
========================================================================

学习通的播放按钮/进度条/游戏图标等无文字元素，OCR 找不到——用模板匹配"找图"。
findtext 找字 + tmatch 找图 = 完整眼睛。

GPU 加速：优先 cv2.cuda（CUDA 版 OpenCV），不可用自动降级 CPU（功能一致），
启动打印实际模式。CPU 模式下用 OpenCV 内置多线程。

用法
----
  python tmatch.py find <模板图> <目标截图> [--threshold 0.8] [--scales 0.8,1.0,1.2]
  python tmatch.py findall <目标截图> <模板1> <模板2> ... [--threshold 0.8]
  python tmatch.py collect <截图> --rect x,y,w,h --name 名字    # 裁剪存为模板（存 模板\目录）
  python tmatch.py list                                        # 列出模板库
  python tmatch.py bench <模板图> <目标截图> [--loops 20]       # GPU/CPU 计时对比

import 两用：
  from tmatch import find_all
  find_all("模板.png", "截图.png", threshold=0.8)  -> [{name,x,y,w,h,conf,scale,method}, ...]

输出：stdout 纯 JSON；退出码 0=找到 1=未找到/无匹配 2=参数/文件错误。
坐标语义：匹配区域左上角 (x,y)，尺寸 (w,h)；中心点 = (x+w//2, y+h//2)。
"""
import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VERSION = "1.0"
BASE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(BASE, "模板")
DEFAULT_SCALES = [0.8, 1.0, 1.2]
TM_METHOD = cv2.TM_CCOEFF_NORMED


# ---------------------------------------------------------------------------
# 模式检测：GPU 可用则用 cv2.cuda，否则 CPU
# ---------------------------------------------------------------------------
_mode_cache = None


def imread_cn(path):
    """中文路径安全读图（cv2.imread 不支持非ASCII路径，见经验库 EXP-049）"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None


def detect_mode(force=False):
    """返回 {'mode': 'gpu'|'cpu', 'reason': str}；GPU 需设备>0 且 createTemplateMatching 可用"""
    global _mode_cache
    if _mode_cache is not None and not force:
        return _mode_cache
    mode, reason = "cpu", ""
    try:
        n = cv2.cuda.getCudaEnabledDeviceCount()
        if n > 0:
            try:
                cv2.cuda.createTemplateMatching(cv2.CV_32F, TM_METHOD)
                mode, reason = "gpu", f"检测到 {n} 个 CUDA 设备，cv2.cuda.createTemplateMatching 可用"
            except Exception as e:
                reason = f"检测到 {n} 个 CUDA 设备，但 createTemplateMatching 不可用: {e}"
        else:
            reason = "cv2.cuda.getCudaEnabledDeviceCount()=0（当前 OpenCV 为 CPU 构建版）"
    except Exception as e:
        reason = f"cv2.cuda 模块不可用: {e}"
    _mode_cache = {"mode": mode, "reason": reason}
    return _mode_cache


def gpu_match(image_f, templ_f):
    """CUDA 路径：输入为 float32 灰度 ndarray，返回相关系数矩阵"""
    tm = cv2.cuda.createTemplateMatching(cv2.CV_32F, TM_METHOD)
    tm.setImage(templ_f)
    return tm.match(image_f)


def cpu_match(image_g, templ_g):
    return cv2.matchTemplate(image_g, templ_g, TM_METHOD)


def match_at_scale(image_g, templ_g):
    """单尺度匹配，返回 (max_val, max_loc)。GPU/CPU 自动选择。"""
    mode = detect_mode()["mode"]
    if mode == "gpu":
        try:
            gimg = cv2.cuda_GpuMat()
            gimg.upload(image_g.astype(np.float32))
            gtpl = cv2.cuda_GpuMat()
            gtpl.upload(templ_g.astype(np.float32))
            res = gpu_match(gimg, gtpl).download()
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            return max_val, max_loc
        except Exception:
            _mode_cache.update({"mode": "cpu", "reason": "GPU 执行失败，本次降级 CPU"})
    res = cpu_match(image_g, templ_g)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return max_val, max_loc


# ---------------------------------------------------------------------------
# 核心：单模板多尺度匹配
# ---------------------------------------------------------------------------
def find_multi(image_bgr, templ_bgr, threshold=0.8, scales=None, max_results=1):
    """多尺度+多实例：返回按置信度排序的匹配列表（邻近去重）。巡回29 补强。"""
    scales = scales or DEFAULT_SCALES
    image_g = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    th, tw = templ_bgr.shape[:2]
    ih, iw = image_g.shape[:2]
    all_hits = []
    for sc in scales:
        w, h = max(4, int(tw * sc)), max(4, int(th * sc))
        if w >= iw or h >= ih:
            continue
        tpl = cv2.resize(templ_bgr, (w, h), interpolation=cv2.INTER_AREA)
        tpl_g = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY).astype(np.float32)
        res = cpu_match(image_g, tpl_g)
        res_work = res.copy()
        for _ in range(max_results):
            _, max_val, max_loc, _ = cv2.minMaxLoc(res_work)
            if max_val < threshold:
                break
            all_hits.append({"x": max_loc[0], "y": max_loc[1], "w": w, "h": h,
                             "conf": round(float(max_val), 4), "scale": sc,
                             "cx": max_loc[0] + w // 2, "cy": max_loc[1] + h // 2})
            # 邻近抑制：清零已命中区域
            x0, y0 = max(0, max_loc[0] - w), max(0, max_loc[1] - h)      # 抑制窗=模板1.5倍（防同物邻近平移连点）
            x1, y1 = min(iw, max_loc[0] + int(w * 1.5)), min(ih, max_loc[1] + int(h * 1.5))
            res_work[y0:y1, x0:x1] = -1
    all_hits.sort(key=lambda c: -c["conf"])
    return all_hits[:max_results]


def find_one(image_bgr, templ_bgr, threshold=0.8, scales=None):
    """多尺度模板匹配。返回最佳 dict 或 None（未达阈值）。"""
    scales = scales or DEFAULT_SCALES
    image_g = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    th, tw = templ_bgr.shape[:2]
    ih, iw = image_g.shape[:2]
    best = None
    for sc in scales:
        w, h = max(4, int(tw * sc)), max(4, int(th * sc))
        if w >= iw or h >= ih:
            continue
        tpl = cv2.resize(templ_bgr, (w, h), interpolation=cv2.INTER_AREA)
        tpl_g = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY).astype(np.float32)
        val, loc = match_at_scale(image_g, tpl_g)
        cand = {"x": loc[0], "y": loc[1], "w": w, "h": h, "conf": round(float(val), 4), "scale": sc}
        if best is None or val > best["conf"]:
            best = cand
    if best and best["conf"] >= threshold:
        best["cx"] = best["x"] + best["w"] // 2
        best["cy"] = best["y"] + best["h"] // 2
        return best
    return best  # 可能低于阈值：调用方按 threshold 判定


def find_all(templ_path, image_path, threshold=0.8, scales=None, max_results=1):
    """模块两用入口：单模板文件 vs 截图，返回结果 dict（JSON 可序列化）。"""
    mode = detect_mode()
    image_bgr = imread_cn(image_path)
    templ_bgr = imread_cn(templ_path)
    if image_bgr is None:
        return {"ok": False, "error": f"目标截图读取失败: {image_path}"}
    if templ_bgr is None:
        return {"ok": False, "error": f"模板读取失败: {templ_path}"}
    t0 = time.perf_counter()
    if max_results > 1:
        matches = find_multi(image_bgr, templ_bgr, threshold, scales, max_results)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        name = os.path.splitext(os.path.basename(templ_path))[0]
        return {"ok": True, "tool": "tmatch", "version": VERSION, "mode": mode["mode"],
                "mode_reason": mode["reason"], "template": name,
                "image": {"path": image_path, "w": image_bgr.shape[1], "h": image_bgr.shape[0]},
                "threshold": threshold, "elapsed_ms": ms,
                "found": bool(matches), "count": len(matches), "matches": matches,
                "match": matches[0] if matches else None}
    best = find_one(image_bgr, templ_bgr, threshold, scales)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    name = os.path.splitext(os.path.basename(templ_path))[0]
    return {"ok": True, "tool": "tmatch", "version": VERSION, "mode": mode["mode"],
            "mode_reason": mode["reason"], "template": name,
            "image": {"path": image_path, "w": image_bgr.shape[1], "h": image_bgr.shape[0]},
            "threshold": threshold, "elapsed_ms": ms,
            "found": bool(best and best["conf"] >= threshold),
            "match": best}


def find_templates(templ_names, image_path, threshold=0.8, scales=None, tpl_dir=TPL_DIR):
    """多模板批量：一次读入目标截图，逐模板匹配（截图只解码一次）。"""
    mode = detect_mode()
    image_bgr = imread_cn(image_path)
    if image_bgr is None:
        return {"ok": False, "error": f"目标截图读取失败: {image_path}"}
    results, missing = [], []
    t0 = time.perf_counter()
    for name in templ_names:
        tp = name if os.path.isabs(name) else os.path.join(tpl_dir, name if name.endswith(".png") else name + ".png")
        if not os.path.exists(tp):
            missing.append(name)
            continue
        tpl = imread_cn(tp)
        best = find_one(image_bgr, tpl, threshold, scales)
        results.append({"template": os.path.splitext(os.path.basename(tp))[0],
                        "found": bool(best and best["conf"] >= threshold),
                        "match": best})
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {"ok": True, "tool": "tmatch", "version": VERSION, "mode": mode["mode"],
            "image": {"path": image_path, "w": image_bgr.shape[1], "h": image_bgr.shape[0]},
            "threshold": threshold, "elapsed_ms": ms, "count": len(results),
            "found_count": sum(1 for r in results if r["found"]),
            "results": results, "missing_templates": missing}


# ---------------------------------------------------------------------------
# 命令：collect / list / bench
# ---------------------------------------------------------------------------
def cmd_collect(args):
    img = imread_cn(args.target)
    if img is None:
        out({"ok": False, "error": f"截图读取失败: {args.target}"}, 2)
    x, y, w, h = [int(v) for v in args.rect.split(",")]
    ih, iw = img.shape[:2]
    if x < 0 or y < 0 or x + w > iw or y + h > ih:
        out({"ok": False, "error": f"矩形越界: 图像 {iw}x{ih}, rect=({x},{y},{w},{h})"}, 2)
    crop = img[y:y + h, x:x + w]
    os.makedirs(args.dir, exist_ok=True)
    out_path = os.path.join(args.dir, args.name if args.name.endswith(".png") else args.name + ".png")
    okenc, buf = cv2.imencode(".png", crop)     # 中文路径安全写图（EXP-049）
    if not okenc:
        out({"ok": False, "error": "模板PNG编码失败"}, 1)
    buf.tofile(out_path)
    out({"ok": True, "action": "collect", "name": os.path.splitext(os.path.basename(out_path))[0],
         "path": out_path, "rect": [x, y, w, h], "source": args.target,
         "hint": "用法: python tmatch.py find \"" + out_path + "\" <目标截图>"})


def cmd_list(args):
    os.makedirs(args.dir, exist_ok=True)
    items = []
    for fn in sorted(os.listdir(args.dir)):
        if fn.lower().endswith(".png"):
            p = os.path.join(args.dir, fn)
            img = imread_cn(p)
            items.append({"name": os.path.splitext(fn)[0], "path": p,
                          "w": img.shape[1] if img is not None else None,
                          "h": img.shape[0] if img is not None else None})
    out({"ok": True, "dir": args.dir, "count": len(items), "templates": items})


def cmd_bench(args):
    """GPU/CPU 同一负载各跑 loops 次（GPU 不可用时 CPU 单跑并说明）。"""
    img = imread_cn(args.target)
    tpl = imread_cn(args.template)
    if img is None or tpl is None:
        out({"ok": False, "error": "文件读取失败（--template/--target）"}, 2)
    image_g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    templ_g = cv2.cvtColor(cv2.resize(tpl, None, fx=1.0, fy=1.0), cv2.COLOR_BGR2GRAY).astype(np.float32)

    def bench(fn):
        ts = []
        for _ in range(args.loops):
            t0 = time.perf_counter()
            fn()
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        return {"loops": args.loops, "median_ms": round(ts[len(ts)//2], 2),
                "min_ms": round(ts[0], 2), "max_ms": round(ts[-1], 2)}

    result = {"ok": True, "action": "bench", "loops": args.loops,
              "image": {"w": img.shape[1], "h": img.shape[0]},
              "template": {"w": templ_g.shape[1], "h": templ_g.shape[0]}}
    detected = detect_mode()
    result["detected"] = detected
    if detected["mode"] == "gpu":
        result["gpu"] = bench(lambda: match_at_scale(image_g, templ_g))
    result["cpu"] = bench(lambda: cpu_match(image_g, templ_g))
    speedup = None
    if "gpu" in result:
        speedup = round(result["cpu"]["median_ms"] / max(result["gpu"]["median_ms"], 1e-6), 2)
        result["speedup_x"] = speedup
    result["conclusion"] = (f"模式={result['detected']['mode']}；CPU中位 {result['cpu']['median_ms']}ms"
                            + (f"；GPU中位 {result['gpu']['median_ms']}ms；加速 {speedup}x" if "gpu" in result
                               else "；GPU 不可用（" + result['detected']['reason'] + "）"))
    out(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def main():
    p = argparse.ArgumentParser(prog="tmatch.py", description="模板匹配工具（找无文字元素，GPU自动降级CPU）")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="单模板匹配")
    f.add_argument("template")
    f.add_argument("target")
    f.add_argument("--threshold", type=float, default=0.8)
    f.add_argument("--scales", default="0.8,1.0,1.2", help="逗号分隔多尺度")
    f.add_argument("--max-results", type=int, default=1, help="多实例枚举上限（巡回29 补强）")

    fa = sub.add_parser("findall", help="多模板批量匹配")
    fa.add_argument("target")
    fa.add_argument("templates", nargs="+")
    fa.add_argument("--threshold", type=float, default=0.8)
    fa.add_argument("--scales", default="0.8,1.0,1.2")
    fa.add_argument("--dir", default=TPL_DIR, help="模板相对名时查找的目录")

    c = sub.add_parser("collect", help="从截图裁剪存模板")
    c.add_argument("target")
    c.add_argument("--rect", required=True, help="x,y,w,h")
    c.add_argument("--name", required=True)
    c.add_argument("--dir", default=TPL_DIR)

    l = sub.add_parser("list", help="列出模板库")
    l.add_argument("--dir", default=TPL_DIR)

    b = sub.add_parser("bench", help="GPU/CPU 计时对比")
    b.add_argument("template")
    b.add_argument("target")
    b.add_argument("--loops", type=int, default=20)

    args = p.parse_args()
    mode = detect_mode()

    if args.cmd == "find":
        scales = [float(x) for x in args.scales.split(",")]
        r = find_all(args.template, args.target, args.threshold, scales, getattr(args, 'max_results', 1))
        r["mode_reason"] = mode["reason"]
        out(r, 0 if r.get("found") else 1)
    elif args.cmd == "findall":
        scales = [float(x) for x in args.scales.split(",")]
        r = find_templates(args.templates, args.target, args.threshold, scales, args.dir)
        out(r, 0 if r.get("ok") and r.get("found_count", 0) > 0 else 1)
    elif args.cmd == "collect":
        cmd_collect(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "bench":
        cmd_bench(args)


if __name__ == "__main__":
    main()

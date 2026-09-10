# -*- coding: utf-8 -*-
"""bench_ocr.py —— 复现 README 中的 OCR 速度数据（可自行验证）

    python benchmarks/bench_ocr.py

测什么：
  1) 冷启（含引擎加载）
  2) 同进程热调用（引擎复用）
  3) 内容缓存命中（子进程，同一画面重复查询）
  4) 区域限定（只 OCR 屏幕一块）
  5) 降采样 scale 的收益与代价（块数 = 召回率）

结论（本机 2560×1440 / 纯 CPU 实测）：
  - 引擎加载约 3.8s，是冷启耗时的大头 → 守护进程 / 同进程复用是第一优化项
  - OCR 有约 1.2s 的物理下限，缩到 800×600 也降不下去 → 参数调优到不了毫秒级
  - 所以毫秒级定位只能靠绕开 OCR（见 bench_tmatch.py）
"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import findtext as FT  # noqa: E402


def bench(label, fn, repeat=1):
    best, n = None, 0
    for _ in range(repeat):
        t0 = time.time()
        blocks = fn()
        ms = (time.time() - t0) * 1000
        n = len(blocks)
        best = ms if best is None else min(best, ms)
    print("  %-40s %8.1f ms   块数 %d" % (label, best, n))
    return best


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 74)
    print(" OCR 速度基准")
    print("=" * 74)

    try:
        arr, W, H, ox, oy = FT.grab_frame()
    except Exception as e:
        print("截屏失败（本基准需要真实屏幕）：%s" % e)
        print("提示：无桌面环境时请改用 --image 路径测图片识别耗时。")
        return 2
    print("屏幕 %dx%d  offset=(%d,%d)" % (W, H, ox, oy))
    print("-" * 74)

    cold = bench("① 冷启（含引擎加载）use_cls=False",
                 lambda: FT.ocr_frame(arr, use_cls=False))
    hot = bench("② 热调用（引擎复用）",
                lambda: FT.ocr_frame(arr, use_cls=False), repeat=3)
    print("     → 引擎加载开销约 %.1f s（≈ ① − ②）" % ((cold - hot) / 1000.0))

    for sc in (0.75, 0.5, 0.35):
        bench("③ 热调用 scale=%.2f" % sc,
              lambda s=sc: FT.ocr_frame(arr, scale=s, use_cls=False), repeat=2)

    for (rw, rh) in ((1600, 1000), (1200, 800), (800, 600)):
        x0, y0 = (W - rw) // 2, (H - rh) // 2
        sub = arr[y0:y0 + rh, x0:x0 + rw].copy()
        bench("④ 区域 %dx%d 热调用" % (rw, rh),
              lambda s=sub: FT.ocr_frame(s, use_cls=False), repeat=2)

    print("-" * 74)
    print(" ⑤ 子进程查询（含进程启动 + 引擎加载）")
    tmp = tempfile.mkdtemp(prefix="bench-ocr-")
    png = os.path.join(tmp, "screen.png")
    FT.save_png(arr, png)
    script = os.path.join(ROOT, "tools", "findtext.py")
    for label, extra in (("首次（真 OCR）", ["--no-cache"]),
                         ("再次（内容缓存命中）", [])):
        ts = []
        for _ in range(2):
            t0 = time.time()
            subprocess.run([sys.executable, script, "--image", png, "--all"] + extra,
                           capture_output=True, timeout=300)
            ts.append((time.time() - t0) * 1000)
        print("     %-28s %8.1f ms" % (label, min(ts)))

    print("-" * 74)
    print("如何真正提速：")
    print("  1. 守护进程：python tools/findtext.py --serve")
    print("     引擎常驻 + 进程开销归零，每次查询从秒级降到约 0.2s")
    print("  2. 别重复 OCR 同一画面：内容缓存默认开启（md5 命中即复用）")
    print("  3. 缩小 OCR 范围：只识别关心的区域")
    print("  4. 最快的是不 OCR：模板匹配在小子窗口内只要几毫秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())

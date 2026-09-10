# -*- coding: utf-8 -*-
"""bench_tmatch.py —— 复现「全屏匹配 vs 锚点窗口匹配」的速度差（可自行验证）

    python benchmarks/bench_tmatch.py

核心结论（本机 2560×1440 / 纯 CPU 实测）：
    全屏 2560×1440 匹配   ≈ 147 ms
    锚点窗口 300×300 匹配 ≈ 4 ms     → 净提速约 37 倍，坐标完全一致

为什么这是重点：OCR 有约 1.2s 的物理下限，而模板匹配在小子窗口里只要毫秒级。
所以「先用便宜的线索缩小范围，再做模板匹配」是达到毫秒级定位的关键路径。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import findtext as FT  # noqa: E402
import tmatch as TM  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 74)
    print(" 模板匹配速度基准：全屏 vs 锚点窗口")
    print("=" * 74)

    try:
        arr, W, H, ox, oy = FT.grab_frame()
    except Exception as e:
        print("截屏失败（本基准需要真实屏幕）：%s" % e)
        return 2
    print("屏幕 %dx%d" % (W, H))

    # 取一个锚点文字，并用它附近的图形当模板（必须是带边缘的模板，纯色块会误匹配）
    blocks = FT.ocr_frame(arr, use_cls=False)
    cands = [b for b in blocks
             if 2 <= len(b["text"].strip()) <= 10 and b["conf"] >= 0.9
             and (b["right"] - b["left"]) > 30 and (b["bottom"] - b["top"]) > 14]
    if not cands:
        print("屏幕上没找到合适的锚点文字，无法演示（换一个有文字的界面再试）。")
        return 1
    a = max(cands, key=lambda b: b["conf"])
    print("锚点文字: %r  bbox=[%d,%d,%d,%d] conf=%.3f"
          % (a["text"], a["left"], a["top"], a["right"], a["bottom"], a["conf"]))

    # 用锚点 bbox 本身裁一个模板（含边缘，能体现出模板匹配的真实耗时）
    pad = 1
    templ = arr[a["top"] + pad:a["bottom"] - pad, a["left"] + pad:a["right"] - pad].copy()
    g = templ.mean(axis=2)
    print("模板 %dx%d 灰度标准差 %.2f %s"
          % (templ.shape[1], templ.shape[0], g.std(),
             "（偏低，纯色模板会误匹配）" if g.std() < 8 else "（有边缘特征，可用）"))
    print("-" * 74)

    # A. 全屏匹配
    ts = []
    for _ in range(3):
        t0 = time.time()
        m_full = TM.find_one(arr, templ, threshold=0.8)
        ts.append((time.time() - t0) * 1000)
    full_ms = min(ts)
    print("A 全屏 %dx%d 匹配 : %8.1f ms   conf=%s"
          % (W, H, full_ms, (m_full or {}).get("conf")))
    if m_full:
        print("   绝对坐标: (%d, %d)" % (m_full["cx"], m_full["cy"]))

    # B. 锚点窗口匹配
    WIN = 300
    x0 = max(0, min(W - WIN, a["left"] - WIN // 2))
    y0 = max(0, min(H - WIN, a["top"] - WIN // 2))
    sub = arr[y0:y0 + WIN, x0:x0 + WIN].copy()
    ts = []
    for _ in range(5):
        t0 = time.time()
        m_win = TM.find_one(sub, templ, threshold=0.8)
        ts.append((time.time() - t0) * 1000)
    win_ms = min(ts)
    print("B 锚点窗口 %dx%d 匹配: %8.1f ms   conf=%s"
          % (WIN, WIN, win_ms, (m_win or {}).get("conf")))
    if m_win:
        abs_cx, abs_cy = m_win["cx"] + x0, m_win["cy"] + y0
        print("   窗口内 (%d, %d) + 窗口原点 (%d, %d) = 绝对 (%d, %d)"
              % (m_win["cx"], m_win["cy"], x0, y0, abs_cx, abs_cy))

    print("-" * 74)
    if m_full and m_win:
        d = (abs(abs_cx - m_full["cx"]), abs(abs_cy - m_full["cy"]))
        print("坐标一致性: 偏差 %s 像素 → %s"
              % (d, "完全一致（互为确认）" if max(d) <= 2 else "不一致，需排查"))
    if win_ms:
        print("净提速: %.1f 倍（%.1f ms → %.1f ms）" % (full_ms / win_ms, full_ms, win_ms))
        print("批量场景更能体现：连续匹配 20 次 → 全屏 %.1fs vs 窗口 %.2fs"
              % (full_ms * 20 / 1000.0, win_ms * 20 / 1000.0))
    print("-" * 74)
    print("实际使用建议：")
    print("  1. 能给 --region 就给（上层已知大概范围时，直接走最快路径）")
    print("  2. 否则给 --anchor（OCR 找一个必然存在的文字，再在它附近做模板匹配）")
    print("  3. 模板必须带边缘/纹理：纯色模板灰度方差接近 0，会产生满分误匹配")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""示例 3：Python 内嵌调用 —— 省掉进程启动开销，适合循环 / 批量

    python examples/03_python_api.py

与走 CLI 的区别（实测参考）：
  - 每次起一个新进程调 CLI：约 +120ms 固定开销，且 OCR 引擎每次都要重新加载（约 +3.8s）
  - 本示例在同进程内调用：引擎只加载一次，之后的查询只有推理耗时

所以：**偶尔一次性定位用 CLI 方便；循环/批量定位请用本方式。**
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import findtext as FT  # noqa: E402


def main():
    t0 = time.time()
    arr, w, h, ox, oy = FT.grab_frame()          # 截屏（只读，不抢焦点）
    shot_ms = (time.time() - t0) * 1000
    print("截屏 %dx%d  offset=(%d,%d)  %.0fms" % (w, h, ox, oy, shot_ms))

    t0 = time.time()
    blocks = FT.ocr_frame(arr, use_cls=False)    # OCR；引擎首次调用自动加载并复用
    first_ms = (time.time() - t0) * 1000
    print("OCR 第 1 次（含引擎加载）: %.0fms，识别到 %d 个文字块" % (first_ms, len(blocks)))

    t0 = time.time()
    FT.ocr_frame(arr, use_cls=False)
    print("OCR 第 2 次（引擎已加载）: %.0fms" % ((time.time() - t0) * 1000))

    print("-" * 56)
    words = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not words:
        # 没给词：把屏幕上真实识别到的文字列出来（顺序按从上到下、从左到右）
        print("屏幕上识别到的文字块（前 12 个）——每一行都带可点击中心坐标：")
        for b in sorted(blocks, key=lambda b: (b["top"], b["left"]))[:12]:
            cx = (b["left"] + b["right"]) // 2
            cy = (b["top"] + b["bottom"]) // 2
            text = b["text"][:22]
            print("  %-24s center=(%4d,%4d)  conf=%.3f" % (text, cx, cy, b["conf"]))
        print()
        print('搜你自己的词：python examples/03_python_api.py "要搜的词"')
    for word in words:
        hits = FT.match_blocks(blocks, word, mode="contains")
        if not hits:
            print("%-14r → 未命中" % word)
            continue
        b = hits[0]
        cx = (b["left"] + b["right"]) // 2
        cy = (b["top"] + b["bottom"]) // 2
        print("%-14r → bbox=[%d,%d,%d,%d] center=(%d,%d) conf=%.4f  候选 %d 处"
              % (word, b["left"], b["top"], b["right"], b["bottom"], cx, cy, b["conf"], len(hits)))
        if len(hits) > 1:
            print("%-14s  注意：同屏有 %d 处同名，直接取第一个可能点错——"
                  "这种情况请用模板匹配二次确认。" % ("", len(hits)))

    print("-" * 56)
    print("要点击：用 ctypes 设置光标位置后发鼠标事件（参见 tools/vlocate.py 的 click_at）")
    print("要提速：启动守护进程 python tools/findtext.py --serve（每次查询从约 7s 降到约 0.2s）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

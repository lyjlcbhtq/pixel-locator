# -*- coding: utf-8 -*-
"""示例 1：OCR 找字 —— 拿到屏幕上某个词的像素级坐标

    python examples/01_locate_text.py                  # 用仓库自带素材
    python examples/01_locate_text.py "发送" --live     # 对当前屏幕实时定位

输出的是**屏幕物理像素**坐标。DPI 缩放不是 100% 时，逻辑坐标 = 物理坐标 / dpi_scale
（结果里的 screen.dpi_scale 就是当前缩放比）。
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    word = args[0] if args else "像素级定位"
    live = "--live" in sys.argv

    cmd = [PY, os.path.join(ROOT, "tools", "findtext.py")] + ([] if live else
          ["--image", os.path.join(ROOT, "fixtures", "sample.png")])
    cmd += ["--query", word]

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not r.stdout.strip():
        print("调用失败：", r.stderr[:400])
        return 2
    d = json.loads(r.stdout)

    scr = d.get("screen", {})
    print("画面 %dx%d  dpi_scale=%s  offset=%s"
          % (scr.get("width", 0), scr.get("height", 0),
             scr.get("dpi_scale"), scr.get("offset")))
    print("OCR 共识别到 %d 个文字块" % d.get("blocks_found", 0))
    if d.get("cached"):
        print("（结果来自内容缓存，cache=%s）" % d.get("cache"))
    print("-" * 56)

    any_found = False
    for res in d.get("results", []):
        for m in res.get("matches", []):
            any_found = True
            x0, y0, x1, y1 = m["bbox"]
            print("命中 %r" % m["text"])
            print("  像素级 bbox : [%d, %d, %d, %d]" % (x0, y0, x1, y1))
            print("  可点击中心  : (%d, %d)" % (m["x"], m["y"]))
            print("  置信度      : %.4f" % m["conf"])
    if not any_found:
        print("没有找到 %r —— OCR 没认出这个词，或它不在画面上。" % word)
        return 1
    print("-" * 56)
    print("把上面的中心坐标交给点击函数即可；但若同屏有多个同名文字，")
    print("请改用 examples/02_hybrid_locate.py 的锚点+模板方案做二次确认。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

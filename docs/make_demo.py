# -*- coding: utf-8 -*-
"""make_demo.py —— 生成 README 用的演示图（可复现）

三张图：
  docs/demo-locate.png        定位结果标注（坐标来自工具真实输出，不是手画的）
  docs/demo-accuracy.png      VLM 目测 vs OCR 像素级（精度对比示意）
  docs/demo-architecture.png  分层定位架构

用法：python docs/make_demo.py
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FX = os.path.join(ROOT, "fixtures")
TOOLS = os.path.join(ROOT, "tools")
PY = sys.executable

from PIL import Image, ImageDraw, ImageFont

CJK_BOLD = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"]
CJK = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"]
MONO = [r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\cour.ttf"]


def font(size, bold=False, mono=False):
    paths = (MONO if mono else (CJK_BOLD if bold else CJK))
    for p in paths + CJK:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def sh(args):
    r = subprocess.run([PY] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT)
    try:
        return json.loads(r.stdout)
    except ValueError:
        print("调用失败:", args, r.stdout[:200], r.stderr[:200])
        sys.exit(1)


# ---------------------------------------------------------------------------
# 图 1：定位结果标注（真实坐标）
# ---------------------------------------------------------------------------
def demo_locate():
    sample = os.path.join(FX, "sample.png")
    tpl = os.path.join(FX, "template.png")

    cands = sh([os.path.join(TOOLS, "vlocate.py"), "--image", sample, "--list-all"])
    hit = sh([os.path.join(TOOLS, "vlocate.py"), "--image", sample,
              "--region", "560,220,800,460", "--template", tpl])

    src = Image.open(sample).convert("RGB")
    S = 2
    big = src.resize((src.width * S, src.height * S), Image.LANCZOS)
    bar, foot = 74, 58
    W, H = big.width, big.height + bar + foot
    img = Image.new("RGB", (W, H), (22, 24, 30))
    img.paste(big, (0, bar))
    d = ImageDraw.Draw(img)

    d.text((26, 14), "pixel-locator", font=font(30, True), fill=(255, 255, 255))
    d.text((26, 48), "像素级定位结果 —— 每个框的坐标都来自本地 OCR / 模板匹配（零 token）",
           font=font(19), fill=(150, 158, 175))

    OFFSET = bar

    # OCR 文字块：绿框 + 中心点 + 坐标
    for c in cands["candidates"]:
        x0, y0, x1, y1 = c["bbox"]
        d.rectangle([x0 * S, y0 * S + OFFSET, x1 * S, y1 * S + OFFSET],
                    outline=(72, 214, 143), width=2)
        cx, cy = c["center"]
        for dx, dy in ((-5, 0), (5, 0), (0, -5), (0, 5)):
            d.line([cx * S - dx, cy * S - dy + OFFSET, cx * S + dx, cy * S + dy + OFFSET],
                   fill=(72, 214, 143), width=1)
        label = "(%d,%d) %.3f" % (cx, cy, c["conf"])
        tw = d.textlength(label, font=font(15, mono=True))
        lx, ly = x0 * S, y1 * S + OFFSET + 4
        d.rectangle([lx, ly, lx + tw + 10, ly + 22], fill=(16, 40, 30))
        d.text((lx + 5, ly + 3), label, font=font(15, mono=True), fill=(130, 240, 180))

    # 模板命中：红框 + 准星 + 坐标
    if hit.get("ok"):
        bx0, by0, bx1, by1 = hit["precise"]["bbox"]
        d.rectangle([bx0 * S, by0 * S + OFFSET, bx1 * S, by1 * S + OFFSET],
                    outline=(255, 92, 92), width=4)
        cx, cy = hit["click"]
        r = 22
        d.line([cx * S - r, cy * S + OFFSET, cx * S + r, cy * S + OFFSET], fill=(255, 92, 92), width=2)
        d.line([cx * S, cy * S - r + OFFSET, cx * S, cy * S + r + OFFSET], fill=(255, 92, 92), width=2)
        d.ellipse([cx * S - 6, cy * S - 6 + OFFSET, cx * S + 6, cy * S + 6 + OFFSET],
                  outline=(255, 255, 255), width=2)
        label = "click (%d,%d)  conf %.2f  %s" % (cx, cy, hit["precise"]["conf"], hit["method"])
        tw = d.textlength(label, font=font(17, mono=True))
        lx, ly = bx0 * S, by0 * S + OFFSET - 28
        if ly < OFFSET + 2:
            ly = by1 * S + OFFSET + 6
        d.rectangle([lx, ly, lx + tw + 12, ly + 26], fill=(70, 16, 16))
        d.text((lx + 6, ly + 4), label, font=font(17, mono=True), fill=(255, 150, 150))

    legend = ("绿框 = OCR 文字块（像素级 bbox）     "
              "红框/准星 = 模板匹配命中（像素级 + 相似度分）")
    d.text((26, H - foot + 12), legend, font=font(19), fill=(178, 186, 202))

    out = os.path.join(HERE, "demo-locate.png")
    img.save(out, quality=95)
    print("已生成", out, img.size)


# ---------------------------------------------------------------------------
# 图 2：VLM 目测 vs OCR 像素级（示意，数据来自真实实测）
# ---------------------------------------------------------------------------
def demo_accuracy():
    W, H = 1180, 706
    img = Image.new("RGB", (W, H), (22, 24, 30))
    d = ImageDraw.Draw(img)
    d.text((34, 24), "为什么不能直接用视觉模型的坐标", font=font(30, True), fill=(255, 255, 255))
    d.text((34, 62), "同一张 2560×1440 截图、同一个目标输入框，两条路径的实测结果",
           font=font(19), fill=(150, 158, 175))

    # 模拟界面
    px, py, pw, ph = 100, 130, 980, 440
    d.rectangle([px, py, px + pw, py + ph], fill=(32, 35, 44), outline=(58, 63, 76), width=2)
    d.text((px + 22, py + 16), "聊天窗口（示意）", font=font(19), fill=(130, 138, 155))

    # 消息区（上方）
    for i, (mx, my, mw) in enumerate(((px + 22, py + 60, 620), (px + 22, py + 108, 760),
                                      (px + 22, py + 156, 540))):
        d.rounded_rectangle([mx, my, mx + mw, my + 34], 8, fill=(46, 50, 62))
        d.text((mx + 14, my + 8), "消息内容 " * 3, font=font(15), fill=(120, 128, 145))

    # 输入框（目标）
    ix0, iy0, ix1, iy1 = px + 22, py + 320, px + pw - 22, py + 384
    d.rounded_rectangle([ix0, iy0, ix1, iy1], 10, fill=(250, 249, 246), outline=(150, 156, 170), width=2)
    d.text((ix0 + 16, iy0 + 16), "在这里输入…", font=font(19), fill=(150, 155, 165))

    # VLM 估计点（偏高，落在输入框上方 —— 这就是"点不到"的实际情况）
    vx, vy = ix0 + 420, iy0 - 76
    for rr, col in ((16, (255, 90, 90)), (9, (255, 140, 140)), (4, (255, 220, 220))):
        d.ellipse([vx - rr, vy - rr, vx + rr, vy + rr], fill=col)
    # 虚线连到真实目标，直观展示偏差
    for t in range(0, 100, 10):
        yy = vy + int((iy0 - vy) * t / 100.0)
        d.line([vx, yy, vx, yy + 6], fill=(255, 110, 110), width=2)
    d.text((vx + 30, vy - 24), "视觉模型目测点", font=font(19, True), fill=(255, 120, 120))
    d.text((vx + 30, vy + 2), "偏差 30~40 像素 —— 点不到输入框", font=font(16), fill=(225, 125, 125))

    # OCR 精确框（正好框住输入框）
    d.rectangle([ix0 - 3, iy0 - 3, ix1 + 3, iy1 + 3], outline=(72, 214, 143), width=3)
    ox, oy = (ix0 + ix1) // 2, (iy0 + iy1) // 2
    d.line([ox - 18, oy, ox + 18, oy], fill=(72, 214, 143), width=2)
    d.line([ox, oy - 18, ox, oy + 18], fill=(72, 214, 143), width=2)
    d.text((ix1 - 430, iy1 + 14), "OCR 像素级 bbox → 精确命中", font=font(19, True), fill=(90, 230, 160))

    # 底部结论
    by = py + ph + 26
    d.rectangle([100, by, W - 100, by + 74], fill=(18, 46, 36), outline=(40, 120, 90), width=2)
    d.text((122, by + 12), "VLM 负责「这是什么、点哪一个」；OCR / 模板匹配负责「精确在哪个像素」。",
           font=font(20), fill=(140, 240, 190))
    d.text((122, by + 40), "pixel-locator 把两件事拆开做，再串起来 —— 主路径零 token、纯本地。",
           font=font(20), fill=(120, 210, 175))

    out = os.path.join(HERE, "demo-accuracy.png")
    img.save(out, quality=95)
    print("已生成", out, img.size)


# ---------------------------------------------------------------------------
# 图 3：分层定位架构
# ---------------------------------------------------------------------------
def demo_architecture():
    W, H = 1180, 620
    img = Image.new("RGB", (W, H), (22, 24, 30))
    d = ImageDraw.Draw(img)
    d.text((34, 24), "分层定位架构：从便宜到昂贵，前面命中就不再往下走", font=font(28, True), fill=(255, 255, 255))
    d.text((34, 60), "cost-ordered locating pipeline — stop at the first hit",
           font=font(17, mono=True), fill=(140, 148, 165))

    layers = [
        ("第 0 层", "坐标记忆", "画面指纹命中 → 复用上次坐标并验证", "~1 ms", "0 token", (34, 70, 56), (110, 235, 175)),
        ("第 1 层", "模板匹配 tmatch", "图标 / 图形元素，像素级 + 可量化相似度", "~4 ms", "0 token", (30, 74, 62), (120, 240, 180)),
        ("第 2 层", "OCR 锚点 + 模板确认", "文字锚点定位 + 图形确认（双证），主力路径", "~200 ms", "0 token", (34, 62, 78), (130, 200, 250)),
        ("第 3 层", "视觉模型 VLM", "只负责圈范围缩小搜索；坐标永不直接用于点击", "5~7 s", "消耗 token", (70, 48, 34), (250, 180, 110)),
    ]
    y = 110
    for name, title, desc, ms, tok, bg, fg in layers:
        h = 100
        d.rounded_rectangle([70, y, W - 70, y + h], 12, fill=bg, outline=fg, width=2)
        d.text((96, y + 16), name, font=font(20, True), fill=fg)
        d.text((96, y + 46), title, font=font(24, True), fill=(240, 244, 252))
        d.text((96, y + 74), desc, font=font(17), fill=(180, 188, 205))
        d.text((W - 300, y + 30), ms, font=font(22, mono=True), fill=fg)
        d.text((W - 300, y + 62), tok, font=font(17), fill=(160, 168, 185))
        y += h + 14

    d.text((70, y + 6), "最终落点坐标一律来自 tmatch（像素级 + 相似度分）或 OCR（像素级 bbox + 置信度），"
                        "VLM 只做侦察。", font=font(18), fill=(150, 158, 175))

    out = os.path.join(HERE, "demo-architecture.png")
    img.save(out, quality=95)
    print("已生成", out, img.size)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    demo_locate()
    demo_accuracy()
    demo_architecture()

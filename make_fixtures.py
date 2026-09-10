# -*- coding: utf-8 -*-
"""生成冒烟测试自带素材（离线可跑，不依赖任何外部工程文件）
产物：
  fixtures/sample.png     —— 含文字与图形的测试图（findtext OCR / tmatch 模板匹配用）
  fixtures/template.png   —— 从 sample.png 裁出的色块（tmatch 模板）
  fixtures/sample.txt     —— pipeline 输入样例
"""
import os, sys
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures")
os.makedirs(FX, exist_ok=True)

W, H = 900, 460
img = Image.new("RGB", (W, H), (250, 248, 242))
d = ImageDraw.Draw(img)


def font(sz):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


# 文字区（OCR 目标）
d.text((60, 50), "工具包冒烟测试", font=font(48), fill=(28, 28, 30))
d.text((60, 130), "OCR TARGET 2026", font=font(34), fill=(40, 40, 44))
d.text((60, 190), "像素级定位", font=font(30), fill=(60, 60, 64))

# 图形区（模板匹配目标）：红色方块 —— 位置固定便于裁模板
RED_BOX = (640, 300, 720, 380)
d.rectangle(RED_BOX, fill=(190, 45, 40))
d.rectangle((640, 300, 720, 380), outline=(120, 20, 18), width=3)
# 干扰图形（不同形状/颜色，验证匹配不会误报）
d.ellipse([60, 290, 150, 380], fill=(40, 80, 150))
d.polygon([(240, 380), (300, 290), (360, 380)], fill=(50, 120, 60))

sample = os.path.join(FX, "sample.png")
img.save(sample)

# 模板：裁"含边框"的区域 —— 必须是带边缘/纹理的模板。
# 反面教材：纯色块在灰度图上无方差，归一化相关会给出满分误匹配。
tpl = img.crop((634, 294, 726, 386))
tpl.save(os.path.join(FX, "template.png"))

# pipeline 输入样例
with open(os.path.join(FX, "sample.txt"), "w", encoding="utf-8") as f:
    f.write("""工具包冒烟测试素材

这是一段用于流水线测试的文本。它包含若干句子，用来验证
pipeline 工具能否正确读取、切分与处理纯文本输入。

第二段：像素级定位是这套工具的核心能力。OCR 给出精确坐标，
视觉模型负责理解语义，两者互补而不是互相替代。
""")

print("已生成素材：")
for n in ("sample.png", "template.png", "sample.txt"):
    p = os.path.join(FX, n)
    print("  %-14s %6d bytes" % (n, os.path.getsize(p)))

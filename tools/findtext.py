#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
findtext.py — 全图 OCR 文字定位工具 v4.0
=========================================

用途：AI 操作图形界面时，"截屏 → 识别所有文字 → 根据目标文字拿到屏幕坐标"。

v4.1 提速要点（详见 提速报告2.md）：
  * 守护进程：`--serve` 引擎常驻（预热一次），CLI 自动复用（进程启动/导入/初始化/预热全部归零）；
  * 模糊指纹缓存：64×36 灰度指纹 MAD≤4 视为同一画面——时钟/光标/流式文本等动态屏也可命中；
  * 窗口激活轮询调优（已前台零等待）；内存级区域缓存（守护模式重复区域秒回）。

v4.0 提速要点（详见 提速报告.md）：
  * 截屏不再落盘 PNG：mss 原始帧（ndarray）直接送 OCR，省去编码与读盘；
  * 截图默认 use_cls=False（屏幕文字无需方向分类，实测推理 2.67s→2.24s，块保留率 98.9%），--cls 可开回；
  * md5 内容缓存：同一画面（像素级一致）的重复查询直接复用上次结果，find→click→at 场景秒回；
  * --bench：标准化性能基准，写入 基准测试.json。

坐标约定：
  * 默认输出"物理像素坐标"，原点 = 主显示器左上角，与全屏截图 1:1 对应。
  * 若系统开了 DPI 缩放（如 150%），额外输出 logical 坐标（逻辑像素）。

示例：
  python findtext.py --selftest                     # 首次使用：环境自检
  python findtext.py --all                          # 截屏并列出全部文字块
  python findtext.py --query "发送"                  # 截屏并找一个词
  python findtext.py --query "发送,打开"             # 批量找
  python findtext.py --query "发送" --verify         # 找到并自证
  python findtext.py --at 1200,800                  # 坐标反查
  python findtext.py --near 1200,800 --radius 150   # 坐标邻域
  python findtext.py --near-text "用户名"            # 文字锚点邻域
  python findtext.py --in 0,0,400,300               # 矩形区域
  python findtext.py --lines --all                  # 按视觉行输出
  python findtext.py --all --out 快照.json           # 先看后挑两步流
  python findtext.py --pick 3,7 --from 快照.json     # 按编号挑
  python findtext.py --window "记事本" --query "保存" # 窗口内找字
  python findtext.py --bench                        # 性能基准

退出码：0=全部命中；1=执行成功但有目标未命中；2=出错
"""

import argparse
import ctypes
import subprocess
import ctypes.wintypes
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata

# ---------------------------------------------------------------------------
# 全局：DPI 感知。必须最先设置，保证截屏为完整物理分辨率、坐标为物理像素。
# ---------------------------------------------------------------------------
def set_dpi_aware():
  if sys.platform == "win32":
    try:
      ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
      try:
        ctypes.windll.user32.SetProcessDPIAware()
      except Exception:
        pass


set_dpi_aware()

DEFAULT_SAVE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "4.1"
CACHE_FILE = os.path.join(DEFAULT_SAVE_DIR, "_cache.json")
MEM_CACHE = {}  # 进程内模糊指纹缓存（守护模式下收益最大；键=区域）

_engine = None


def get_engine():
  """惰性加载 RapidOCR（首次约 0.4s，之后进程内复用）。"""
  global _engine
  if _engine is None:
    from rapidocr_onnxruntime import RapidOCR

    _engine = RapidOCR()
  return _engine


# ---------------------------------------------------------------------------
# 截屏：原始帧（不落盘）
# ---------------------------------------------------------------------------
def grab_frame(region=None, monitor="primary"):
  """截取屏幕原始帧。

  :returns: (bgr_ndarray[H,W,3], width, height, offset_x, offset_y)
            offset 为该帧左上角在屏幕坐标系中的绝对坐标（主显示器恒为 0,0）
  """
  import mss
  import numpy as np

  factory = getattr(mss, "MSS", mss.mss)  # 兼容 mss>=10（旧名弃用）
  with factory() as sct:
    if region is not None:
      x, y, w, h = region
      mon = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
    elif isinstance(monitor, int):
      mon = sct.monitors[monitor]
    elif monitor == "all":
      mon = sct.monitors[0]
    else:
      mon = sct.monitors[1]  # mss: 1 号即主显示器
    shot = sct.grab(mon)
    import cv2

    arr = cv2.cvtColor(
      np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3),
      cv2.COLOR_RGB2BGR,
    ).copy()
    return arr, shot.width, shot.height, mon["left"], mon["top"]


def save_png(arr, path):
  """ndarray(BGR) → PNG 文件（兼容中文路径）。"""
  import cv2

  ok, buf = cv2.imencode(".png", arr)
  if ok:
    with open(path, "wb") as f:
      f.write(buf.tobytes())
  return ok


# ---------------------------------------------------------------------------
# OCR（ndarray / 文件均可）
# ---------------------------------------------------------------------------
def ocr_frame(arr, scale=1.0, contrast=False, use_cls=False):
  """对 BGR ndarray 做 OCR，返回文字块列表（坐标已在帧内）。

  scale < 1.0 先等比缩小（提速）；contrast=True 自动对比度增强；
  use_cls 默认 False（屏幕文字不需要方向分类）。
  每块：{"text", "conf", "left", "top", "right", "bottom"}
  """
  import cv2

  work = arr
  if contrast:
    lo, hi = float(work.min()), float(work.max())
    if hi > lo:
      work = ((work.astype("float32") - lo) / (hi - lo) * 255).astype("uint8")
  if scale and scale != 1.0:
    work = cv2.resize(work, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

  result, elapse = get_engine()(work, use_cls=use_cls)

  items = []
  if result is None:
    return items
  if isinstance(result, tuple) and len(result) >= 1:
    items = result[0] or []
    if items and hasattr(items, "boxes"):
      items = list(zip(items.boxes, items.txts, items.scores))
  elif hasattr(result, "boxes"):
    items = list(zip(result.boxes, result.txts, result.scores))
  else:
    items = list(result)

  inv = 1.0 / scale if scale else 1.0
  blocks = []
  for it in items:
    if len(it) < 3:
      continue
    box, text, score = it[0], it[1], it[2]
    xs = [float(p[0]) * inv for p in box]
    ys = [float(p[1]) * inv for p in box]
    blocks.append(
      {
        "text": str(text),
        "conf": round(float(score), 4),
        "left": round(min(xs)),
        "top": round(min(ys)),
        "right": round(max(xs)),
        "bottom": round(max(ys)),
      }
    )
  return blocks


def ocr_image_file(image_path, scale=1.0, contrast=False, use_cls=True):
  """对图片文件做 OCR（用户提供的图片可能带旋转，默认 use_cls=True）。"""
  import cv2
  import numpy as np

  arr = cv2.imdecode(np.fromfile(image_path, dtype="uint8"), cv2.IMREAD_COLOR)
  if arr is None:
    raise FileNotFoundError(f"无法读取图片: {image_path}")
  return ocr_frame(arr, scale=scale, contrast=contrast, use_cls=use_cls)


# ---------------------------------------------------------------------------
# 窗口定位
# ---------------------------------------------------------------------------
def get_window_rect(title_sub):
  """按标题模糊匹配可见窗口，返回 (hwnd, (left, top, right, bottom), 标题)。"""
  user32 = ctypes.windll.user32
  result = []

  @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
  def cb(hwnd, _):
    if not user32.IsWindowVisible(hwnd):
      return True
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    if title_sub in buf.value and buf.value.strip():
      r = ctypes.wintypes.RECT()
      if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        result.append((hwnd, (r.left, r.top, r.right, r.bottom), buf.value))
        return False
    return True

  user32.EnumWindows(cb, 0)
  return result[0] if result else None


def activate_window(hwnd):
  """把窗口带到前台（轮询确认，最多约 0.5s），返回是否成功。避免截到遮挡窗口的像素。"""
  import time as _t

  user32 = ctypes.windll.user32
  if user32.GetForegroundWindow() == hwnd:
    return True  # 已在前台：零等待
  user32.ShowWindow(hwnd, 9)  # SW_RESTORE
  t0 = _t.time()
  while _t.time() - t0 < 0.3:
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
      return True
    _t.sleep(0.06)
  user32.keybd_event(0x12, 0, 0, 0)   # ALT 技巧解锁前台限制
  user32.SetForegroundWindow(hwnd)
  user32.keybd_event(0x12, 0, 2, 0)
  t0 = _t.time()
  while _t.time() - t0 < 0.2:
    if user32.GetForegroundWindow() == hwnd:
      return True
    _t.sleep(0.05)
  return user32.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------------------
# 文本归一化与匹配
# ---------------------------------------------------------------------------
def norm_text(s):
  """归一化：全角转半角、去所有空白、转小写。缓解 OCR 空格/全半角抖动。"""
  s = unicodedata.normalize("NFKC", str(s))
  s = re.sub(r"\s+", "", s)
  return s.lower()


_CONFUSION = str.maketrans({"o": "0", "l": "1", "i": "1", "O": "0", "L": "1", "I": "1"})


def fold_confusion(s):
  return s.translate(_CONFUSION)


def match_blocks(blocks, query, mode="contains", fuzzy_threshold=0.7):
  """按匹配模式筛选文字块。mode: exact | contains | fuzzy"""
  nq = norm_text(query)
  fq = fold_confusion(nq)
  hits = []
  for b in blocks:
    nt = norm_text(b["text"])
    if not nt:
      continue
    if mode == "exact":
      ok = nt == nq
    elif mode == "fuzzy":
      ft = fold_confusion(nt)
      ok = (
        nq in nt
        or ft == fq
        or fq in ft
        or difflib.SequenceMatcher(None, nq, nt).ratio() >= fuzzy_threshold
      )
    else:  # contains
      ok = nq in nt
    if ok:
      hits.append(b)
  return hits


# ---------------------------------------------------------------------------
# 编号 / 视觉行 / 空间查询
# ---------------------------------------------------------------------------
def cluster_lines(blocks):
  """按垂直重叠把文字块聚类成"视觉行"，行内按左→右排序，行间按上→下排序。"""
  kept = sorted(blocks, key=lambda b: b["top"])
  lines = []
  for b in kept:
    placed = False
    for line in lines:
      ref = line[0]
      overlap = min(ref["bottom"], b["bottom"]) - max(ref["top"], b["top"])
      min_h = min(ref["bottom"] - ref["top"], b["bottom"] - b["top"])
      if min_h > 0 and overlap >= 0.5 * min_h:
        line.append(b)
        placed = True
        break
    if not placed:
      lines.append([b])
  lines.sort(key=lambda line: min(b["top"] for b in line))
  for line in lines:
    line.sort(key=lambda b: b["left"])
  return lines


def number_blocks(blocks, min_conf=0.0):
  """按阅读顺序过滤并编号（no 从 1 起）。视觉行聚类保证同行错落元素编号稳定。"""
  ordered = []
  for line in cluster_lines([b for b in blocks if b["conf"] >= min_conf]):
    ordered.extend(line)
  for i, b in enumerate(ordered, 1):
    b["no"] = i
  return ordered


def conf_grade(conf):
  return "high" if conf >= 0.9 else ("mid" if conf >= 0.7 else "low")


def frame_sig(arr):
  """灰度指纹（128×72 int16）：单元格约 20×20 物理像素，
  对滚动/位移敏感（配合双指标防误命中），对时钟/光标级微变容忍。"""
  import cv2

  g = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
  return cv2.resize(g, (128, 72), interpolation=cv2.INTER_AREA).astype("int16")


def sig_diff_stats(a, b):
  """指纹差异双指标：MAD（平均绝对差）与 变化单元格占比（|差|>25 的格子比例）。"""
  import numpy as np

  d = np.abs(np.asarray(a, dtype="int16") - np.asarray(b, dtype="int16"))
  return float(d.mean()), float((d > 25).mean())


def sig_same(a, b):
  """双指标判定同一画面：MAD≤4 且 变化占比≤8%。
  - 时钟/光标级微变：MAD≈0.03、占比<2% → 命中；
  - 滚动/位移级变化：MAD 达标但占比超限 → 不命中（保证坐标精度优先）。"""
  mad, frac = sig_diff_stats(a, b)
  return mad <= 4.0 and frac <= 0.08


# 兼容旧调用
def sig_diff(a, b):
  return sig_diff_stats(a, b)[0]


SIG_DIFF_THRESHOLD = 4.0


def block_public(b):
  return {
    "text": b["text"],
    "conf": b["conf"],
    "bbox": [b["left"], b["top"], b["right"], b["bottom"]],
    "center": [b["cx"], b["cy"]],
  }


def block_to_match(b, dpi_scale):
  """文字块 → 匹配结果。块坐标须已是屏幕绝对坐标；x/y 为中心点（可直接点击）。"""
  m = {
    "text": b["text"],
    "x": b["cx"],
    "y": b["cy"],
    "w": b["right"] - b["left"],
    "h": b["bottom"] - b["top"],
    "conf": b["conf"],
    "bbox": [b["left"], b["top"], b["right"], b["bottom"]],
  }
  if dpi_scale and dpi_scale != 1.0:
    m["logical"] = [round(m["x"] / dpi_scale), round(m["y"] / dpi_scale)]
  return m


def hit_test(blocks, x, y):
  """坐标 → 文字：返回 (hit 或 None, (nearest块, 距离) 或 None)。"""
  for b in blocks:
    if b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]:
      return b, None
  best, best_d = None, None
  for b in blocks:
    d = ((b["cx"] - x) ** 2 + (b["cy"] - y) ** 2) ** 0.5
    if best_d is None or d < best_d:
      best, best_d = b, d
  return None, (best, round(best_d)) if best else None


def near_query(blocks, x, y, radius):
  """半径内文字块，按中心距离升序。点在块内记 0。"""
  out = []
  for b in blocks:
    inside = b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]
    d = 0 if inside else ((b["cx"] - x) ** 2 + (b["cy"] - y) ** 2) ** 0.5
    if d <= radius:
      item = block_public(b)
      item["distance"] = round(d)
      item["point_inside"] = inside
      out.append(item)
  return sorted(out, key=lambda i: i["distance"])


def in_rect_query(blocks, x, y, w, h):
  """中心落在矩形内的文字块，按阅读顺序排序。"""
  out = []
  for b in blocks:
    if x <= b["cx"] <= x + w and y <= b["cy"] <= y + h:
      out.append(block_public(b))
  return sorted(out, key=lambda i: (i["bbox"][1], i["bbox"][0]))


def verify_frame(arr, bbox_local, query):
  """自证找到：从帧中裁剪目标周围（必要时放大）重 OCR，检查目标文字是否仍在。"""
  import cv2

  l, t, r, btm = bbox_local
  pad_x = max(24, (r - l) // 3)
  pad_y = max(14, (btm - t) // 2)
  H, W = arr.shape[:2]
  x0, y0 = max(0, l - pad_x), max(0, t - pad_y)
  x1, y1 = min(W, r + pad_x), min(H, btm + pad_y)
  crop = arr[y0:y1, x0:x1]
  if crop.shape[1] < 400:
    crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
  blocks = ocr_frame(crop)
  joined = norm_text("".join(b["text"] for b in blocks))
  nq = norm_text(query)
  fq = fold_confusion(nq)
  ok = nq in joined or fq in fold_confusion(joined)
  return {"verified": bool(ok), "crop_text": "".join(b["text"] for b in blocks)[:120]}


def diff_snapshots(a_blocks, b_blocks, move_threshold=25):
  """对比两张 --all 快照：报告消失/新增/移动的文字块。"""

  def norm_list(blocks):
    out = []
    for b in blocks:
      out.append({"text": b["text"], "norm": norm_text(b["text"]),
                  "center": b.get("center", [(b["bbox"][0] + b["bbox"][2]) // 2,
                                             (b["bbox"][1] + b["bbox"][3]) // 2]),
                  "no": b.get("no", 0)})
    return out

  A, B = norm_list(a_blocks), norm_list(b_blocks)
  used_b = set()
  moved, removed = [], []
  for a in A:
    hit = None
    for j, b in enumerate(B):
      if j in used_b:
        continue
      if b["norm"] == a["norm"] or (b["norm"] and (a["norm"] in b["norm"] or b["norm"] in a["norm"])):
        hit = j
        break
    if hit is None:
      removed.append({"text": a["text"], "center": a["center"], "no": a["no"]})
    else:
      used_b.add(hit)
      dist = ((B[hit]["center"][0] - a["center"][0]) ** 2 +
              (B[hit]["center"][1] - a["center"][1]) ** 2) ** 0.5
      if dist > move_threshold:
        moved.append({"text": a["text"], "from": a["center"], "to": B[hit]["center"],
                      "distance": round(dist)})
  added = [{"text": b["text"], "center": b["center"], "no": b["no"]}
           for j, b in enumerate(B) if j not in used_b]
  return {"added": added, "removed": removed, "moved": moved,
          "changed": bool(added or removed or moved),
          "summary": f"新增{len(added)} 消失{len(removed)} 移动{len(moved)}"}


# ---------------------------------------------------------------------------
# 内容缓存（md5）：同一画面像素级一致 → 复用上次 OCR 块，find→click→at 秒回
# ---------------------------------------------------------------------------
def cache_find(arr, sig, md5, region_key):
  """文件缓存查找：精确 md5 或 模糊指纹（同区域 + MAD≤阈值）。"""
  try:
    c = json.load(open(CACHE_FILE, encoding="utf-8"))
  except Exception:
    return None
  if c.get("region_key") != region_key:
    return None
  if c.get("md5") == md5:
    c["fuzzy"] = False
    return c
  sig_old = c.get("sig")
  if sig_old and sig_same(sig_old, sig):
    c["fuzzy"] = True
    return c
  return None


def cache_store(md5, sig, blocks, image_path, region_key=None):
  try:
    json.dump({"md5": md5, "sig": sig.tolist() if hasattr(sig, "tolist") else sig,
               "region_key": region_key, "saved_at": time.time(),
               "image": image_path, "blocks": blocks},
              open(CACHE_FILE, "w", encoding="utf-8"))
  except Exception:
    pass


# ---------------------------------------------------------------------------
# 同行合并
# ---------------------------------------------------------------------------
def merge_lines(blocks):
  """同行相邻文字块合并（间距/重叠启发式），修复目标文字被 OCR 拆成两块的问题。"""
  blocks_sorted = sorted(blocks, key=lambda b: (b["top"], b["left"]))
  n = len(blocks_sorted)
  used = [False] * n
  out = []
  for i in range(n):
    if used[i]:
      continue
    group = [blocks_sorted[i]]
    used[i] = True
    changed = True
    while changed:
      changed = False
      for j in range(n):
        if used[j]:
          continue
        ref = group[-1]
        c = blocks_sorted[j]
        v_overlap = min(ref["bottom"], c["bottom"]) - max(ref["top"], c["top"])
        min_h = max(1, min(ref["bottom"] - ref["top"], c["bottom"] - c["top"]))
        gap = c["left"] - ref["right"]
        if v_overlap >= 0.5 * min_h and -min_h * 0.5 <= gap <= 1.6 * min_h:
          group.append(c)
          used[j] = True
          changed = True
    if len(group) > 1:
      g = sorted(group, key=lambda x: x["left"])
      m = {
        "text": "".join(x["text"] for x in g),
        "conf": round(sum(x["conf"] for x in g) / len(g), 4),
        "left": min(x["left"] for x in g), "top": min(x["top"] for x in g),
        "right": max(x["right"] for x in g), "bottom": max(x["bottom"] for x in g),
      }
      m["cx"] = (m["left"] + m["right"]) // 2
      m["cy"] = (m["top"] + m["bottom"]) // 2
      out.append(m)
    else:
      out.append(group[0])
  return out


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def selftest():
  checks = []

  def add(name, ok, detail=""):
    checks.append({"check": name, "pass": bool(ok), "detail": str(detail)[:200]})

  try:
    import rapidocr_onnxruntime  # noqa: F401
    add("依赖 rapidocr_onnxruntime", True)
  except Exception as e:
    add("依赖 rapidocr_onnxruntime", False, f"{e}; 修复: pip install rapidocr-onnxruntime")
  try:
    import mss  # noqa: F401
    import numpy  # noqa: F401
    add("依赖 mss / numpy", True)
  except Exception as e:
    add("依赖 mss / numpy", False, f"{e}; 修复: pip install mss numpy")

  try:
    get_engine()
    add("OCR 引擎初始化", True)
  except Exception as e:
    add("OCR 引擎初始化", False, f"{e}; 修复: pip install rapidocr-onnxruntime 或检查磁盘空间")

  try:
    from PIL import Image, ImageDraw, ImageFont
    import cv2
    import numpy as np

    img = Image.new("RGB", (400, 120), (255, 255, 255))
    d = ImageDraw.Draw(img)
    font = None
    for fp in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
      try:
        font = ImageFont.truetype(fp, 40)
        break
      except Exception:
        pass
    if font is None:
      raise RuntimeError("未找到中文字体")
    d.text((30, 40), "发送取消", fill=(0, 0, 0), font=font)
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    blocks = ocr_frame(arr)
    found = any("发送" in b["text"] or "取消" in b["text"] for b in blocks)
    add("样图中文 OCR", found, f"识别到 {len(blocks)} 块: {[b['text'] for b in blocks][:4]}")

    to_absolute(blocks, 0, 0)
    target = next(b for b in blocks if "发送" in b["text"])
    hit, _ = hit_test(blocks, target["cx"], target["cy"])
    ok2 = hit is not None and "发送" in hit["text"]
    add("空间反查(--at 逻辑)", ok2, f"点({target['cx']},{target['cy']}) 反查命中「{hit['text'] if hit else None}」")
  except Exception as e:
    add("样图中文 OCR", False, e)

  try:
    arr, w, h, _, _ = grab_frame()
    add("真实屏幕截屏", w > 100 and h > 100, f"{w}x{h}")
  except Exception as e:
    add("真实屏幕截屏", False, f"{e}; 修复: 检查显示器/会话是否可交互")

  passed = sum(1 for c in checks if c["pass"])
  return {
    "ok": passed == len(checks),
    "version": VERSION,
    "passed": f"{passed}/{len(checks)}",
    "checks": checks,
    "hint": "全部通过即可正常使用；失败项按 detail 里的修复命令处理后重试",
  }


def to_absolute(blocks, off_x, off_y):
  """把 OCR 块坐标从帧局部坐标系平移到屏幕绝对坐标系，并补中心点。"""
  for b in blocks:
    b["left"] += off_x
    b["top"] += off_y
    b["right"] += off_x
    b["bottom"] += off_y
    b["cx"] = (b["left"] + b["right"]) // 2
    b["cy"] = (b["top"] + b["bottom"]) // 2
  return blocks


def select_from_snapshot(blocks, args):
  """按编号/文字/混合 token 挑选。返回 (picked列表, missing列表)。"""
  picked, missing = [], []
  if getattr(args, "pick", None):
    by_no = {b.get("no", i + 1): b for i, b in enumerate(blocks)}
    taken = set()
    for tok in args.pick:
      b = None
      if tok.isdigit():
        b = by_no.get(int(tok))
        label = int(tok)
      else:
        nq, fq = norm_text(tok), fold_confusion(norm_text(tok))
        for cand in blocks:
          if id(cand) in taken:
            continue
          nt = norm_text(cand["text"])
          if nt and (nq in nt or fq in fold_confusion(nt)):
            b = cand
            label = tok
            taken.add(id(cand))
            break
      if b is None:
        missing.append(tok)
        continue
      item = dict(block_public(b))
      item["pick"] = label
      item["conf"] = b.get("conf", 1.0)
      picked.append(item)
  else:  # --pick-text
    for q in args.pick_text:
      nq, fq = norm_text(q), fold_confusion(norm_text(q))
      matched = None
      for b in blocks:
        nt = norm_text(b["text"])
        if not nt:
          continue
        if nq in nt or fq in fold_confusion(nt) or (
            args.match == "fuzzy" and difflib.SequenceMatcher(None, nq, nt).ratio() >= 0.7):
          matched = b
          break
      if matched is None:
        missing.append(q)
        continue
      item = dict(block_public(matched))
      item["no"] = matched.get("no", 0)
      item["conf"] = matched.get("conf", 1.0)
      item["query"] = q
      picked.append(item)
  return picked, missing


def get_dpi_scale():
  try:
    hdc = ctypes.windll.user32.GetDC(0)
    dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
    ctypes.windll.user32.ReleaseDC(0, hdc)
    return dpi / 96.0
  except Exception:
    return 1.0


# ---------------------------------------------------------------------------
# 守护进程（引擎常驻）与客户端
# ---------------------------------------------------------------------------
DAEMON_PORT = 8377


def serve_daemon(port):
  """常驻模式：引擎加载+预热一次，之后每次调用只花 截屏+推理 的时间。"""
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
  import numpy as np

  get_engine()
  ocr_frame(np.full((80, 500, 3), 245, dtype="uint8"))  # 预热：触发 ONNX 图优化
  print(f"[daemon] findtext v{VERSION} 就绪：http://127.0.0.1:{port}（Ctrl+C 停止）", flush=True)

  class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
      pass

    def _send(self, obj):
      body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def do_GET(self):
      if self.path.startswith("/health"):
        self._send({"ok": True, "tool": "findtext", "version": VERSION, "pid": os.getpid()})
      else:
        self._send({"ok": False, "error": "use POST /ocr"})

    def do_POST(self):
      if not self.path.startswith("/ocr"):
        self._send({"ok": False, "error": "unknown endpoint"})
        return
      try:
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        ns = argparse.Namespace(**body)
        out, code = execute(ns)
        self._send({"ok": True, "result": out, "exit_code": code})
      except Exception as e:
        self._send({"ok": False, "result": {"ok": False, "error": f"{type(e).__name__}: {e}"},
                    "exit_code": 2})

  ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def daemon_health(port, timeout=0.35):
  """探测常驻守护（版本一致才算命中）。返回 health dict 或 None。"""
  try:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
      d = json.loads(resp.read().decode("utf-8"))
      return d if d.get("ok") and d.get("version") == VERSION else None
  except Exception:
    return None


def daemon_call(port, body, timeout):
  import urllib.request

  req = urllib.request.Request(
    f"http://127.0.0.1:{port}/ocr",
    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 基准
# ---------------------------------------------------------------------------
def bench(out_path, runs=3):
  """标准化基准：全屏 / 区域 / 缓存命中 各 runs 次 + 受控图准确率抽检。"""
  from PIL import Image, ImageDraw, ImageFont

  results = {"version": VERSION, "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "modes": {}}

  def timed(args_list, n=runs):
    ts = []
    last = {}
    for _ in range(n):
      t0 = time.time()
      proc = subprocess.run([sys.executable, os.path.abspath(__file__)] + args_list,
                            capture_output=True, text=True, encoding="utf-8")
      ts.append(round((time.time() - t0) * 1000))
      try:
        last = json.loads(proc.stdout)
      except Exception:
        pass
    return {"runs_ms": ts, "median_ms": sorted(ts)[len(ts) // 2], "last_ok": last.get("ok")}

  results["modes"]["全屏 --all"] = timed(["--all", "--no-save"])
  results["modes"]["全屏 --all（含落盘证据）"] = timed(["--all"])
  results["modes"]["区域 --region 500x420"] = timed(["--region", "0,80,500,420", "--all", "--no-save"])
  results["modes"]["区域 --around 300"] = timed(["--around", "400,300", "--size", "300", "--all", "--no-save"])

  # 缓存命中：同一图片文件连续两次 --image（像素必然一致 → 第二次命中缓存）
  from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font

  cache_img = os.path.join(DEFAULT_SAVE_DIR, "_bench_cache.png")
  _img2 = _Img.new("RGB", (900, 400), (250, 250, 250))
  _d2 = _Draw.Draw(_img2)
  _f2 = _Font.truetype(r"C:\Windows\Fonts\msyh.ttc", 34)
  _d2.text((40, 60), "缓存命中测试 固定画面", font=_f2, fill=(0, 0, 0))
  _img2.save(cache_img)
  subprocess.run([sys.executable, os.path.abspath(__file__), "--image", cache_img, "--all"],
                 capture_output=True, text=True)
  t0 = time.time()
  proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--image", cache_img, "--all"],
                        capture_output=True, text=True)
  results["modes"]["缓存命中(同图二次)"] = {
    "runs_ms": [round((time.time() - t0) * 1000)],
    "cached": json.loads(proc.stdout).get("cached", False) if proc.stdout.strip() else False}
  os.remove(cache_img)

  # 准确率抽检：受控图 4 词
  targets = {"发送": (800, 500), "取消": (950, 500), "用户名": (200, 200), "确定": (60, 650)}
  img = Image.new("RGB", (1280, 720), (240, 240, 240))
  d = ImageDraw.Draw(img)
  font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 40)
  truth = {}
  for text, (x, y) in targets.items():
    d.text((x, y), text, fill=(0, 0, 0), font=font)
    bb = d.textbbox((x, y), text, font=font)
    truth[text] = ((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
  acc_path = os.path.join(DEFAULT_SAVE_DIR, "_bench_acc.png")
  img.save(acc_path)
  proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--image", acc_path,
                         "--query", "发送,取消,用户名,确定"],
                        capture_output=True, text=True, encoding="utf-8")
  os.remove(acc_path)
  try:
    d = json.loads(proc.stdout)
  except Exception:
    d = {}
  ok, maxdev = 0, 0
  for r in d.get("results", []):
    q = r["query"]
    if r["matches"]:
      mt = r["matches"][0]
      dev = max(abs(mt["x"] - truth[q][0]), abs(mt["y"] - truth[q][1]))
      maxdev = max(maxdev, dev)
      if dev <= 3:
        ok += 1
  results["accuracy"] = {"sample": "受控图4词", "命中且偏差≤3px": f"{ok}/4", "max_dev_px": maxdev}
  json.dump(results, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
  return results


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def resolve_capture(args):
  """根据 --window/--region/--around 解析截屏区域。返回 (region 或 None, monitor, err 或 None)。"""
  if getattr(args, "window", None):
    hit = get_window_rect(args.window)
    if not hit:
      return None, None, {"ok": False, "error": f"未找到标题包含「{args.window}」的可见窗口",
                          "hint": "窗口须未最小化；标题支持部分匹配；或用 --monitor list 查看显示器"}
    hwnd, (l, tp, r, b), wtitle = hit
    if not getattr(args, "no_activate", False):
      activate_window(hwnd)
    print(f"[window] {wtitle} region={(l, tp, r - l, b - tp)}", file=sys.stderr)
    return (l, tp, r - l, b - tp), args.monitor, None
  if getattr(args, "around", None):
    ax, ay = args.around
    size = int(getattr(args, "size", 400))
    return (max(0, ax - size // 2), max(0, ay - size // 2), size, size), args.monitor, None
  if args.region:
    try:
      region = [int(v) for v in args.region.split(",")]
      assert len(region) == 4
      return region, args.monitor, None
    except Exception:
      return None, None, {"ok": False, "error": "--region 格式应为 x,y,w,h"}
  return None, args.monitor, None


def _exit_code(out):
  flags = [r["found"] for r in out.get("results", [])]
  for key in ("at", "near", "near_text", "in"):
    if key in out:
      flags.append(out[key]["found"])
  if "picked" in out:
    flags.append(bool(out["picked"]) and not out.get("missing"))
  return 0 if all(flags) else 1


def _finalize(out, args, blocks, arr, off_x, off_y, dpi, t0):
  """公共收尾：查询匹配(含verify) / 空间查询 / --all / --lines / 挑选 / 输出。"""

  # 查询匹配（未命中时附带自救建议；--verify 裁剪复核）
  if args.query:
    queries = [q.strip() for q in args.query.split(",") if q.strip()]
    results = []
    for q in queries:
      hits = [b for b in match_blocks(blocks, q, mode=args.match) if b["conf"] >= args.min_conf]
      hits.sort(key=lambda b: -b["conf"])
      item = {"query": q, "found": len(hits) > 0,
              "matches": [block_to_match(b, dpi) for b in hits]}
      if item["found"] and getattr(args, "verify", False):
        for mt in item["matches"][:3]:
          local = [mt["bbox"][0] - off_x, mt["bbox"][1] - off_y,
                   mt["bbox"][2] - off_x, mt["bbox"][3] - off_y]
          mt["verify"] = verify_frame(arr, local, q)
      if not item["found"]:
        sugg = ["改用 --all 查看屏幕上实际识别到了哪些文字",
                "降低 --min-conf（如 0.2）再试",
                "改用 --match fuzzy 容错错字",
                "若按钮文字可能被拆成相邻两块，加 --join 重试",
                "检查目标是否在另一显示器（--monitor）或屏幕内容已变化，重新截屏",
                "只关心某个应用时用 --window 标题 直接在该窗口内找"]
        if len(queries) > 1:
          sugg.insert(0, "逐词单独查询，定位是哪个词未命中")
        item["suggest"] = sugg
      results.append(item)
    out["results"] = results
    if len(results) == 1:
      out["query"] = results[0]["query"]
      out["found"] = results[0]["found"]
      out["matches"] = results[0]["matches"]
      if not out["found"]:
        out["suggest"] = results[0]["suggest"]

  # 坐标反查
  if getattr(args, "at", None):
    ax, ay = args.at
    hit, near = hit_test(blocks, ax, ay)
    out["at"] = {"point": [ax, ay], "found": hit is not None}
    if hit is not None:
      out["at"]["hit"] = block_public(hit)
      if hit["conf"] < args.min_conf:
        out["at"]["low_conf"] = True
    if near:
      nb, dist = near
      out["at"]["nearest"] = dict(block_public(nb), distance=dist)
      out["at"].setdefault("suggest", []).append(
        f"该点上没有文字；最近的文字是「{nb['text']}」距 {dist}px，若找它请用其中心 {nb['cx']},{nb['cy']}")

  # 邻域
  if getattr(args, "near", None):
    nx, ny = args.near
    radius = getattr(args, "radius", 200)
    found_list = near_query(blocks, nx, ny, radius)
    out["near"] = {"point": [nx, ny], "radius": radius,
                   "found": len(found_list) > 0, "blocks": found_list}

  # 文字锚点邻域
  if getattr(args, "near_text", None):
    anchors = [b for b in match_blocks(blocks, args.near_text, mode=args.match)
               if b["conf"] >= args.min_conf]
    radius_nt = getattr(args, "radius", 200)
    out["near_text"] = {"anchor_query": args.near_text, "found": bool(anchors)}
    if anchors:
      a = max(anchors, key=lambda b: b["conf"])
      neighbors = [dict(block_public(b),
                        distance=round(((b["cx"] - a["cx"]) ** 2 + (b["cy"] - a["cy"]) ** 2) ** 0.5))
                   for b in blocks
                   if b is not a and ((b["cx"] - a["cx"]) ** 2 + (b["cy"] - a["cy"]) ** 2) ** 0.5 <= radius_nt]
      neighbors.sort(key=lambda i: i["distance"])
      out["near_text"].update({"anchor": block_public(a), "radius": radius_nt, "neighbors": neighbors})
    else:
      out["near_text"]["suggest"] = ["锚点文字未找到：改 --all 查看实际文字，或 --match fuzzy 容错"]

  # 矩形区域
  if getattr(args, "in_rect", None):
    rx, ry, rw, rh = args.in_rect
    found_list = in_rect_query(blocks, rx, ry, rw, rh)
    out["in"] = {"rect": [rx, ry, rw, rh], "found": len(found_list) > 0, "blocks": found_list}

  # --all / --lines
  numbered = number_blocks([b for b in blocks if b["conf"] >= args.min_conf]) \
    if (args.all or getattr(args, "lines", False)) else None
  if args.all:
    out["blocks"] = [
      {"no": b["no"], "text": b["text"], "conf": b["conf"], "grade": conf_grade(b["conf"]),
       "bbox": [b["left"], b["top"], b["right"], b["bottom"]], "center": [b["cx"], b["cy"]]}
      for b in numbered
    ]
  if getattr(args, "lines", False):
    out["lines"] = [
      {"row": ri + 1, "y": min(b["top"] for b in line), "count": len(line),
       "text": " | ".join(b["text"] for b in line),
       "items": [{"no": b["no"], "text": b["text"], "center": [b["cx"], b["cy"]]} for b in line]}
      for ri, line in enumerate(cluster_lines(numbered))
    ]

  # 新鲜路径挑选（无 --from）
  if getattr(args, "pick", None) or getattr(args, "pick_text", None):
    picked, missing = select_from_snapshot(numbered if numbered is not None else blocks, args)
    out["picked"] = picked
    out["found"] = len(picked) > 0
    if missing:
      out["missing"] = missing

  return out


def execute(args):
  """执行并返回 (out_dict, exit_code)。不打印任何内容（打印由 run()/守护进程负责）。"""
  t0 = time.time()

  # 0. 快照挑选模式：--pick/--pick-text + --from，直接读上次 --all 的 JSON，秒回、编号不漂移
  if (getattr(args, "pick", None) or getattr(args, "pick_text", None)) and getattr(args, "from_json", None):
    try:
      snap = json.load(open(args.from_json, encoding="utf-8"))
      blocks = snap.get("blocks", [])
    except FileNotFoundError:
      return ({"ok": False, "error": f"快照文件不存在: {args.from_json}",
               "hint": "先用 --all --out 快照.json 生成，再 --pick N --from 快照.json"}, 2)
    except Exception as e:
      return ({"ok": False, "error": f"快照文件无法解析: {e}",
               "hint": "--from 需要的是此前 --all --out 保存的 JSON 文件"}, 2)
    for i, b in enumerate(blocks, 1):
      b.setdefault("no", i)
      b.setdefault("conf", 1.0)
      if "center" not in b:
        b["center"] = [(b["bbox"][0] + b["bbox"][2]) // 2, (b["bbox"][1] + b["bbox"][3]) // 2]
      if "left" not in b:
        b["left"], b["top"], b["right"], b["bottom"] = b["bbox"]
      b["cx"], b["cy"] = b["center"]
    picked, missing = select_from_snapshot(blocks, args)
    out = {"ok": True, "from": args.from_json, "picked": picked, "found": len(picked) > 0}
    if missing:
      out["missing"] = missing
      out["suggest"] = [f"编号超出快照范围（快照共 {len(blocks)} 块），先重新 --all 确认"]
    if getattr(args, "pick_text", None):
      out["query"] = args.pick_text
    return (out, 0 if picked else 1)

  # 0.5 等待模式：--wait-for / --wait-change（轮询期间引擎常驻）
  if getattr(args, "wait_for", None) or getattr(args, "wait_change", False):
    region, mon, err = resolve_capture(args)
    if err:
      print(json.dumps(err, ensure_ascii=False))
      return 2
    scale = 0.5 if getattr(args, "fast", False) else getattr(args, "scale", 1.0)
    use_cls = bool(getattr(args, "cls", False))
    if args.wait_for:
      deadline = time.time() + args.timeout
      attempts = 0
      last_arr = None
      while True:
        attempts += 1
        arr, w, h, off_x, off_y = grab_frame(region, mon)
        last_arr = arr
        blocks = to_absolute(ocr_frame(arr, scale=scale, use_cls=use_cls), off_x, off_y)
        if getattr(args, "join", False):
          blocks = merge_lines(blocks)
        hits = [b for b in match_blocks(blocks, args.wait_for, mode=args.match)
                if b["conf"] >= args.min_conf]
        if hits:
          hits.sort(key=lambda b: -b["conf"])
          res = {"ok": True, "found": True, "wait_for": args.wait_for, "attempts": attempts,
                 "waited_ms": int((time.time() - (deadline - args.timeout)) * 1000),
                 "match": block_to_match(hits[0], get_dpi_scale())}
          if getattr(args, "save", None):
            save_png(arr, args.save)
            res["image"] = args.save
          return (res, 0)
        if time.time() >= deadline:
          return ({"ok": True, "found": False, "wait_for": args.wait_for,
                   "attempts": attempts, "waited_ms": int(args.timeout * 1000),
                   "blocks_last_seen": len(blocks),
                   "suggest": ["目标可能未出现：确认触发动作已执行",
                               "加大 --timeout 或缩小 --region 加快轮询",
                               "用 --all 查看当前屏幕上实际有什么"]}, 1)
        time.sleep(max(0.3, args.interval))
    else:
      start = time.time()
      deadline = start + args.timeout
      arr, w, h, off_x, off_y = grab_frame(region, mon)
      base = set(norm_text(b["text"]) for b in ocr_frame(arr, scale=scale, use_cls=use_cls)
                 if b["conf"] >= args.min_conf)
      attempts = 0
      while True:
        attempts += 1
        time.sleep(max(0.3, args.interval))
        if time.time() >= deadline:
          return ({"ok": True, "changed": False, "attempts": attempts,
                   "waited_ms": int((time.time() - start) * 1000),
                   "baseline_blocks": len(base),
                   "suggest": ["画面在超时内未变化：确认触发动作是否真的执行了"]}, 1)
        arr, w, h, off_x, off_y = grab_frame(region, mon)
        blocks = to_absolute(ocr_frame(arr, scale=scale, use_cls=use_cls), off_x, off_y)
        sigs = set(norm_text(b["text"]) for b in blocks if b["conf"] >= args.min_conf)
        if sigs != base:
          appeared = sorted(list(sigs - base))[:30]
          disappeared = sorted(list(base - sigs))[:30]
          if getattr(args, "save", None):
            save_png(arr, args.save)
          return ({"ok": True, "changed": True, "attempts": attempts,
                   "waited_ms": int((time.time() - start) * 1000),
                   "appeared": appeared, "disappeared": disappeared,
                   "blocks": [block_public(b) for b in
                              number_blocks([b for b in blocks if b["conf"] >= args.min_conf])][:40]}, 0)

  # 0.9 空间查询的区域预判：--at/--near/--in 只截目标周边（全分辨率小图，提速核心）
  spatial_region = None
  if args.at or args.near or args.in_rect:
    margin = 500
    if args.at:
      cx, cy = args.at
    elif args.near:
      cx, cy = args.near
      margin = int(getattr(args, "radius", 200)) + 300
    else:
      cx, cy = args.in_rect[0] + args.in_rect[2] // 2, args.in_rect[1] + args.in_rect[3] // 2
      margin = max(args.in_rect[2], args.in_rect[3]) // 2 + 300
    sm_w = ctypes.windll.user32.GetSystemMetrics(0)
    sm_h = ctypes.windll.user32.GetSystemMetrics(1)
    x0, y0 = max(0, cx - margin), max(0, cy - margin)
    x1, y1 = min(sm_w, cx + margin), min(sm_h, cy + margin)
    spatial_region = (x0, y0, x1 - x0, y1 - y0)

  # 1. 取图：截屏（ndarray，不落盘）或 读取图片文件
  if args.image:
    image_path = os.path.abspath(args.image)
    off_x, off_y = 0, 0
    region, mon = None, "primary"  # 图片模式无截屏区域（region_key 用）
    import cv2
    import numpy as np

    arr = cv2.imdecode(np.fromfile(image_path, dtype="uint8"), cv2.IMREAD_COLOR)
    if arr is None:
      return ({"ok": False, "error": f"无法读取图片: {image_path}",
               "hint": "检查路径与图片格式（建议绝对路径）"}, 2)
    h, w = arr.shape[:2]
    if getattr(args, "around", None):  # 图片模式同样支持以点为中心裁剪（坐标仍按原图计）
      ax, ay = args.around
      size = int(getattr(args, "size", 400))
      cx0, cy0 = max(0, ax - size // 2), max(0, ay - size // 2)
      arr = arr[cy0:min(h, cy0 + size), cx0:min(w, cx0 + size)]
      off_x, off_y = cx0, cy0
      h, w = arr.shape[:2]
    if getattr(args, "save", None):
      save_png(arr, args.save)
  else:
    if spatial_region:
      region, mon = spatial_region, "primary"
    else:
      region, mon, err = resolve_capture(args)
      if err:
        return (err, 2)
    arr, w, h, off_x, off_y = grab_frame(region, mon)
    if getattr(args, "save", None):
      save_png(arr, args.save)

  sig = frame_sig(arr)
  md5 = hashlib.md5(arr.tobytes()).hexdigest()
  region_key = json.dumps([region, mon])

  # 2. 内容缓存：精确 md5 + 模糊指纹（内存/文件），动态屏（时钟/光标/流式文本）也可命中
  blocks = None
  cached = False
  cache_kind = None
  if not getattr(args, "no_cache", False):
    mem = MEM_CACHE.get(region_key)
    if mem and sig_same(mem["sig"], sig):
      blocks = mem["blocks"]          # 内存命中（守护模式/进程内复用，最快）
      cached = True
      cache_kind = "mem"
    if blocks is None:
      c = cache_find(arr, sig, md5, region_key)
      if c:
        blocks = c["blocks"]          # 文件命中（跨进程复用）
        cached = True
        cache_kind = "fuzzy" if c.get("fuzzy") else "exact"
    if blocks is not None:
      out = {"ok": True, "cached": True, "cache": cache_kind,
             "screen": {"offset": [off_x, off_y], "width": w, "height": h,
                        "dpi_scale": get_dpi_scale()},
             "blocks_found": len(blocks)}
      if spatial_region:
        out["capture_region"] = list(spatial_region)
      dpi = out["screen"]["dpi_scale"]
      return (_finalize(out, args, blocks, arr, off_x, off_y, dpi, t0), _exit_code(out))

  # 3. OCR（截图默认 use_cls=False；--image 文件默认 True；--cls 强制开启）
  scale = 0.5 if getattr(args, "fast", False) else getattr(args, "scale", 1.0)
  use_cls = bool(getattr(args, "cls", False)) or bool(args.image)
  blocks = to_absolute(
    ocr_frame(arr, scale=scale, contrast=getattr(args, "contrast", False), use_cls=use_cls),
    off_x, off_y)
  if getattr(args, "join", False):
    blocks = merge_lines(blocks)
  ocr_ms = int((time.time() - t0) * 1000)
  if not getattr(args, "no_cache", False):
    MEM_CACHE[region_key] = {"sig": sig, "blocks": blocks}
    if len(MEM_CACHE) > 4:  # 只保留最近 4 个区域
      for k in list(MEM_CACHE)[:-4]:
        MEM_CACHE.pop(k)
  cache_store(md5, sig, blocks, None, region_key)

  dpi = get_dpi_scale()
  out = {
    "ok": True,
    "cached": False,
    "screen": {"offset": [off_x, off_y], "width": w, "height": h, "dpi_scale": dpi},
    "engine": "rapidocr_onnxruntime",
    "ocr_ms": ocr_ms,
    "blocks_found": len(blocks),
  }
  if spatial_region:
    out["capture_region"] = list(spatial_region)
  if getattr(args, "save", None):
    out["image"] = args.save

  return (_finalize(out, args, blocks, arr, off_x, off_y, dpi, t0), _exit_code(out))


def run(args):
  """执行并打印（CLI 入口）。返回退出码。"""
  out, code = execute(args)
  js = json.dumps(out, ensure_ascii=False, indent=None if getattr(args, "compact", False) else 2)
  print(js)
  if getattr(args, "out", None):
    open(args.out, "w", encoding="utf-8").write(js)
    print(f"[saved json] {args.out}", file=sys.stderr)
  return code


def main():
  if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

  p = argparse.ArgumentParser(
    description="全图 OCR 文字定位工具 v4.0：截屏→识别→找字→返回屏幕坐标(JSON)。首次使用建议先跑 --selftest"
  )
  p.add_argument("--query", help='要找的文字；多个词用英文逗号分隔，如 "发送,打开"')
  p.add_argument("--verify", action="store_true", help="配合 --query：对命中结果裁剪重 OCR 复核，输出 verified")
  p.add_argument("--at", help="坐标反查：这个点上是什么文字，如 --at 1200,800")
  p.add_argument("--near", help="找某坐标半径内的文字块，如 --near 1200,800")
  p.add_argument("--near-text", help='以某文字为锚点找附近元素，如 --near-text 用户名 --radius 300')
  p.add_argument("--radius", type=float, default=200, help="--near/--near-text 的搜索半径（物理像素），默认 200")
  p.add_argument("--in", dest="in_rect", help="列出某矩形区域内的文字块，如 --in 0,0,400,300")
  p.add_argument("--pick", help='按编号/文字挑选坐标（混合可用），如 --pick 3,7 或 --pick 发送,3')
  p.add_argument("--pick-text", help='按文字挑选坐标，如 --pick-text "新建任务,插件市场"')
  p.add_argument("--from", dest="from_json", help='挑选的快照来源：此前 --all --out 保存的 JSON（秒回、编号不漂移）')
  p.add_argument("--match", choices=["exact", "contains", "fuzzy"], default="contains",
                 help="匹配模式：exact=完全相等 contains=包含(默认) fuzzy=模糊容错(含形近字折叠)")
  p.add_argument("--image", help="对已有图片找字（跳过截屏；默认启用方向分类）")
  p.add_argument("--save", help="截图/帧保存路径（PNG）")
  p.add_argument("--out", help="JSON 结果另存路径")
  p.add_argument("--all", action="store_true", help="输出全部识别到的文字块（带编号/中心点/置信度分级）")
  p.add_argument("--lines", action="store_true", help="按视觉行输出界面文字结构")
  p.add_argument("--region", help="只截取区域 x,y,w,h（屏幕物理像素坐标）")
  p.add_argument("--around", help="以某点为中心截取周边区域，如 --around 1200,800（配 --size）")
  p.add_argument("--size", type=int, default=400, help="--around 的方形边长，默认 400")
  p.add_argument("--window", help='只截取指定标题的窗口（自动置前；配 --no-activate 关闭置前）')
  p.add_argument("--no-activate", action="store_true", help="--window 时不把窗口带到前台")
  p.add_argument("--monitor", default="primary",
                 help='primary=主显示器(默认) all=全部显示器 或显示器编号；list=列出显示器')
  p.add_argument("--min-conf", type=float, default=0.3, help="置信度过滤阈值，默认 0.3")
  p.add_argument("--join", action="store_true", help="开启同行相邻文字块合并")
  p.add_argument("--scale", type=float, default=1.0, help="识别前缩放倍率(0~1]，坐标自动还原")
  p.add_argument("--fast", action="store_true", help="等价 --scale 0.5，速度优先")
  p.add_argument("--contrast", action="store_true", help="自动对比度增强（低对比度文字场景）")
  p.add_argument("--cls", action="store_true", help="开启方向分类（截图默认关闭以提速；--image 默认开启）")
  p.add_argument("--no-cache", action="store_true", help="禁用同画面内容缓存（默认开启：md5 一致即复用）")
  p.add_argument("--no-save", action="store_true", help="兼容保留（v4.0 截屏默认不落盘，此参数无实际作用）")
  p.add_argument("--wait-for", help='轮询等待目标文字出现，如 --wait-for "发送成功"')
  p.add_argument("--wait-change", action="store_true", help="轮询等待画面文字变化")
  p.add_argument("--timeout", type=float, default=15, help="--wait-for/--wait-change 超时秒数，默认 15")
  p.add_argument("--interval", type=float, default=1.5, help="轮询间隔秒数，默认 1.5")
  p.add_argument("--diff", nargs=2, metavar=("快照A.json", "快照B.json"), help="对比两张 --all 快照")
  p.add_argument("--compact", action="store_true", help="JSON 单行输出（AI 解析省 token）")
  p.add_argument("--serve", action="store_true", help="常驻守护模式：引擎预热一次，HTTP 服务于 127.0.0.1，CLI 自动复用")
  p.add_argument("--port", type=int, default=DAEMON_PORT, help="守护端口，默认 8377")
  p.add_argument("--no-daemon", action="store_true", help="禁用守护复用（强制本进程执行）")
  p.add_argument("--bench", action="store_true", help="性能基准（全屏/区域/缓存命中×3 + 准确率抽检），写入 基准测试.json")
  p.add_argument("--selftest", action="store_true", help="一键自检（依赖/引擎/截屏/OCR/坐标反查），输出 JSON")
  p.add_argument("--version", action="store_true", help="输出版本号")
  args = p.parse_args()

  if args.version:
    print(json.dumps({"ok": True, "tool": "findtext", "version": VERSION,
                      "engine": "rapidocr_onnxruntime"}, ensure_ascii=False))
    sys.exit(0)

  if args.serve:
    try:
      serve_daemon(args.port)
    except KeyboardInterrupt:
      print()
      print("[daemon] stopped", file=sys.stderr)
    sys.exit(0)

  if args.selftest:
    try:
      print(json.dumps(selftest(), ensure_ascii=False, indent=2))
      sys.exit(0)
    except Exception as e:
      print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
      sys.exit(2)

  if args.bench:
    try:
      res = bench(os.path.join(DEFAULT_SAVE_DIR, "基准测试.json"))
      print(json.dumps(res, ensure_ascii=False, indent=2))
      sys.exit(0)
    except Exception as e:
      print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
      sys.exit(2)

  if args.diff:
    try:
      A = json.load(open(args.diff[0], encoding="utf-8")).get("blocks", [])
      B = json.load(open(args.diff[1], encoding="utf-8")).get("blocks", [])
      print(json.dumps({"ok": True, "a": args.diff[0], "b": args.diff[1],
                        **diff_snapshots(A, B)}, ensure_ascii=False, indent=2))
      sys.exit(0)
    except FileNotFoundError as e:
      print(json.dumps({"ok": False, "error": f"快照文件不存在: {e}",
                        "hint": "先分别执行 --all --out A.json 和 --all --out B.json"}, ensure_ascii=False))
      sys.exit(2)

  if (args.monitor or "").lower() == "list":
    try:
      import mss
      factory = getattr(mss, "MSS", mss.mss)
      with factory() as sct:
        mons = [{"index": i, "left": m["left"], "top": m["top"],
                 "width": m["width"], "height": m["height"]}
                for i, m in enumerate(sct.monitors)]
      print(json.dumps({"ok": True, "monitors": mons,
                        "note": "0=全部虚拟屏 1=主显示器；--monitor 可接编号"}, ensure_ascii=False, indent=2))
      sys.exit(0)
    except Exception as e:
      print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
      sys.exit(2)

  # 坐标类参数解析
  def parse_ints(s, n, name):
    try:
      v = [int(x) for x in s.split(",")]
      assert len(v) == n
      return v
    except Exception:
      print(json.dumps({"ok": False, "error": f"{name} 格式应为逗号分隔的 {n} 个整数，收到: {s}"},
                       ensure_ascii=False))
      sys.exit(2)

  args.at = parse_ints(args.at, 2, "--at 1200,800") if args.at else None
  args.near = parse_ints(args.near, 2, "--near 1200,800") if args.near else None
  args.in_rect = parse_ints(args.in_rect, 4, "--in 0,0,400,300") if args.in_rect else None
  args.around = parse_ints(args.around, 2, "--around 1200,800") if args.around else None
  if args.pick:
    args.pick = [x.strip() for x in args.pick.split(",") if x.strip()]
    for x in args.pick:
      if x.isdigit() and int(x) < 1:
        print(json.dumps({"ok": False, "error": f"--pick 编号须从 1 起，收到: {x}",
                          "hint": "编号来自 --all 输出的 no 字段；也可直接写文字，如 --pick 发送,3"},
                         ensure_ascii=False))
        sys.exit(2)
  if args.pick_text:
    args.pick_text = [x.strip() for x in args.pick_text.split(",") if x.strip()]

  has_action = any([bool(args.query), bool(args.all), args.at is not None, args.near is not None,
                    args.in_rect is not None, bool(args.near_text), bool(args.pick),
                    bool(args.pick_text), bool(args.wait_for), args.wait_change, args.lines])
  if not has_action:
    p.error("至少提供 --query / --all / --at / --near / --near-text / --in / --lines / --pick / "
            "--pick-text / --wait-for / --wait-change 之一")

  # 守护复用：本机已有同版本常驻引擎时，进程启动/导入/初始化/预热全部归零
  if not getattr(args, "no_daemon", False) and daemon_health(args.port):
    body = {k: v for k, v in vars(args).items()
            if k not in ("serve", "port", "no_daemon", "bench", "selftest")}
    wait_t = max(30.0, (args.timeout or 0) + 15.0) if (args.wait_for or args.wait_change) else 30.0
    try:
      d = daemon_call(args.port, body, wait_t)
      out = d.get("result", {})
      js = json.dumps(out, ensure_ascii=False, indent=None if args.compact else 2)
      print(js)
      if getattr(args, "out", None):
        open(args.out, "w", encoding="utf-8").write(js)
      sys.exit(d.get("exit_code", 0))
    except Exception:
      pass  # 守护处理失败 → 落回本地执行

  try:
    sys.exit(run(args))
  except SystemExit:
    raise
  except FileNotFoundError as e:
    print(json.dumps({"ok": False, "error": f"文件不存在: {e}",
                      "hint": "检查 --image / --out / --save 的路径是否正确（建议绝对路径）"}, ensure_ascii=False))
    sys.exit(2)
  except ModuleNotFoundError as e:
    print(json.dumps({"ok": False, "error": f"缺少依赖: {e}",
                      "hint": "运行 pip install rapidocr-onnxruntime mss numpy opencv-python 后重试，或先跑 --selftest"},
                     ensure_ascii=False))
    sys.exit(2)
  except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}",
                      "hint": "先跑 python findtext.py --selftest 定位问题；截屏类报错多为会话不可交互或权限不足"},
                     ensure_ascii=False))
    sys.exit(2)


if __name__ == "__main__":
  main()

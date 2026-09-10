#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台验收.py — HTML/网页成果的无头验收工具（零弹窗、零抢焦点）
=============================================================

原理：调用本机已安装的 Edge/Chrome 的 headless 模式渲染页面并直接输出截图文件。
headless 浏览器**架构上不创建任何窗口**，从根源上杜绝"弹窗抢焦点"。

用法：
  python 后台验收.py 目标.html                          # 截图存到同目录 验收截图/ 下
  python 后台验收.py 目标.html --expect "墨韵,番茄钟"     # 截图后 OCR 核对页面含预期文字
  python 后台验收.py 目标.html --out 指定.png --width 1440 --height 900 --vtime 4000

输出：JSON（stdout）—— {ok, screenshot, ms, foreground{before,during,after,unchanged}, expect:[...]}
退出码：0=成功且(如给定)预期文字全部命中；1=截图成功但预期文字未全命中；2=出错
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 浏览器定位
# ---------------------------------------------------------------------------
def find_browser():
  """返回 (exe路径, 名称)。优先 Edge（Windows 自带），其次 Chrome。"""
  cands = []
  for exe, name in [
    ("msedge.exe", "Edge"),
    ("chrome.exe", "Chrome"),
  ]:
    for base in [
      os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
      os.environ.get("PROGRAMFILES", r"C:\Program Files"),
      os.environ.get("LOCALAPPDATA", ""),
    ]:
      if not base:
        continue
      sub = "Microsoft\\Edge\\Application" if name == "Edge" else "Google\\Chrome\\Application"
      p = os.path.join(base, sub, exe)
      if os.path.exists(p):
        cands.append((p, name))
  # PATH 兜底
  for exe in ["msedge.exe", "chrome.exe"]:
    p = shutil.which(exe)
    if p:
      cands.append((p, "Edge" if "edge" in p.lower() else "Chrome"))
  return cands[0] if cands else (None, None)


# ---------------------------------------------------------------------------
# 前台窗口采样（证明"没抢焦点"）
# ---------------------------------------------------------------------------
def foreground_title():
  try:
    user32 = ctypes.windll.user32
    h = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, buf, 256)
    return buf.value
  except Exception:
    return ""


# ---------------------------------------------------------------------------
# 无头截图
# ---------------------------------------------------------------------------
def headless_shot(browser, url, out_png, width, height, vtime, dsf=1.0):
  """headless 渲染并截图。返回 (ok, err)。"""
  profile = os.path.join(os.environ.get("TEMP", HERE), "moyun_headless_profile")
  os.makedirs(profile, exist_ok=True)
  cmd = [
    browser,
    "--headless=new",
    f"--user-data-dir={profile}",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--hide-scrollbars",
    f"--window-size={width},{height}",
    f"--force-device-scale-factor={dsf}",
    f"--screenshot={out_png}",
    f"--virtual-time-budget={vtime}",
    url,
  ]
  try:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    err = (proc.stderr or "")[-300:]
  except subprocess.TimeoutExpired:
    return False, "headless 渲染超时（60s）"
  if not os.path.exists(out_png) or os.path.getsize(out_png) < 2000:
    # 新版 headless 失败时回退旧 headless 语法
    cmd[1] = "--headless"
    try:
      subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
      return False, "headless 渲染超时（60s，回退模式）"
  if os.path.exists(out_png) and os.path.getsize(out_png) >= 2000:
    return True, err
  return False, f"截图未生成或过小。stderr 末尾: {err}"


# ---------------------------------------------------------------------------
# OCR 核对（复用 findtext 的识别能力）
# ---------------------------------------------------------------------------
def ocr_check(png, expects):
  # findtext.py 与本脚本同目录（tools/）——显式加入 sys.path，不依赖调用方 CWD，
  # 也不依赖任何外部工程的目录结构（此前的写法指向一个不存在的外部目录，属歪打正着）
  if HERE not in sys.path:
    sys.path.insert(0, HERE)
  try:
    from findtext import ocr_image_file, norm_text, fold_confusion
  except Exception as e:
    return {"available": False, "error": str(e), "results": []}
  try:
    blocks = ocr_image_file(png)
  except Exception as e:
    return {"available": False, "error": f"OCR 失败: {e}", "results": []}
  joined = norm_text("".join(b["text"] for b in blocks))
  results = []
  for text in expects:
    nq, fq = norm_text(text), fold_confusion(norm_text(text))
    hit = nq in joined or fq in fold_confusion(joined)
    # 找到最接近的块供展示
    best = ""
    for b in blocks:
      if norm_text(text) in norm_text(b["text"]):
        best = b["text"]
        break
    results.append({"expect": text, "found": bool(hit), "match_block": best})
  return {"available": True, "blocks": len(blocks), "results": results}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
  if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

  p = argparse.ArgumentParser(description="HTML/网页 无头验收（零弹窗零抢焦点）")
  p.add_argument("target", help="要验收的 HTML 文件路径（本地文件）")
  p.add_argument("--out", help="截图输出路径（默认：目标同目录 验收截图/<名>-headless-<时间戳>.png）")
  p.add_argument("--expect", help='页面应包含的文字，逗号分隔，如 "墨韵,番茄钟"（OCR 核对）')
  p.add_argument("--width", type=int, default=1440)
  p.add_argument("--height", type=int, default=900)
  p.add_argument("--vtime", type=int, default=4000, help="虚拟时间预算毫秒（等 JS 渲染），默认 4000")
  p.add_argument("--dsf", type=float, default=1.0, help="设备缩放因子（1=标准 2=两倍分辨率，小字 OCR 更稳）")
  p.add_argument("--query-string", help='URL 查询串（附加到 file:// URL，用于带参数的页面），如 "days=90&hours=2"')
  args = p.parse_args()

  target = os.path.abspath(args.target)
  if not os.path.exists(target):
    print(json.dumps({"ok": False, "error": f"目标文件不存在: {target}"}))
    sys.exit(2)
  browser, bname = find_browser()
  if not browser:
    print(json.dumps({"ok": False, "error": "未找到 Edge/Chrome，无法无头验收",
                      "hint": "安装 Edge/Chrome，或改用 playwright 方案"}))
    sys.exit(2)

  url = urllib.parse.quote("file:///" + target.replace("\\", "/"), safe="/:.")
  if getattr(args, "query_string", None):
    url += "?" + urllib.parse.quote(args.query_string, safe="=&%")

  if args.out:
    out_png = os.path.abspath(args.out)
    parent = os.path.dirname(out_png)
    if parent:
      os.makedirs(parent, exist_ok=True)  # 输出目录不存在时自动创建（headless 写文件失败的头号原因）
  else:
    out_dir = os.path.join(os.path.dirname(target), "验收截图")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(target))[0]
    out_png = os.path.join(out_dir, f"{stem}-headless-{time.strftime('%H%M%S')}.png")

  fg_before = foreground_title()

  t0 = time.time()
  ok, err = headless_shot(browser, url, out_png, args.width, args.height, args.vtime, args.dsf)
  ms = int((time.time() - t0) * 1000)
  fg_mid = foreground_title()
  fg_after = foreground_title()

  out = {
    "ok": ok and os.path.exists(out_png),
    "screenshot": out_png if ok else None,
    "ms": ms,
    "browser": bname,
    "headless": True,
    "foreground": {"before": fg_before, "during": fg_mid, "after": fg_after,
                   "unchanged": fg_before == fg_after == fg_mid},
    "error": None if ok else err,
  }

  # OCR 核对预期文字
  if ok and args.expect:
    expects = [x.strip() for x in args.expect.split(",") if x.strip()]
    check = ocr_check(out_png, expects)
    out["expect_check"] = check
    if check.get("available"):
      allhit = all(r["found"] for r in check["results"])
      out["ok"] = out["ok"] and allhit

  print(json.dumps(out, ensure_ascii=False, indent=2))
  sys.exit(0 if out["ok"] else (1 if ok else 2))


if __name__ == "__main__":
  main()

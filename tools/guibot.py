#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guibot.py — GUI 自动化编排引擎（给 AI 的"找字→等待→点击→输入→断言"流程运行器）
================================================================================

用法：
  python guibot.py 流程.json            # 执行流程（stdout 纯 JSON，日志走 stderr）
  python guibot.py --check 流程.json     # 只校验流程定义，不执行
  python guibot.py --vars '{"k":"v"}' 流程.json   # 注入初始变量

流程定义（JSON）：
{
  "name": "流程名",
  "variables": {"greeting": "你好"},
  "on_fail": "abort",                    // abort(默认，截图后中止) | continue(记录后继续)
  "steps": [
    {"action": "activate",  "window": "ZCode"},
    {"action": "find",      "text": "开始", "window": "ZCode"},
    {"action": "wait",      "text": "完成", "timeout": 10, "mode": "appear"},
    {"action": "wait",      "text": "加载中", "mode": "disappear", "timeout": 10},
    {"action": "click",     "text": "开始", "retry": 2},
    {"action": "click",     "x": 100, "y": 200, "double": true},
    {"action": "type",      "text": "你好 ${greeting}"},
    {"action": "key",       "keys": "ctrl+s"},
    {"action": "key",       "keys": "enter"},
    {"action": "assert",    "text": "保存成功", "mode": "present"},
    {"action": "assert",    "text": "错误", "mode": "absent"},
    {"action": "screenshot","path": "shot.png"},
    {"action": "sleep",     "sec": 1.5},
    {"action": "set",       "name": "note", "value": "${last.text}"},
    {"action": "run",       "cmd": "echo hello", "timeout": 10},
    {"action": "if",        "exists": "登录", "then": [{"action":"click","text":"登录"}],
                            "else": [{"action":"click","text":"注册"}]},
    {"action": "find",      "text": "X", "retry": 3, "on_fail": "continue"}
  ]
}

变量：${名称} 引用；内置 ${last.x} / ${last.y} / ${last.text}（最近一次 find/click 的结果）。
退出码：0=全部成功；1=流程执行完但有失败步骤/断言；2=用法或 IO 错误。
"""
import argparse
import ctypes
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# DPI 感知（坐标物理像素）
# ---------------------------------------------------------------------------
def set_dpi_aware():
  try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
  except Exception:
    try:
      ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
      pass


set_dpi_aware()

# ---------------------------------------------------------------------------
# findtext 自动探测与导入（OCR 能力复用）
# ---------------------------------------------------------------------------
def locate_findtext():
  """定位 findtext.py：同目录 → 上级 tools/ → 当前工作目录 → 环境变量 FINDTEXT_PATH"""
  cands = [
    os.path.join(HERE, "findtext.py"),
    os.path.join(HERE, "..", "tools", "findtext.py"),
    os.path.join(os.getcwd(), "findtext.py"),
    os.environ.get("FINDTEXT_PATH", ""),
  ]
  for c in cands:
    if c and os.path.exists(c):
      return os.path.abspath(c)
  return None


FT_PATH = locate_findtext()
FT = None
FT_ERR = None
if FT_PATH:
  try:
    spec = importlib.util.spec_from_file_location("findtext_mod", FT_PATH)
    FT = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(FT)
  except Exception as e:
    FT_ERR = f"findtext 导入失败: {e}"
else:
  FT_ERR = "未找到 findtext.py（已搜索：同目录 / 上级 tools/ / 当前工作目录 / 环境变量 FINDTEXT_PATH）"


def require_ft(action):
  if FT is None:
    raise RuntimeError(f"动作 {action} 需要 findtext.py（OCR）。原因: {FT_ERR}")


# ---------------------------------------------------------------------------
# Windows 输入注入（ctypes SendInput，零第三方依赖）
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KEYBDINPUT(ctypes.Structure):
  _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
              ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
              ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
  _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 64)]


class INPUT(ctypes.Structure):
  _fields_ = [("type", ctypes.c_uint), ("union", _INPUTUNION)]


VK = {
  "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
  "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
  "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
  "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
  "win": 0x5B, "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
  "printscreen": 0x2C, "pause": 0x13, "apps": 0x5D,
  "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
}
for i in range(1, 13):
  VK[f"f{i}"] = 0x6F + i  # F1=0x70


def tap_key(vk, mods_down):
  for m in mods_down:
    _key_event(m, 0)
  _key_event(vk, 0)
  _key_event(vk, 2)
  for m in reversed(mods_down):
    _key_event(m, 2)


def _key_event(vk, flags):
  inp = INPUT()
  inp.type = INPUT_KEYBOARD
  inp.union.ki = KEYBDINPUT(wVk=vk, dwFlags=flags)
  user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def type_unicode(text, delay=0.006):
  """UNICODE 方式逐字符输入（支持中文）。"""
  for ch in text:
    if ch == "\n":
      tap_key(VK["enter"], [])
      continue
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    inp.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if delay:
      time.sleep(delay)


def parse_combo(s):
  """'ctrl+shift+s' → (mods列表[VK], 主键列表[VK])"""
  mods, mains = [], []
  for tok in s.split("+"):
    k = tok.strip().lower()
    if not k:
      continue
    vk = VK.get(k)
    if vk is None and len(k) == 1:
      vk = ord(k.upper())
    if vk is None:
      raise ValueError(f"未知按键: {tok}")
    if k in ("ctrl", "control", "alt", "shift", "win"):
      mods.append(vk)
    else:
      mains.append(vk)
  return mods, mains


def do_key_combo(s):
  mods, mains = parse_combo(s)
  if not mains:  # 只有修饰键（如单独 ctrl）→ 按下再抬起
    mains = mods[:]
    mods = []
  for m in mods:
    _key_event(m, 0)
  for k in mains:
    _key_event(k, 0)
    _key_event(k, 2)
  for m in reversed(mods):
    _key_event(m, 2)


MOUSE_LEFT_DOWN, MOUSE_LEFT_UP = 0x0002, 0x0004
MOUSE_RIGHT_DOWN, MOUSE_RIGHT_UP = 0x0008, 0x0010


def click_at(x, y, button="left", clicks=1):
  user32.SetCursorPos(int(x), int(y))
  time.sleep(0.03)
  down, up = ((MOUSE_LEFT_DOWN, MOUSE_LEFT_UP) if button == "left"
              else (MOUSE_RIGHT_DOWN, MOUSE_RIGHT_UP))
  for _ in range(clicks):
    user32.mouse_event(down, 0, 0, 0, None)
    time.sleep(0.02)
    user32.mouse_event(up, 0, 0, 0, None)
    time.sleep(0.06)


# ---------------------------------------------------------------------------
# 流程引擎
# ---------------------------------------------------------------------------
def interp(s, ctx):
  """${名称} 变量替换。"""
  def rep(m):
    key = m.group(1)
    if key in ctx:
      return str(ctx[key])
    return m.group(0)
  return re.sub(r"\$\{([^}]+)\}", rep, str(s))


class FlowError(Exception):
  def __init__(self, message, detail=None):
    super().__init__(message)
    self.detail = detail


class Guibot:
  def __init__(self, flow, global_vars=None, dry=False):
    self.flow = flow
    self.name = flow.get("name", "未命名流程")
    self.on_fail = flow.get("on_fail", "abort")
    self.ctx = {"last": {}}
    self.ctx.update(flow.get("variables", {}) or {})
    if global_vars:
      self.ctx.update(global_vars)
    self.dry = dry
    self.results = []
    self.failed = False
    self.fail_shot = None

  def I(self, v):
    return interp(v, self.ctx)

  def _ocr(self, window=None, region=None):
    require_ft("OCR 类动作")
    if region:
      arr, w, h, off_x, off_y = FT.grab_frame(region=region)
      blocks = FT.to_absolute(FT.ocr_frame(arr), off_x, off_y)
      return blocks
    if window:
      hit = FT.get_window_rect(self.I(window))
      if not hit:
        raise FlowError(f"未找到窗口「{self.I(window)}」")
      hwnd, rect, _ = hit
      FT.activate_window(hwnd)
      arr, w, h, off_x, off_y = FT.grab_frame(region=rect)
      blocks = FT.to_absolute(FT.ocr_frame(arr), off_x, off_y)
      return blocks
    arr, w, h, off_x, off_y = FT.grab_frame()
    return FT.to_absolute(FT.ocr_frame(arr), off_x, off_y)

  # ---------------- 各动作实现（返回 detail dict） ----------------
  def act_find(self, st):
    require_ft("find")
    text = self.I(st.get("text", ""))
    blocks = self._ocr(st.get("window"), st.get("region"))
    hits = FT.match_blocks(blocks, text, mode=st.get("match", "contains"))
    hits.sort(key=lambda b: -b["conf"])
    if not hits:
      raise FlowError(f"未找到文字「{text}」", {"blocks": len(blocks)})
    h = hits[0]
    self.ctx["last"] = {"x": h["cx"], "y": h["cy"], "text": h["text"]}
    return {"text": h["text"], "x": h["cx"], "y": h["cy"], "conf": h["conf"],
            "candidates": len(hits)}

  def act_wait(self, st):
    require_ft("wait")
    text = self.I(st.get("text", ""))
    mode = st.get("mode", "appear")
    timeout = float(st.get("timeout", 10))
    interval = float(st.get("interval", 0.8))
    deadline = time.time() + timeout
    while True:
      blocks = self._ocr(st.get("window"), st.get("region"))
      hits = FT.match_blocks(blocks, text, mode="contains")
      if mode == "appear" and hits:
        h = hits[0]
        self.ctx["last"] = {"x": h["cx"], "y": h["cy"], "text": h["text"]}
        return {"found": True, "text": h["text"], "center": [h["cx"], h["cy"]]}
      if mode == "disappear" and not hits:
        return {"found": False, "gone": True}
      if time.time() >= deadline:
        texts = [b["text"][:30] for b in sorted(blocks, key=lambda x: -x["conf"])[:20]]
        raise FlowError(f"等待「{text}」{mode} 超时（{timeout}s）",
                        {"blocks": len(blocks), "screen_texts": texts})
      time.sleep(interval)

  def act_click(self, st):
    text = self.I(st.get("text")) if st.get("text") else None
    if text:
      require_ft("click(text)")
      blocks = self._ocr(st.get("window"), st.get("region"))
      hits = FT.match_blocks(blocks, text, mode=st.get("match", "contains"))
      hits.sort(key=lambda b: -b["conf"])
      if not hits:
        raise FlowError(f"未找到可点击的文字「{text}」")
      h = hits[0]
      x, y = h["cx"], h["cy"]
      self.ctx["last"] = {"x": x, "y": y, "text": h["text"]}
    else:
      x, y = int(st["x"]), int(st["y"])
      self.ctx["last"] = {"x": x, "y": y}
    if self.dry:
      return {"clicked": [x, y]}
    click_at(x, y, button=st.get("button", "left"), clicks=2 if st.get("double") else 1)
    time.sleep(float(st.get("settle", 0.35)))
    return {"clicked": [x, y]}

  def act_type(self, st):
    text = self.I(st.get("text", ""))
    if self.dry:
      return {"typed": text}
    type_unicode(text, delay=float(st.get("delay", 0.006)))
    time.sleep(0.15)
    return {"typed": text[:60]}

  def act_key(self, st):
    combo = self.I(st.get("keys", ""))
    if self.dry:
      return {"keys": combo}
    do_key_combo(combo)
    time.sleep(0.15)
    return {"keys": combo}

  def act_assert(self, st):
    require_ft("assert")
    text = self.I(st.get("text", ""))
    mode = st.get("mode", "present")
    timeout = float(st.get("timeout", 0))
    deadline = time.time() + timeout
    while True:
      blocks = self._ocr(st.get("window"), st.get("region"))
      hits = FT.match_blocks(blocks, text, mode="contains")
      present = bool(hits)
      if mode == "present" and present:
        return {"assert": "present", "text": text, "ok": True, "match": hits[0]["text"][:40]}
      if mode == "absent" and not present:
        return {"assert": "absent", "text": text, "ok": True}
      if time.time() >= deadline:
        break
      time.sleep(0.8)
    raise FlowError(f"断言失败：页面{'未出现' if mode == 'present' else '仍存在'}「{text}」",
                    {"blocks": len(blocks)})

  def act_screenshot(self, st):
    require_ft("screenshot")
    path = self.I(st.get("path")) or f"guibot-{time.strftime('%H%M%S')}.png"
    if self.dry:
      return {"screenshot": path}
    arr, w, h, _, _ = FT.grab_frame()
    FT.save_png(arr, path)
    return {"screenshot": os.path.abspath(path), "size": os.path.getsize(path)}

  def act_sleep(self, st):
    if self.dry:
      return {"sleep": st.get("sec")}
    time.sleep(float(st.get("sec", 1)))
    return {"slept": st.get("sec")}

  def act_run(self, st):
    cmd = self.I(st.get("cmd", ""))
    timeout = float(st.get("timeout", 30))
    if st.get("wait") is False:  # 启动 GUI 程序等不退出的命令：不等待
      subprocess.Popen(cmd, shell=True)
      time.sleep(float(st.get("settle", 1.5)))
      return {"launched": cmd[:60], "wait": False}
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "")[-400:]
    return {"returncode": proc.returncode, "stdout_tail": out}

  def act_activate(self, st):
    require_ft("activate")
    w = self.I(st.get("window", ""))
    hit = FT.get_window_rect(w)
    if not hit:
      raise FlowError(f"未找到窗口「{w}」")
    ok = FT.activate_window(hit[0])
    time.sleep(float(st.get("settle", 0.3)))
    return {"activated": ok, "window": hit[2][:40]}

  def act_set(self, st):
    name = st.get("name", "")
    val = self.I(st.get("value", ""))
    self.ctx[name] = val
    return {"set": name}

  def act_if(self, st):
    require_ft("if")
    if "exists" in st:
      cond = bool(FT.match_blocks(self._ocr(st.get("window"), st.get("region")),
                                  self.I(st["exists"]), mode="contains"))
    elif "absent" in st:
      cond = not FT.match_blocks(self._ocr(st.get("window"), st.get("region")),
                                 self.I(st["absent"]), mode="contains")
    else:
      raise FlowError("if 步骤需要 exists 或 absent 字段")
    branch = st.get("then") if cond else st.get("else")
    if not branch:
      return {"cond": cond, "ran": 0}
    r = self.run_steps(branch, prefix=st.get("_prefix", "if"))
    return {"cond": cond, "sub": r}

  ACTIONS = {
    "find": act_find, "wait": act_wait, "click": act_click, "type": act_type,
    "key": act_key, "assert": act_assert, "screenshot": act_screenshot,
    "sleep": act_sleep, "run": act_run, "activate": act_activate,
    "set": act_set, "if": act_if,
  }

  # ---------------- 步骤执行（含 retry / on_fail） ----------------
  def run_steps(self, steps, prefix=""):
    results = []
    for i, st in enumerate(steps, 1):
      action = st.get("action")
      label = st.get("step_name") or action
      if action == "if":
        st["_prefix"] = f"{prefix}{i}."
      t0 = time.time()
      retry = int(st.get("retry", 1))
      rec = {"step": f"{prefix}{i}", "action": action, "ok": False, "ms": 0}
      last_err = None
      for attempt in range(1, max(1, retry) + 1):
        try:
          fn = self.ACTIONS.get(action)
          if fn is None:
            raise FlowError(f"未知动作: {action}（支持: {', '.join(sorted(self.ACTIONS))}）")
          detail = fn(self, st) if action != "if" else self.ACTIONS["if"](self, st)
          rec.update(ok=True, ms=int((time.time() - t0) * 1000),
                     attempt=attempt, detail=detail)
          break
        except Exception as e:
          last_err = f"{type(e).__name__}: {e}"
          if attempt < max(1, retry):
            time.sleep(0.8)
      else:
        pass
      if not rec.get("ok"):
        rec["ms"] = int((time.time() - t0) * 1000)
        rec["attempt"] = retry
        rec["error"] = last_err
        self.failed = True
        self._fail_screenshot(i, label)
        if self.on_fail != "continue":
          rec["aborted"] = True
          self.results.append(rec)
          raise FlowError(f"第 {i} 步（{label}）失败: {last_err}",
                          {"results": self.results + [rec]})
      print(f"[guibot] {prefix}{i} {action}: {'OK' if rec.get('ok') else '失败'}", file=sys.stderr)
      self.results.append(rec)
    return len(steps)

  def _fail_screenshot(self, step_no, label):
    if FT is None or self.dry:
      return
    try:
      p = self.fail_shot or os.path.join(
        HERE, f"{self.name}-失败-第{step_no}步-{time.strftime('%H%M%S')}.png")
      arr, w, h, _, _ = FT.grab_frame()
      FT.save_png(arr, p)
      self.fail_shot = os.path.abspath(p)
    except Exception:
      pass

  # ---------------- 整体执行 ----------------
  def run_flow(self):
    steps = self.flow.get("steps", [])
    t0 = time.time()
    aborted = None
    try:
      self.run_steps(steps)
    except FlowError as e:
      aborted = str(e)
    ms = int((time.time() - t0) * 1000)
    ok = (not self.failed) and aborted is None
    out = {
      "ok": ok,
      "flow": self.name,
      "steps_total": len(steps),
      "steps_exec": len(self.results),
      "results": self.results,
      "ms": ms,
      "context": {"last": self.ctx.get("last", {})},
    }
    if aborted:
      out["aborted"] = aborted
      out["screenshot"] = self.fail_shot
    return out, (0 if ok else 1)


# ---------------------------------------------------------------------------
# 校验与 CLI
# ---------------------------------------------------------------------------
ALLOWED = set(Guibot.ACTIONS.keys()) | {"action", "text", "keys", "x", "y", "cmd", "value", "path", "sec", "name", "retry", "step_name", "on_fail", "timeout",
                                        "interval", "mode", "window", "region",
                                        "settle", "double", "button", "delay",
                                        "match", "min_conf", "then", "else",
                                        "exists", "absent", "_prefix"}


def validate(flow):
  errs = []
  if not isinstance(flow, dict):
    return ["流程根必须是 JSON 对象"]
  if not isinstance(flow.get("steps", []), list) or not flow.get("steps"):
    return ["steps 必须是非空数组"]
  for i, st in enumerate(flow["steps"], 1):
    if not isinstance(st, dict):
      errs.append(f"步骤{i} 必须是对象")
      continue
    a = st.get("action")
    if a not in Guibot.ACTIONS:
      errs.append(f"步骤{i}: 未知动作 {a!r}（支持: {', '.join(sorted(Guibot.ACTIONS))}）")
    for k in st:
      if k not in ALLOWED:
        errs.append(f"步骤{i}: 未知字段 {k!r}")
  return errs


def main():
  if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

  p = argparse.ArgumentParser(description="guibot — GUI 自动化流程引擎（stdout 纯 JSON）")
  p.add_argument("flow", nargs="?", help="流程 JSON 文件")
  p.add_argument("--check", action="store_true", help="只校验流程定义，不执行")
  p.add_argument("--vars", help='初始变量 JSON，如 \'{"user":"tom"}\'')
  p.add_argument("--dry", action="store_true", help="演练：插值/解析但不真正点击输入")
  p.add_argument("--list-actions", action="store_true", help="列出全部动作")
  args = p.parse_args()

  if args.list_actions:
    print(json.dumps({"actions": sorted(Guibot.ACTIONS.keys())}, ensure_ascii=False, indent=1))
    sys.exit(0)

  if not args.flow:
    p.error("需要流程 JSON 文件（或 --list-actions）")

  try:
    flow = json.load(open(args.flow, encoding="utf-8"))
  except Exception as e:
    print(json.dumps({"ok": False, "error": f"流程文件读取失败: {e}"}, ensure_ascii=False))
    sys.exit(2)

  errs = validate(flow)
  if errs:
    print(json.dumps({"ok": False, "error": "流程定义校验失败", "errors": errs}, ensure_ascii=False, indent=1))
    sys.exit(2)

  if args.check:
    print(json.dumps({"ok": True, "checked": args.flow, "steps": len(flow.get("steps", []))},
                     ensure_ascii=False, indent=1))
    sys.exit(0)

  gvars = {}
  if args.vars:
    try:
      gvars = json.loads(args.vars)
    except Exception:
      print(json.dumps({"ok": False, "error": "--vars 不是有效 JSON"}, ensure_ascii=False))
      sys.exit(2)

  if FT is None and any(st.get("action") not in ("sleep", "set", "run") for st in flow.get("steps", [])):
    print(json.dumps({"ok": False, "error": FT_ERR or "未找到 findtext.py（OCR 依赖）",
                      "hint": "把 findtext.py 放到 guibot.py 同目录（tools/）即可，或用环境变量 FINDTEXT_PATH 指定"},
                     ensure_ascii=False))
    sys.exit(2)

  bot = Guibot(flow, global_vars=gvars, dry=args.dry)
  out, code = bot.run_flow()
  print(json.dumps(out, ensure_ascii=False, indent=2))
  sys.exit(code)


if __name__ == "__main__":
  main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vlocate.py —— 混合定位：让大模型圈范围，让本地算法定准星（v2.0）

设计原则
--------
视觉大模型（VLM）能看懂"那是什么"，但它的坐标误差可达几十像素，**不够用来点击**；
OCR 与模板匹配给出的是像素级坐标，却看不懂语义。vlocate 把两者串成一条链，
并严格分层——**越贵的越少用，前面命中就不往下走**：

    第 0 层  坐标记忆          （调用方持有，本工具不占）   ~1 ms
    第 1 层  模板匹配 tmatch    图标/图形元素，像素级+"相似度分"    ~4 ms    0 token
    第 2 层  锚点+模板          文字锚点定位 + 模板确认（双证）      ~200 ms  0 token
    第 3 层  VLM 圈范围→本地定位 前三层都失败时才用，只负责缩小范围   5~7 s    消耗

**VLM 的输出永远不会被直接当作点击坐标。**

置信度分级（调用方必须看这个字段）
----------------------------------
  high    有模板匹配验证（verified=true）——像素级 + 可量化相似度，可以直接点击
  medium  OCR 锚点 + 几何偏移——坐标精确，但缺少图形确认，同名多匹配时有风险
  low     纯 OCR 直搜——**不可靠**（OCR 会认错字、同名多处会全部返回），
          仅在无模板无锚点时的兜底，请优先补 --template 或 --anchor

用法
----
  # 1) 有图标模板 + 文字锚点（推荐：最快最稳，零 token）
  python vlocate.py --anchor "设置" --template 齿轮.png

  # 2) 只给区域和模板（例如上层已经圈好了范围）
  python vlocate.py --region 1200,800,1800,1100 --template 播放.png

  # 3) 文字锚点 + 相对偏移（按钮在"密码"右边一格）
  python vlocate.py --anchor "密码" --offset 1,0

  # 4) 纯自然语言（需要配 VLM，作兜底）
  python vlocate.py --target "那个红色的提交按钮"

  # 5) 完全不知道叫什么：列出全部候选，让上层 AI 自己挑（零模型）
  python vlocate.py --list-all

  # 6) 命中后直接点击
  python vlocate.py --anchor "发送" --template 纸飞机.png --click

退出码：0=命中 1=未命中 2=用法/环境错误
"""
import argparse, base64, ctypes, json, os, re, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ---------------------------------------------------------------------------
# 配置：VLM 端点（从 paths.json / 环境变量读，工具内不硬编码任何厂商）
# ---------------------------------------------------------------------------
def load_cfg():
    for name in ("paths.json", "paths.example.json"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


CFG = load_cfg()
_VLM = CFG.get("vlm") or {}
VLM_BASE = os.environ.get("VLM_BASE") or _VLM.get("baseURL") or ""
VLM_MODEL = os.environ.get("VLM_MODEL") or _VLM.get("model") or ""
VLM_KEY_ENV = _VLM.get("apiKeyEnv") or "VLM_API_KEY"
VLM_KEY = os.environ.get(VLM_KEY_ENV) or os.environ.get("VLM_API_KEY") or ""
VLM_READY = bool(VLM_BASE and VLM_MODEL and VLM_KEY)
VLM_LAST_ERR = None

STOPWORDS = ["按钮", "按扭", "图标", "链接", "输入框", "搜索框", "标签", "选项卡", "表单",
             "那个", "这个", "屏幕上的", "页面上的", "button", "icon", "link"]


def out(obj, code=0):
    obj.setdefault("tool", "vlocate")
    obj.setdefault("ok", code == 0)
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def keyword_of(target, explicit=None):
    """从自然语言目标里抽出可以拿去 OCR 的关键词。"""
    if explicit:
        return explicit
    if not target:
        return ""
    s = target.strip()
    for w in STOPWORDS:
        s = s.replace(w, " ")
    s = re.sub(r"[，。！,.!?\"'（）()【】\[\]]", " ", s).strip()
    parts = [p for p in s.split() if p]
    return max(parts, key=len) if parts else ""


# ---------------------------------------------------------------------------
# VLM 调用（OpenAI 兼容；失败原因记入 VLM_LAST_ERR，绝不静默吞掉）
# ---------------------------------------------------------------------------
def vlm_ask(png_path, prompt, timeout=60):
    global VLM_LAST_ERR
    VLM_LAST_ERR = None
    if not VLM_READY:
        VLM_LAST_ERR = "未配置 VLM（需要 VLM_BASE / VLM_MODEL / 密钥环境变量）"
        return None
    try:
        import requests
    except ImportError as e:
        VLM_LAST_ERR = "requests 未安装: %s" % e
        return None
    try:
        with open(png_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        VLM_LAST_ERR = "读取图片失败: %s" % e
        return None
    try:
        r = requests.post(VLM_BASE.rstrip("/") + "/chat/completions", timeout=timeout,
                          headers={"Authorization": "Bearer " + VLM_KEY},
                          json={"model": VLM_MODEL, "temperature": 0, "max_tokens": 400,
                                "messages": [{"role": "user", "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url",
                                     "image_url": {"url": "data:image/png;base64," + b64}}]}]})
        if r.status_code != 200:
            VLM_LAST_ERR = "HTTP %s: %s" % (r.status_code, r.text[:300])
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        VLM_LAST_ERR = "%s: %s" % (type(e).__name__, e)
        return None


def parse_vlm_json(text):
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 几何/工具
# ---------------------------------------------------------------------------
def block_bbox(b):
    """兼容多种块结构：{left,top,right,bottom}（findtext）/ {bbox:[..]} / {x,y,w,h}"""
    if b.get("bbox"):
        x0, y0, x1, y1 = b["bbox"]
        return int(x0), int(y0), int(x1), int(y1)
    if "left" in b:
        return int(b["left"]), int(b["top"]), int(b["right"]), int(b["bottom"])
    x, y = int(b.get("x", 0)), int(b.get("y", 0))
    w, h = int(b.get("w", 0)), int(b.get("h", 0))
    return x, y, x + w, y + h


def clamp_region(r, w, h):
    x0, y0, x1, y1 = r
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(w, int(x1)), min(h, int(y1))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return [x0, y0, x1, y1]


def window_around(cx, cy, win, w, h):
    r = [cx - win // 2, cy - win // 2, cx + win // 2, cy + win // 2]
    return clamp_region(r, w, h)


def expand(region, factor, w, h):
    x0, y0, x1, y1 = region
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) * factor / 2, (y1 - y0) * factor / 2
    return clamp_region([cx - hw, cy - hh, cx + hw, cy + hh], w, h)


def click_at(x, y):
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.03)
    u.mouse_event(0x0004, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="vlocate.py",
                                description="混合定位：VLM 圈范围 + OCR/模板定准星",
                                epilog="置信度 high=模板验证过 / medium=锚点偏移 / low=纯OCR(不可靠)")
    p.add_argument("--target", help="自然语言目标（如 \"发送按钮\"）")
    p.add_argument("--text", help="显式 OCR 关键词（跳过从 target 抽词）")
    p.add_argument("--anchor", help="锚点文字（用它来定位目标附近，推荐与 --template 同用）")
    p.add_argument("--template", help="模板图路径（图标/图形元素，tmatch 匹配）")
    p.add_argument("--offset", help="锚点到目标的相对偏移，如 \"1,0\" 表示右边一格或 dx,dy 像素")
    p.add_argument("--region", help="显式搜索区域 x0,y0,x1,y1（外部粗定位插槽）")
    p.add_argument("--window", type=int, default=320, help="锚点周围搜索窗口边长（默认 320）")
    p.add_argument("--threshold", type=float, default=0.8, help="模板匹配阈值（默认 0.8，别低于 0.6）")
    p.add_argument("--image", help="用给定图片代替实时截屏")
    p.add_argument("--monitor", default="primary", help="primary|all|<数字>")
    p.add_argument("--no-vlm", action="store_true", help="禁止调用 VLM（纯本地）")
    p.add_argument("--no-verify", action="store_true", help="跳过命中后的 VLM 复核")
    p.add_argument("--reticle", type=float, default=1.5, help="未命中时区域放大倍数")
    p.add_argument("--retries", type=int, default=2, help="最大重试次数")
    p.add_argument("--click", action="store_true", help="命中后直接点击")
    p.add_argument("--list-all", action="store_true", help="列出全部 OCR 候选后退出")
    p.add_argument("--out-dir", help="中间产物目录（默认临时目录）")
    a = p.parse_args()

    if not (a.target or a.anchor or a.list_all or a.template):
        out({"ok": False, "error": "至少需要 --target / --anchor / --template / --list-all 之一",
             "examples": ["--anchor \"设置\" --template 齿轮.png",
                          "--region 1200,800,1800,1100 --template 播放.png",
                          "--target \"那个红色按钮\"",
                          "--list-all"]}, 2)

    try:
        import findtext as FT
        import cv2
    except Exception as e:
        out({"ok": False, "error": "无法导入 findtext/cv2（需同目录 findtext.py 与 opencv）: %s" % e}, 2)

    TM = None
    templ_quality = None
    if a.template:
        try:
            import tmatch as TM
        except Exception as e:
            out({"ok": False, "error": "无法导入 tmatch（--template 需要）: %s" % e}, 2)
        _t = TM.imread_cn(a.template)
        if _t is None:
            out({"ok": False, "error": "--template 无法读取: %s" % a.template}, 2)
        # 模板质量预检：纯色/近纯色模板在灰度上无方差，归一化相关会给出满分误匹配。
        # 这是实测踩过的坑，必须在源头拦一下。
        try:
            import numpy as _np
            _g = cv2.cvtColor(_t, cv2.COLOR_BGR2GRAY).astype("float32")
            _std = float(_g.std())
            templ_quality = {"std": round(_std, 2), "size": [_t.shape[1], _t.shape[0]]}
            if _std < 8.0:
                templ_quality["warning"] = (
                    "模板灰度标准差仅 %.2f（近乎纯色）——纯色模板极易产生满分误匹配，"
                    "请改用带边缘/纹理的图标模板（可用 tmatch collect 从真实截图裁剪）" % _std)
        except Exception as _e:
            templ_quality = {"error": str(_e)[:120]}

    out_dir = a.out_dir or tempfile.mkdtemp(prefix="vlocate-")
    os.makedirs(out_dir, exist_ok=True)
    t_start = time.time()
    timings = {}

    # ---------- 1. 取图 ----------
    if a.image:
        import numpy as np
        arr = cv2.imdecode(np.fromfile(a.image, dtype="uint8"), cv2.IMREAD_COLOR)
        if arr is None:
            out({"ok": False, "error": "无法读取图片: %s" % a.image}, 2)
        img_h, img_w = arr.shape[:2]
        off_x = off_y = 0
        full_png = a.image
        source = "image"
    else:
        mon = int(a.monitor) if str(a.monitor).isdigit() else a.monitor
        arr, img_w, img_h, off_x, off_y = FT.grab_frame(monitor=mon)
        full_png = os.path.join(out_dir, "screen.png")
        FT.save_png(arr, full_png)
        source = "screen"
    timings["grab_ms"] = int((time.time() - t_start) * 1000)

    def ocr_of(region=None, tag="full"):
        """对整图或指定区域 OCR，返回 (blocks, 区域原点)。结果按区域缓存。"""
        key = tuple(region) if region else None
        if key in ocr_cache:
            return ocr_cache[key]
        if region is None:
            blocks = FT.ocr_frame(arr, use_cls=False)
            origin = (0, 0)
        else:
            x0, y0, x1, y1 = region
            sub_png = os.path.join(out_dir, "region-%d-%d-%d-%d.png" % (x0, y0, x1, y1))
            FT.save_png(arr[y0:y1, x0:x1], sub_png)
            blocks = FT.ocr_image_file(sub_png, use_cls=False)
            origin = (x0, y0)
        ocr_cache[key] = (blocks, origin)
        return blocks, origin

    ocr_cache = {}
    t0 = time.time()
    keyword = keyword_of(a.target, a.text)
    attempts = []
    anchor_info = None
    coarse = None
    precise = None

    def pack(b, origin):
        x0, y0, x1, y1 = block_bbox(b)
        return {"text": b.get("text", ""), "conf": round(float(b.get("conf", 0)), 4),
                "bbox": [x0 + origin[0], y0 + origin[1], x1 + origin[0], y1 + origin[1]],
                "center": [(x0 + x1) // 2 + origin[0], (y0 + y1) // 2 + origin[1]]}

    # ---------- 2. 候选清单模式（零模型，供上层 AI 自己挑）----------
    if a.list_all:
        search = None
        if a.region:
            try:
                search = clamp_region([int(v) for v in a.region.split(",")], img_w, img_h)
            except ValueError:
                out({"ok": False, "error": "--region 需要 x0,y0,x1,y1 四个整数"}, 2)
        blocks, origin = ocr_of(search)
        cands = sorted((pack(b, origin) for b in blocks), key=lambda c: -c["conf"])
        out({"ok": True, "mode": "candidates", "source": source,
             "screen": {"width": img_w, "height": img_h, "offset": [off_x, off_y]},
             "region": search, "count": len(cands), "candidates": cands,
             "hint": "把这张清单交给上层 AI 挑选即可——纯文本 AI 也能处理，不需要看图",
             "ms": int((time.time() - t_start) * 1000)}, 0)

    # ---------- 3. 确定搜索区域 ----------
    search_region = None
    if a.region:
        try:
            search_region = clamp_region([int(v) for v in a.region.split(",")], img_w, img_h)
        except ValueError:
            out({"ok": False, "error": "--region 需要 x0,y0,x1,y1 四个整数"}, 2)
        if search_region is None:
            out({"ok": False, "error": "--region 无效或过小"}, 2)

    if a.anchor:
        t0 = time.time()
        blocks, origin = ocr_of(search_region)
        timings["anchor_ocr_ms"] = int((time.time() - t0) * 1000)
        hits = FT.match_blocks(blocks, a.anchor, mode="contains")
        if not hits:
            hits = FT.match_blocks(blocks, a.anchor, mode="fuzzy")
        if hits:
            b = max(hits, key=lambda x: float(x.get("conf", 0)))
            anchor_info = pack(b, origin)
            anchor_info["query"] = a.anchor
            ax0, ay0, ax1, ay1 = block_bbox(b)
            acx, acy = (ax0 + ax1) // 2 + origin[0], (ay0 + ay1) // 2 + origin[1]
            search_region = window_around(acx, acy, a.window, img_w, img_h)
            attempts.append({"step": "anchor", "query": a.anchor, "found": True,
                             "center": [acx, acy], "window": search_region})
        else:
            attempts.append({"step": "anchor", "query": a.anchor, "found": False})

    # ---------- 4. 第 1/2 层：模板匹配（主力，零 token）----------
    hit = None
    method = None
    verified = False

    if a.template and TM is not None:
        t0 = time.time()
        templ = TM.imread_cn(a.template)
        regions_to_try = [search_region] if search_region else [None]
        for reg in regions_to_try:
            if reg is None:
                img_for_match = arr
                rorigin = (0, 0)
            else:
                x0, y0, x1, y1 = reg
                img_for_match = arr[y0:y1, x0:x1]
                rorigin = (x0, y0)
            m = TM.find_one(img_for_match, templ, threshold=a.threshold)
            if m and m.get("conf", 0) >= a.threshold:
                abs_cx, abs_cy = m["cx"] + rorigin[0], m["cy"] + rorigin[1]
                hit = {"cx": abs_cx, "cy": abs_cy, "conf": m["conf"],
                       "bbox": [m["x"] + rorigin[0], m["y"] + rorigin[1],
                                m["x"] + m["w"] + rorigin[0], m["y"] + m["h"] + rorigin[1]],
                       "scale": m.get("scale"), "source": "tmatch"}
                verified = True
                method = "anchor+tmatch" if anchor_info else ("region+tmatch" if reg else "tmatch")
                break
            attempts.append({"step": "tmatch", "region": reg,
                             "found": False,
                             "best_conf": (m or {}).get("conf")})
        timings["tmatch_ms"] = int((time.time() - t0) * 1000)

    # ---------- 5. 第 2 层备选：锚点 + 几何偏移（无模板）----------
    if hit is None and anchor_info and a.offset:
        try:
            dx, dy = [int(v) for v in a.offset.split(",")]
        except ValueError:
            out({"ok": False, "error": "--offset 需要 dx,dy 两个整数"}, 2)
        ax, ay = anchor_info["center"]
        hit = {"cx": ax + dx, "cy": ay + dy, "conf": None, "bbox": None,
               "source": "anchor+offset"}
        method = "anchor+offset"
        verified = False
        attempts.append({"step": "anchor+offset", "from": [ax, ay], "offset": [dx, dy]})

    # ---------- 6. 第 3 层：VLM 圈范围 → 区域内 OCR（兜底）----------
    if hit is None and keyword:
        vlm_used = False
        if VLM_READY and not a.no_vlm:
            vlm_used = True
            prompt = ("你是屏幕元素定位助手。用户要点击的目标是：%s\n"
                      "请只看这张截图，判断该目标在图中的大致矩形区域，用归一化坐标（0~1，左上为原点）。\n"
                      "严格只输出一个 JSON：\n"
                      '{"found": true, "region": [x0, y0, x1, y1], "text": "元素上的文字（没有就空串）", '
                      '"desc": "元素外观的一句话描述"}' % (a.target or a.anchor))
            raw = vlm_ask(full_png, prompt)
            v = parse_vlm_json(raw)
            if v and v.get("found") and isinstance(v.get("region"), list) and len(v["region"]) == 4:
                r = v["region"]
                reg = clamp_region([r[0] * img_w, r[1] * img_h, r[2] * img_w, r[3] * img_h], img_w, img_h)
                coarse = {"norm": r, "region": reg, "text": v.get("text", ""),
                          "desc": v.get("desc", "")}
                q = (v.get("text") or "").strip() or keyword
                for i in range(max(0, a.retries) + 1):
                    if reg is None:
                        break
                    blocks, origin = ocr_of(reg)
                    hits = FT.match_blocks(blocks, q, mode="contains") if q else []
                    attempts.append({"step": "vlm+ocr", "try": i + 1, "region": reg,
                                     "query": q, "found": bool(hits)})
                    if hits:
                        b = max(hits, key=lambda x: float(x.get("conf", 0)))
                        pk = pack(b, origin)
                        hit = {"cx": pk["center"][0], "cy": pk["center"][1],
                               "conf": pk["conf"], "bbox": pk["bbox"], "source": "ocr"}
                        method = "vlm+ocr"
                        break
                    reg = expand(reg, a.reticle, img_w, img_h)
            else:
                attempts.append({"step": "vlm", "found": False, "raw": (raw or "")[:200],
                                 "error": VLM_LAST_ERR})
        # 纯 OCR 兜底（低置信度，必须警示）
        if hit is None:
            blocks, origin = ocr_of(search_region)
            hits = FT.match_blocks(blocks, keyword, mode="contains")
            attempts.append({"step": "ocr_fallback", "query": keyword,
                             "found": bool(hits), "vlm_used": vlm_used})
            if hits:
                if len(hits) > 1:
                    attempts.append({"step": "ocr_fallback_note",
                                     "note": "OCR 命中 %d 处同名文字，无法区分，结果不可靠" % len(hits)})
                b = max(hits, key=lambda x: float(x.get("conf", 0)))
                pk = pack(b, origin)
                hit = {"cx": pk["center"][0], "cy": pk["center"][1],
                       "conf": pk["conf"], "bbox": pk["bbox"], "source": "ocr",
                       "ambiguous": len(hits)}
                method = "vlm+ocr" if coarse else "ocr_only"
                verified = False

    timings["total_ms"] = int((time.time() - t_start) * 1000)

    if hit is None:
        out({"ok": False, "method": "none", "verified": False, "confidence": "none",
             "target": a.target, "keyword": keyword, "vlm_ready": VLM_READY,
             "vlm_error": VLM_LAST_ERR, "coarse": coarse, "anchor": anchor_info,
             "attempts": attempts, "timings_ms": timings, "template_quality": templ_quality,
             "hint": "三条路：① --anchor \"已知文字\" ② --template 图标.png ③ --list-all 看候选自己挑",
             "error": "未命中"}, 1)

    conf_level = "high" if verified else ("medium" if anchor_info and method != "ocr_only" else "low")
    warning = None
    if conf_level == "low":
        warning = ("纯 OCR 定位未经图形验证：同名多处会歧义、形近字可能认错。"
                   "建议补 --template（图标模板）或 --anchor（文字锚点）以提高可靠性。")
        if hit.get("ambiguous", 0) > 1:
            warning += " 本次命中 %d 处同名文字，取的是置信度最高的一处，请务必人工/上层复核。" % hit["ambiguous"]

    result = {
        "ok": True, "method": method, "verified": verified, "confidence": conf_level,
        "target": a.target, "keyword": keyword,
        "source": source, "screen": {"width": img_w, "height": img_h, "offset": [off_x, off_y]},
        "click": [hit["cx"], hit["cy"]],
        "precise": {"source": hit["source"], "conf": hit["conf"], "bbox": hit["bbox"]},
        "anchor": anchor_info, "coarse": coarse, "attempts": attempts,
        "template_quality": templ_quality,
        "timings_ms": timings, "vlm_ready": VLM_READY, "vlm_error": VLM_LAST_ERR,
        "warning": warning,
        "hint": "坐标为屏幕物理像素；DPI 缩放非 100% 时逻辑坐标 = 物理 / 缩放比",
    }

    # ---------- 7. 自证（可选）----------
    if verified and VLM_READY and not a.no_verify and hit.get("bbox"):
        x0, y0, x1, y1 = hit["bbox"]
        pad = 24
        crop = arr[max(0, y0 - pad):min(img_h, y1 + pad), max(0, x0 - pad):min(img_w, x1 + pad)]
        vpng = os.path.join(out_dir, "verify.png")
        FT.save_png(crop, vpng)
        ans = vlm_ask(vpng, '这张小图里是否包含「%s」？只回答 JSON：{"yes": true/false}'
                      % (a.target or a.anchor or keyword))
        vj = parse_vlm_json(ans) or {}
        result["verify"] = {"crop": vpng, "confirmed": bool(vj.get("yes")), "raw": (ans or "")[:200]}

    if a.click:
        click_at(*result["click"])
        result["clicked"] = True
    out(result, 0)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""示例 2：混合定位 —— 文字锚点 + 模板匹配，双证命中（推荐用法）

    python examples/02_hybrid_locate.py

流程：OCR 找到锚点文字 → 在锚点周围开一个小窗口 → 窗口内做模板匹配 → 命中。
两个独立证据指向同一个位置才算命中，比任何单一方法都稳。

**返回里的 confidence 字段必须先看：**
    high    有模板验证，像素级 + 可量化相似度 → 可以直接点击
    medium  锚点 + 几何偏移，缺少图形确认     → 建议复核
    low     纯 OCR 直搜，同名多处会歧义        → 不可靠，请补模板或锚点
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def main():
    cmd = [PY, os.path.join(ROOT, "tools", "vlocate.py"),
           "--image", os.path.join(ROOT, "fixtures", "sample.png"),
           "--anchor", "工具包冒烟测试",            # 文字锚点：屏幕上一定有的字
           "--template", os.path.join(ROOT, "fixtures", "template.png"),  # 图标模板
           "--window", "1200",                     # 锚点周围搜索窗口边长
           "--no-vlm"]                             # 纯本地，不调任何模型
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not r.stdout.strip():
        print("调用失败：", r.stderr[:400])
        return 2
    d = json.loads(r.stdout)

    if not d.get("ok"):
        print("未命中。可用的诊断信息：")
        print(json.dumps({k: d.get(k) for k in ("hint", "attempts", "vlm_error")},
                         ensure_ascii=False, indent=2)[:1200])
        return 1

    print("定位方法    : %s" % d["method"])
    print("置信度      : %s  (verified=%s)" % (d["confidence"], d["verified"]))
    if d.get("anchor"):
        a = d["anchor"]
        print("锚点        : %r  center=%s" % (a["text"], a["center"]))
    p = d.get("precise", {})
    print("精确来源    : %s  conf=%s" % (p.get("source"), p.get("conf")))
    print("像素级 bbox : %s" % (p.get("bbox"),))
    print("可点击坐标  : %s" % (d["click"],))
    print("各级耗时    : %s" % (d.get("timings_ms"),))
    if d.get("warning"):
        print("!! 警示     : %s" % d["warning"])
    if d.get("template_quality", {}).get("warning"):
        print("!! 模板问题 : %s" % d["template_quality"]["warning"])
    print("-" * 56)
    if d["confidence"] == "high":
        print("可以直接点击。要真的点击：给上面的命令加 --click")
    else:
        print("置信度未达 high，建议先复核，或补一个带边缘纹理的模板。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

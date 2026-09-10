#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke.py —— pixel-locator 冒烟测试（全新克隆即可运行）

特性：
- 只用仓库自带素材（fixtures/），不依赖任何外部工程或本机特定文件
- 不弹窗、不抢焦点、不改动用户桌面、不移动鼠标
- 每个用例独立子进程，返回码 + 期望关键字双判定
- 结果写 冒烟测试记录.json，控制台打印汇总

用法：
  python smoke.py            # 跑全部
  python smoke.py -v         # 打印每个用例的输出片段
  python smoke.py --ci       # CI 模式：只跑不依赖真实桌面/窗口的用例
"""
import json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")
PY = sys.executable
FX = os.path.join(HERE, "fixtures")
RECORD = os.path.join(HERE, "冒烟测试记录.json")

TMP = tempfile.mkdtemp(prefix="pixel-locator-smoke-")
SHOT = os.path.join(TMP, "headless.png")
SAMPLE = os.path.join(FX, "sample.png")
TEMPLATE = os.path.join(FX, "template.png")

# name, script+args, 期望输出中出现的关键字（None = 只看退出码）, 是否能在无桌面的 CI 上跑
CASES = [
    ("vlocate 区域+模板（最快路径，high 置信度）",
     ["vlocate.py", "--image", SAMPLE, "--region", "560,220,800,460", "--template", TEMPLATE],
     '"confidence": "high"', True),
    ("vlocate 锚点+模板（推荐主力）",
     ["vlocate.py", "--image", SAMPLE, "--anchor", "工具包冒烟测试", "--template", TEMPLATE,
      "--window", "1200", "--no-vlm"],
     '"verified": true', True),
    ("vlocate 候选清单（零模型，供上层 AI 挑）",
     ["vlocate.py", "--image", SAMPLE, "--list-all"], '"mode": "candidates"', True),
    ("findtext 找字（像素级坐标）",
     ["findtext.py", "--image", SAMPLE, "--query", "像素级定位"], "像素级定位", True),
    ("findtext 全文识别",
     ["findtext.py", "--image", SAMPLE, "--all"], "工具包", True),
    ("tmatch 模板匹配",
     ["tmatch.py", "find", TEMPLATE, SAMPLE], '"found": true', True),
    ("guibot 动作表",
     ["guibot.py", "--list-actions"], None, True),
    ("guibot 流程结构校验",
     ["guibot.py", os.path.join(HERE, "examples", "04_flow.json"), "--check"], None, True),
    ("headless_check 无头验收（自证不抢焦点）",
     ["headless_check.py", os.path.join(FX, "sample.html"), "--out", SHOT,
      "--expect", "墨韵,番茄钟"], "found", False),
    ("winctl 窗口列表",
     ["winctl.py", "--list"], None, False),
]


def run_case(name, args, expect, verbose):
    script = os.path.join(TOOLS, args[0])
    if not os.path.exists(script):
        return {"case": name, "status": "SKIP", "reason": f"脚本不存在: {args[0]}", "ms": 0}
    t0 = time.time()
    try:
        r = subprocess.run([PY, script] + args[1:], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240, cwd=HERE)
        code, so, se = r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return {"case": name, "status": "FAIL", "reason": "超时 240s", "ms": 240000}
    ms = int((time.time() - t0) * 1000)
    joined = (so + "\n" + se)
    if verbose:
        print(f"      ── {args[0]} 输出片段 ──")
        print("      " + so.strip().replace("\n", "\n      ")[:600])
    if code != 0:
        return {"case": name, "status": "FAIL", "reason": f"退出码 {code}: {se.strip()[:200]}", "ms": ms}
    if expect and expect not in joined:
        return {"case": name, "status": "FAIL", "reason": f"输出未包含期望关键字 {expect!r}", "ms": ms}
    return {"case": name, "status": "PASS", "ms": ms}


def main():
    verbose = "-v" in sys.argv
    only_ci = "--ci" in sys.argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 62)
    print(" pixel-locator 冒烟测试" + ("（CI 模式：跳过需要桌面/窗口的用例）" if only_ci else ""))
    print("=" * 62)

    results = []
    for name, args, expect, ci_ok in CASES:
        if only_ci and not ci_ok:
            print(f"[--] {name}  （--ci 模式跳过：需要真实桌面/窗口）")
            continue
        print(f"[{'..'}] {name}")
        item = run_case(name, args, expect, verbose)
        results.append(item)
        mark = {"PASS": "OK", "FAIL": "XX", "SKIP": "--"}[item["status"]]
        extra = "" if item["status"] == "PASS" else f"  → {item.get('reason', '')}"
        print(f"[{mark}] {name}  ({item.get('ms', 0)}ms){extra}")

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = [r for r in results if r["status"] == "FAIL"]
    skipped = [r for r in results if r["status"] == "SKIP"]
    summary = {"passed": passed, "failed": len(failed), "skipped": len(skipped),
               "total": len(results), "results": results}
    with open(RECORD, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print("-" * 62)
    print(f"通过 {passed}/{len(results)} · 失败 {len(failed)} · 跳过 {len(skipped)}")
    if failed:
        print("失败详情：")
        for r in failed:
            print("  - " + r["case"] + ": " + r.get("reason", ""))
    if skipped:
        print("跳过（不影响核心功能）：")
        for r in skipped:
            print("  - " + r["case"] + ": " + r.get("reason", ""))
    print(f"记录已写入: {RECORD}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

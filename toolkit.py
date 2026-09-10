#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""toolkit.py —— pixel-locator 统一入口

给 AI 精准的屏幕坐标：**视觉模型负责看懂界面，OCR 与模板匹配负责给出像素级落点。**
零 GPU、主路径零 token、可完全不依赖任何视觉大模型运行。

用法：
  python toolkit.py list                      工具索引（JSON）
  python toolkit.py doctor                    环境自检（JSON）
  python toolkit.py paths                     查看 paths.json（JSON）
  python toolkit.py <工具名> [参数...]         调用工具（原样透传退出码）
  python toolkit.py --json <工具名> [参数...]  信封模式：包成 {tool,ok,exit,stdout,stderr,ms}
                                              （工具原生 --json 时直接透传，不二次包裹）

约定：
- 全部工具位于 tools/ 子目录
- 环境常量一律读 paths.json（缺失时自动回退 paths.example.json），工具内不硬编码本机路径
- 退出码：0=成功 1=工具无结果/警告 2=用法错误（本入口与各工具统一）
"""
import json, os, subprocess, sys, time, socket

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(HERE, "tools")
PATHS_FILE = os.path.join(HERE, "paths.json")
EXAMPLE_FILE = os.path.join(HERE, "paths.example.json")
PY = sys.executable

# 工具注册表：调用名 → (脚本, 一句话用途, 别名)
# 本仓库只收录与"精准坐标"直接相关的工具，主题保持聚焦。
TOOLS = {
    "vlocate":        ("vlocate.py",        "混合定位（招牌）：模板/锚点主力 + VLM 兜底，输出像素级可点击坐标 + 置信度分级", ["定位", "nl"]),
    "findtext":       ("findtext.py",       "OCR 找字定位：--query 词（或 --all 全文）→ 像素级 bbox；含内容缓存与守护进程", ["ocr", "找字"]),
    "tmatch":         ("tmatch.py",         "模板匹配：定位无文字元素（图标/图形），像素级 + 相似度分；零 GPU（CUDA 可选自动加速）", ["找图", "模板"]),
    "guibot":         ("guibot.py",         "GUI 流程编排：JSON 声明式流程（找元素/点击/键入/断言），把定位串成可复用动作", ["gui", "流程"]),
    "winctl":         ("winctl.py",         "窗口控制：列表/置顶/移动/免激活后台截图（一切定位的前提能力）", ["窗口"]),
    "headless_check": ("headless_check.py", "无头验收：目标.html --out 截图 --expect 词，OCR 核对并自证全程未抢用户焦点", ["accept", "验收", "后台验收"]),
}
ALIAS = {}
for k, (_, _, al) in TOOLS.items():
    ALIAS[k] = k
    for a in al:
        ALIAS[a] = k


def load_paths():
    """读 paths.json；不存在或损坏时回退 paths.example.json（全新克隆即可运行）。"""
    for cand in (PATHS_FILE, EXAMPLE_FILE):
        if os.path.exists(cand):
            try:
                with open(cand, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.exit(code)


def cmd_list():
    tools = [{"name": k, "script": v[0], "desc": v[1], "aliases": v[2],
              "present": os.path.exists(os.path.join(TOOLS_DIR, v[0]))}
             for k, v in sorted(TOOLS.items())]
    out({"ok": True, "count": len(tools), "tools_dir": TOOLS_DIR, "tools": tools})


def cmd_paths():
    out({"ok": True, "source": PATHS_FILE if os.path.exists(PATHS_FILE) else EXAMPLE_FILE,
         "paths": load_paths()})


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_doctor():
    p = load_paths()
    checks = []

    def rec(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]})

    rec("python版本>=3.10", sys.version_info >= (3, 10), sys.version.split()[0])

    # 必需依赖
    deps = {"PIL": "pillow", "numpy": "numpy", "cv2": "opencv-python",
            "rapidocr_onnxruntime": "rapidocr-onnxruntime", "requests": "requests", "mss": "mss"}
    for mod, pkg in deps.items():
        try:
            __import__(mod)
            rec(f"依赖:{pkg}", True)
        except Exception as e:
            rec(f"依赖:{pkg}", False, str(e)[:80])

    # 工具完整性
    missing = [t for t in TOOLS.values() if not os.path.exists(os.path.join(TOOLS_DIR, t[0]))]
    rec(f"工具完整性({len(TOOLS)})", not missing,
        f"缺{missing}" if missing else f"{len(TOOLS)}个齐全")

    # 可选依赖（缺失不影响核心功能，单列）
    optional = []
    for mod, pkg, why in (("win32com", "pywin32", "个别 COM/窗口增强场景需要"),
                          ("mss", "mss", "全屏截图（findtext/tmatch 屏幕路径需要）")):
        try:
            __import__(mod)
            optional.append({"dep": pkg, "present": True, "why": why})
        except Exception:
            optional.append({"dep": pkg, "present": False, "why": why})

    # 可按需启动的服务：单列 services，不参与 ok 判定
    services = []
    proxy = (p.get("proxy") or {}).get("http", "")
    if proxy and ":" in proxy:
        ph, pp = proxy.rsplit(":", 1)
        on = pp.isdigit() and port_open(ph, int(pp), 0.8)
        services.append({"service": f"代理 {proxy}", "ready": on,
                         "detail": "在线" if on else "未启动（仅联网下载类任务需要）"})
    vlm = p.get("vlm") or {}
    if vlm.get("baseURL"):
        services.append({"service": "VLM 端点（vlocate 第 3 层兜底用，可选）", "ready": None,
                         "detail": vlm.get("baseURL", "") + " / " + str(vlm.get("model", ""))})
    else:
        services.append({"service": "VLM 端点（可选，未配置）", "ready": None,
                         "detail": "未配置不影响使用：vlocate 的模板/锚点/候选清单路径全部本地运行，零 token"})
    # findtext 守护进程（大幅提速项，可选）
    daemon_on = port_open("127.0.0.1", 8377, 0.5)
    services.append({"service": "findtext 守护进程 127.0.0.1:8377（可选，提速约 35 倍）",
                     "ready": daemon_on,
                     "detail": "运行中" if daemon_on else "未启动（启动：python tools/findtext.py --serve）"})

    failed = [x for x in checks if not x["ok"]]
    out({"ok": not failed, "total": len(checks), "failed": [x["check"] for x in failed],
         "services": services, "optional_deps": optional, "checks": checks},
        0 if not failed else 1)


def cmd_run(name, args, envelope):
    real = ALIAS.get(name)
    if not real:
        out({"ok": False, "error": f"未知工具: {name}", "hint": "python toolkit.py list 查看索引"}, 2)
    script = os.path.join(TOOLS_DIR, TOOLS[real][0])
    if not os.path.exists(script):
        out({"ok": False, "error": f"工具脚本缺失: {script}"}, 2)
    cmd = [PY, script] + args
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        code, so, se = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        code, so, se = 1, "", "timeout 600s"
    ms = int((time.time() - t0) * 1000)
    if not envelope:
        sys.stdout.write(so)
        if se:
            sys.stderr.write(se)
        sys.exit(code)
    # 信封模式：若 --json 且工具输出本就是合法 JSON → 原样透传；否则包裹
    if "--json" in args:
        try:
            parsed = json.loads(so)
            if isinstance(parsed, dict):
                parsed.setdefault("_tool", real)
                parsed.setdefault("_ms", ms)
                print(json.dumps(parsed, ensure_ascii=False, indent=2))
                sys.exit(code)
        except (ValueError, TypeError):
            pass
    out({"tool": real, "ok": code == 0, "exit": code, "ms": ms,
         "stdout": so[-8000:], "stderr": se[-2000:]}, code)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    envelope = False
    if args[0] == "--json":
        envelope = True
        args = args[1:]
        if not args:
            print(__doc__)
            sys.exit(2)
    head = args[0]
    if head == "list":
        cmd_list()
    if head == "doctor":
        cmd_doctor()
    if head == "paths":
        cmd_paths()
    cmd_run(head, args[1:], envelope)


if __name__ == "__main__":
    main()

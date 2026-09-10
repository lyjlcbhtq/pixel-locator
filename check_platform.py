# -*- coding: utf-8 -*-
"""check_platform.py —— 跨平台导入自检（静态检查，无需 Linux 环境即可运行）

背景：本项目曾在 CI 上失败，根因是 `tools/findtext.py` 顶层写了
`import ctypes.wintypes` —— 该模块在 Linux / macOS 上不存在，导致
vlocate / guibot 等一并在 Ubuntu 上 import 失败。本脚本就是为了让这类
问题在**提交前**暴露，而不是等到 CI 报红。

它检查两件事：
  1. 模块顶层导入了平台专有模块（且没有 sys.platform 保护）
  2. 模块级（顶层）直接调用 ctypes.WinDLL / ctypes.windll.*
     （若该文件在顶部有「提前退出的平台守卫」，则整体视为已保护）

用法：
  python check_platform.py          # 检查 tools/ 下全部工具
退出码：0=通过，1=发现问题，2=用法错误
"""
import ast
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")

# 平台专有模块：在 Linux / macOS 上不存在
PLATFORM_MODULES = {
    "ctypes.wintypes", "wintypes",
    "win32api", "win32con", "win32gui", "win32file", "win32com", "win32process",
    "msvcrt", "winsound", "_winapi", "pywin32",
}


def is_platform_guard(node):
    """该 if 语句是否是平台相关判断（sys.platform / platform.system）"""
    dumped = ast.dump(node.test)
    return "platform" in dumped or "os.name" in dumped


def has_early_exit_guard(tree):
    """文件顶部是否存在「非 Windows 就退出」的守卫（保护了其后的模块级代码）"""
    for node in tree.body:
        if isinstance(node, ast.If) and is_platform_guard(node):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    name = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                    if name in ("exit", "_exit"):
                        return True
    return False


def iter_module_level(node):
    """遍历「模块级会实际执行」的节点。

    两个关键取舍，避免误报：
      - 不进入函数 / 类定义体：那里的 windll 调用是运行时的，且通常已被
        try/except 或平台判断保护（例如 set_dpi_aware）
      - 跳过三元平台判断：`ctypes.windll.user32 if sys.platform == "win32" else 占位`
        是受保护写法，不应报错
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, ast.IfExp) and is_platform_guard(child):
            continue
        yield child
        for sub in iter_module_level(child):
            yield sub


def check_file(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=path)
    problems = []
    guarded_module = has_early_exit_guard(tree)

    for node in tree.body:
        # 函数 / 类定义体属于「运行时才执行」，不在本检查范围内
        # （其中的 Win32 调用是条件触发，且通常已有 try/except 保护）
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        protected = isinstance(node, ast.If) and is_platform_guard(node)

        # 1) 顶层 import 平台专有模块
        imports = []
        if isinstance(node, ast.Import):
            imports = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]

        for mod in imports:
            top = mod.split(".")[0]
            if mod in PLATFORM_MODULES or top in PLATFORM_MODULES:
                if not protected:
                    problems.append({
                        "kind": "import",
                        "line": node.lineno,
                        "detail": f"顶层导入了平台专有模块 {mod!r}，在 Linux / macOS 上会 ImportError",
                        "fix": "改成 if sys.platform == \"win32\": 条件导入，或加平台守卫后退出",
                    })

        # 2) 模块级调用 WinDLL / windll（只看模块级会执行到的代码）
        if not protected and not guarded_module:
            for sub in iter_module_level(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "windll":
                    problems.append({
                        "kind": "windll",
                        "line": getattr(sub, "lineno", node.lineno),
                        "detail": "模块级访问 ctypes.windll，非 Windows 平台为 AttributeError",
                        "fix": "改用条件赋值（非 Windows 用占位对象）或在文件顶部加平台守卫",
                    })
                elif (isinstance(sub, ast.Call)
                      and getattr(sub.func, "attr", "") == "WinDLL"):
                    problems.append({
                        "kind": "WinDLL",
                        "line": getattr(sub, "lineno", node.lineno),
                        "detail": "模块级调用 ctypes.WinDLL，非 Windows 平台不可用",
                        "fix": "在文件顶部加平台守卫（非 Windows 时输出 JSON 错误并退出）",
                    })
    return problems


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not os.path.isdir(TOOLS):
        print(f"找不到 tools 目录: {TOOLS}")
        return 2

    files = sorted(f for f in os.listdir(TOOLS) if f.endswith(".py"))
    print("=" * 66)
    print(" 跨平台导入自检（模拟 Linux / macOS 上的 import 行为）")
    print("=" * 66)

    total = 0
    report = {}
    for name in files:
        path = os.path.join(TOOLS, name)
        try:
            probs = check_file(path)
        except SyntaxError as e:
            probs = [{"kind": "syntax", "line": e.lineno,
                      "detail": f"语法错误: {e.msg}", "fix": "先修语法"}]
        report[name] = probs
        total += len(probs)
        if probs:
            print(f"[XX] {name}")
            for p in probs:
                print(f"     L{p['line']}  {p['detail']}")
                print(f"           → {p['fix']}")
        else:
            print(f"[OK] {name}")

    print("-" * 66)
    if total == 0:
        print(f"通过 {len(files)}/{len(files)} · 所有工具在非 Windows 平台上均可安全 import")
        print("（提示：这不能替代真机验证，CI 会在 Ubuntu 上实际执行一次）")
    else:
        print(f"发现 {total} 处问题，涉及 {sum(1 for v in report.values() if v)} 个文件")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

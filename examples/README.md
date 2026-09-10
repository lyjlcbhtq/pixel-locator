# Examples

四个可直接运行的最小示例。**全部不需要任何视觉模型、不需要 GPU、不联网。**

```bash
# 1. OCR 找字：拿到屏幕上某个词的像素级坐标
python examples/01_locate_text.py                  # 用仓库自带素材
python examples/01_locate_text.py "发送" --live     # 对当前屏幕实时定位

# 2. 混合定位（推荐）：文字锚点 + 模板双证命中
python examples/02_hybrid_locate.py

# 3. Python 内嵌调用：省掉进程启动开销，适合循环/批量
python examples/03_python_api.py                    # 列出当前屏幕识别到的文字块
python examples/03_python_api.py "要搜的词"          # 搜索指定的词

# 4. 声明式 GUI 流程
python tools/guibot.py examples/04_flow.json --check   # 只校验流程结构（任何环境都能过）
python tools/guibot.py examples/04_flow.json --dry     # 演练：解析但不真的点击（需要界面上真有那些文字）
```

| 示例 | 演示什么 | 关键点 |
|---|---|---|
| `01_locate_text.py` | 用 CLI 调 findtext 找字 | 输出里带 `bbox` 与中心坐标 |
| `02_hybrid_locate.py` | 用 vlocate 做锚点+模板定位 | 返回带 `confidence` 分级，**先看这个字段再决定是否点击** |
| `03_python_api.py` | 直接 import 工具模块 | 无子进程开销；引擎进程内复用（比反复起进程快一个量级） |
| `04_flow.json` | guibot 的声明式流程 | 先用 `--check` 验结构、`--dry` 演练，确认无误再去掉 |

**关于示例 3 与示例 1 的 `--live`**：它们会读取当前屏幕（**只读**，不点击、不抢焦点），
所以输出内容取决于你屏幕上正开着什么。示例 3 不带参数时会列出识别到的文字块，任何环境都能跑出结果。

**关于示例 4**：`--check` 只校验 JSON 结构，任何环境都能通过；
`--dry` 会真的去 OCR 查找流程里的文字（例如"用户名"），**界面不对就会失败，这是正常的**——
它演示的是"拿真实界面跑真实流程"的用法，请按自己的界面改文字后再用。

**注意**：示例 1 的 `--live` 与示例 3 会读取当前屏幕（只读，不点击、不抢焦点）。
`02_hybrid_locate.py` 默认只做定位、不点击；要真的点击请自行加 `--click` 并确认目标正确。

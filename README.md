# pixel-locator

[![smoke](https://github.com/lyjlcbhtq/pixel-locator/actions/workflows/smoke.yml/badge.svg)](https://github.com/lyjlcbhtq/pixel-locator/actions/workflows/smoke.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![GPU](https://img.shields.io/badge/GPU-not%20required-brightgreen)
![tokens](https://img.shields.io/badge/tokens-0%20on%20main%20path-success)

> **给 AI 精准的点击坐标。** 视觉模型看界面会偏 30~40 像素，OCR 与模板匹配能落到那个像素上。
> 纯 CPU · 无 GPU · 不调云 API · 零 token 消耗 · 没有视觉模型也能用
>
> **Pixel-precise screen coordinates for AI agents.** Let the vision model understand the
> interface; let OCR and template matching deliver the exact click point.

---

## 一、它解决什么问题

现在几乎所有"AI 操作电脑"的方案都走同一条路：截屏 → 丢给视觉大模型 → 模型给出坐标 → 点击。
这条路有一个被普遍低估的硬伤：**视觉大模型给出的坐标是不准的。**

本项目的实测（2560×1440 屏，目标是一个文本输入框）：

| 方式 | 给出的坐标 | 结果 |
|---|---|---|
| 视觉模型目测 | (1125, 1154) | 落在输入框**上方**的消息区 —— **失败** |
| OCR 像素级 bbox | (802, 1196) | **精确命中** |

误差约 **30~40 逻辑像素**，大于输入框本身的行高。

![为什么不能直接用视觉模型的坐标](docs/demo-accuracy.png)

这不是模型不够聪明，而是结构性的：主流多模态模型会把输入图压到很粗的 patch 网格
（例如 14px patch、3:1 下采样、单图上限数百 token）。一张 1280×900 的截图进入模型时，
有效分辨率已经低到**无法给出像素级坐标**。

**所以本项目的核心主张是：**

> 让大模型干它擅长的（这是什么界面、该点哪一个），
> 让本地算法干它擅长的（那个东西精确在哪个像素）。
> 两件事分开做，各自达到最好，再用一条链路串起来。

---

## 二、六个工具，各干什么

| 工具 / Tool | 作用（中文） | What it does (English) |
|---|---|---|
| **`vlocate`** | **混合定位（招牌）**：模板/锚点为主力，VLM 只做最后兜底；返回**像素级可点击坐标 + 置信度分级**（high/medium/low） | **Hybrid locator (flagship)**: template/anchor first, VLM only as a last resort. Returns a **pixel-precise click point with a confidence grade**. |
| **`findtext`** | **OCR 找字定位**：给一个词，返回它在屏幕上的**像素级 bbox**；自带内容缓存与守护进程（提速约 35 倍） | **OCR text locator**: give it a word, get its **pixel bbox** on screen. Ships with a content cache and an optional daemon (**~35× faster**). |
| **`tmatch`** | **模板匹配**：定位**无文字元素**（图标、图形、按钮图形），输出像素框 + **相似度分**；零 GPU（有 CUDA 自动加速，无卡自动降级） | **Template matching**: locate **non-text elements** (icons, graphics) with a pixel box and a **similarity score**. Zero GPU (CUDA auto-accelerates when present). |
| **`guibot`** | **声明式 GUI 流程**：把"找元素 → 点击 → 键入 → 断言"写成 JSON，一次跑完可复用 | **Declarative GUI flows**: write locate → click → type → assert steps as JSON and replay them. |
| **`winctl`** | **窗口控制**：列表 / 置顶 / 移动窗口，以及**免激活后台截图**（不抢用户焦点） | **Window control**: list / raise / move windows and **capture them in the background without stealing focus**. |
| **`headless_check`** | **无头验收**：渲染页面 → 截图 → OCR 核对文字，并**自证全程没有抢用户焦点** | **Headless acceptance**: render → screenshot → verify text via OCR, and **prove the foreground window never changed**. |

六个工具共用一套约定：JSON 输出、统一退出码（`0`=成功 / `1`=无结果或警告 / `2`=用法错误），
坐标一律为**屏幕物理像素**。

---

## 三、定位路径与实测速度

![分层定位架构](docs/demo-architecture.png)

`vlocate` 把下面这些路径按"从便宜到昂贵"依次尝试，前面命中就不往下走：

| 路径 | 命令 | 置信度 | 实测耗时 | 消耗 token |
|---|---|---|---|---|
| **区域 + 模板** | `--region x0,y0,x1,y1 --template 图标.png` | **high** | **4 ms** | 0 |
| **锚点 + 模板** | `--anchor "设置" --template 齿轮.png` | **high** | 模板 23ms + OCR 2.2s | 0 |
| 锚点 + 几何偏移 | `--anchor "密码" --offset 1,0` | medium | ~2.2 s | 0 |
| 候选清单 | `--list-all` | — | ~2.2 s | 0 |
| VLM 兜底 | `--target "那个红色的提交按钮"` | low | 5~7 s | **消耗** |

**VLM 的输出永远不会被直接当作点击坐标**——它只负责把搜索范围从全屏缩小到一小块。

### 各环节实测数据（2560×1440，纯 CPU）

| 模式 | 耗时 | 相对基线 |
|---|---|---|
| 冷进程 + 强制真 OCR（`--no-cache`） | 7082 ms | 1× |
| 冷进程 + 内容缓存命中 | ~600 ms | 12× |
| **守护进程（引擎常驻）+ 缓存命中** | **175~225 ms** | **~35×** |
| 引擎已加载，全屏推理 | 2280 ms | 3× |
| 区域 800×600 推理 | 1280 ms | 5.5× |
| **tmatch 窗口匹配（绕开 OCR）** | **4 ms** | **~1770×** |
| **vlocate 区域+模板 端到端** | **4 ms** | — |

**三条硬结论：**

1. **OCR 有约 1.2 秒的物理下限**——即使只识别 800×600 区域、只有 5 个文字块，也要 1280ms。
   这是模型前向的固定开销，靠调参无法到毫秒级。
2. **单项最大收益是守护进程**（`findtext.py --serve`）：把"进程启动 + 导入 + 引擎初始化 + 预热"
   全部归零，7 秒 → 0.6 秒。
3. **毫秒级只能靠绕开 OCR**：常态定位 = 坐标记忆（1ms）+ tmatch 验证（4ms）。
   所以正确做法是让 **OCR 尽量只出现一次**，其结果沉淀为模板与记忆。

![定位结果标注](docs/demo-locate.png)

*上图由 `python docs/make_demo.py` 生成——图里的每个坐标都是工具真实输出的，不是画上去的。*

---

## 四、下载与安装

**下载有三种方式**（压缩包由 GitHub 自动生成，不需要作者上传）：

| 方式 | 操作 / 地址 | 适合 |
|---|---|---|
| **下载 ZIP** | 仓库页绿色 **`Code`** 按钮 → **Download ZIP** | 不想装 git，最省事 |
| **git clone** | `git clone https://github.com/lyjlcbhtq/pixel-locator.git` | 想跟着更新 |
| **固定版本** | `https://github.com/lyjlcbhtq/pixel-locator/archive/refs/tags/v1.0.0.zip` | 要可复现的版本 |

**装好之后，一键安装脚本会做完剩下的事**：

```bat
install.bat          :: Windows —— 双击运行即可
```

```bash
bash install.sh      # Linux / macOS
```

脚本做四件事：检查 Python → 安装依赖 → 环境自检 → 冒烟测试。
（依赖里含 OCR 模型，首次安装需要能访问 pypi.org）

**前提**：Python **3.10 或更高**。Windows 安装时务必勾选 **"Add Python to PATH"**。

---

### 手动安装（三步）

```bash
git clone https://github.com/lyjlcbhtq/pixel-locator.git
cd pixel-locator
pip install -r requirements.txt

# 1. 环境自检（应全部 ok）
python toolkit.py doctor

# 2. 冒烟测试（应 10/10）
python smoke.py

# 3. OCR 找字：拿到像素级坐标
python toolkit.py findtext --image fixtures/sample.png --query "像素级定位"

# 4. 模板匹配：定位无文字元素
python toolkit.py tmatch find fixtures/template.png fixtures/sample.png

# 5. 混合定位（推荐）：锚点 + 模板，双证命中
python toolkit.py vlocate --image fixtures/sample.png \
    --anchor "工具包冒烟测试" --template fixtures/template.png --window 1200

# 6. 完全不知道叫什么？列出候选，让上层 AI 自己挑
python toolkit.py vlocate --image fixtures/sample.png --list-all
```

**想直接看能跑的东西**：

```bash
python examples/01_locate_text.py     # OCR 找字，拿到像素级坐标
python examples/02_hybrid_locate.py   # 锚点 + 模板双证定位（推荐用法）
python examples/03_python_api.py      # 内嵌调用，列出屏幕上的文字块与坐标
python tools/guibot.py examples/04_flow.json --check   # 校验声明式流程

python benchmarks/bench_ocr.py        # 复现 OCR 速度数据
python benchmarks/bench_tmatch.py     # 复现「全屏 vs 锚点窗口」速度差
```

`paths.json` 是可选配置：**不创建也能跑**（自动回退 `paths.example.json`）。
需要时 `cp paths.example.json paths.json` 再改。

---

## 五、典型用法

**A. 点一个带文字的按钮（零模型，最快）**

```bash
python tools/vlocate.py --text "发送" --click
```

**B. 点一个图标（无文字，用模板）**

```bash
python tools/tmatch.py collect --from 截图.png --region 640,300,726,386 --name 齿轮
python tools/vlocate.py --anchor "设置" --template 模板/齿轮.png --click
```

**C. 把一串操作写成流程**

```json
{
  "steps": [
    {"action": "find", "text": "用户名", "save_as": "u"},
    {"action": "type",  "text": "alice"},
    {"action": "find",  "text": "密码"},
    {"action": "type",  "text": "s3cret"},
    {"action": "find",  "text": "登录", "click": true},
    {"action": "assert_text", "text": "欢迎"}
  ]
}
```

```bash
python tools/guibot.py 流程.json --dry     # 先演练，不真的点
python tools/guibot.py 流程.json           # 真跑
```

**D. 无人值守验收一个页面（不抢焦点）**

```bash
python tools/headless_check.py 页面.html --out shot.png --expect "提交成功"
```

---

## 六、⚠️ 防误用清单（务必先读）

这一节全是**踩过的坑**，照抄能省下大量调试时间。

### 1. 坐标是**物理像素**，不是逻辑像素
- 所有工具输出的都是屏幕物理像素（DPI 100% 时两者相同）
- DPI 150% 时：**逻辑坐标 = 物理坐标 / 1.5**
- 点击总是偏移固定比例？先查 DPI 缩放
- **实测事故**：把物理坐标按截图缩放比例又换算一次，结果点到了别处

### 2. **绝不要把 VLM 给的坐标直接用于点击**
- VLM 坐标误差可达几十像素，足以错过输入框、按钮等小目标
- **实测事故**：VLM 圈出的区域 `[63,69,405,151]` 甚至没罩住真实目标（真值 `y=51~117`，顶部少圈 18px）
- 正确用法：VLM 只用来**缩小范围**，最终坐标必须来自 OCR 或 tmatch

### 3. **不要用 OCR 全屏直搜作为唯一定位依据**
- OCR 会认错字（形近字、连字），也会把同名多处全部返回
- 页面里有三个"设置"时，OCR 无法告诉你是哪一个
- 正确做法：OCR 出锚点/候选 → **用 tmatch 匹配确认** → 两者一致才算命中
- 本项目的 `vlocate` 已把纯 OCR 路径标记为 `confidence: low` 并附 `warning`

### 4. 内容缓存会让"看起来没更新"
- `findtext` 默认开启 md5 内容缓存：同一画面（像素级一致）直接复用上次结果
- 还有**模糊指纹缓存**：同一区域差异很小时也会命中——用于动态屏（时钟、光标、流式文本）
- 屏幕已变但结果没变？加 `--no-cache`
- 缓存是速度主要来源之一（12 倍），**不要随手永久关掉**

### 5. 守护进程与端口
- `findtext.py --serve` 在 `127.0.0.1:8377` 常驻，CLI 自动复用（35 倍提速就来自这里）
- 不想用：加 `--no-daemon`，或改 `--port`
- 用完记得停，否则一直占端口

### 6. `--image` 与屏幕路径的默认值不同
- **屏幕截屏路径**：`use_cls=False`（屏幕文字无需方向分类，更快）
- **`--image` 图片路径**：`use_cls=True`（图片可能带旋转，更稳但更慢）
- 追求速度且确定图不旋转时用 `--cls` 显式控制

### 7. 窗口操作会抢用户焦点
- `winctl --top` / `--restore` 会**激活窗口并抢走用户焦点**
- 需要"不打扰用户"时用 `--background`（免激活后台截图）或 `--list`
- `headless_check` 全程无窗口，验收类任务首选

### 8. 模板质量决定成败——**纯色模板会满分误匹配**
- **实测踩坑**：一个纯红色块模板匹配到了画面左上角的**空白处**，相似度却是满分 **1.0**。
  原因是纯色块在灰度图上无方差，归一化相关会退化
- `vlocate` 已内置预检（灰度 std < 8 时返回 `template_quality.warning`）
- 正确做法：**模板必须带边缘/纹理**，用 `tmatch collect` 从真实截图裁剪
- 另外默认 `--threshold 0.8`，降到 0.5 以下会大量误匹配

### 9. `headless_check --expect` 是 OCR 核对，不是 DOM 断言
- 它靠 OCR 读截图文字，受字体渲染、抗锯齿、缩放的轻微影响
- 需要严格断言时请用专门的 DOM 工具
- 它的独特价值是返回 `foreground{before,during,after,unchanged}`，**自证全程没抢用户焦点**

### 10. 平台与安全

**平台支持是分层的**，请按这张表判断（`python check_platform.py` 可静态自检，
CI 也会在 Ubuntu 上真实执行一次）：

| 工具 | Windows | Linux / macOS | 说明 |
|---|---|---|---|
| `tmatch` | 可用 | **可用** | 纯模板匹配，完全跨平台 |
| `vlocate` | 可用 | **可用**（只定位） | `--click` 需要 Windows |
| `findtext` | 可用 | **可用**（`--image` 路径） | 实时截屏需 `mss` + 图形界面；窗口/点击需 Windows |
| `guibot` | 可用 | **可用**（只读动作） | `find / wait / assert / screenshot` 跨平台；`click / type / key` 需 Windows |
| `winctl` | 可用 | 不支持 | Win32 窗口 API，仅 Windows |
| `headless_check` | 可用 | 不支持 | 依赖 Edge / Chrome 路径与前台窗口 API |

- 非 Windows 平台上，需要键鼠注入的动作会抛出**明确的中文错误**，而不是静默失败或崩溃
- 本项目曾因 `findtext.py` 顶层写 `import ctypes.wintypes` 导致 CI 全红，
  `check_platform.py` 就是为杜绝这类问题加入的
- **安全提醒**：本工具集能模拟键鼠。请勿在无人看管的敏感界面上放任自动点击；
  写流程时先用 `--dry` 演练

---

## 七、依赖与硬件

- **零 GPU 硬需求**：全部 CPU 即可。`tmatch` 检测到 CUDA 会自动加速，无卡自动降级 CPU，结果一致
  （本机实测 OpenCV 为 CPU 构建版，相似度仍为 1.0，窗口匹配 4ms）
- **零模型训练**：OCR 用 RapidOCR 自带 ONNX 模型，首次运行本地加载，无需联网下载
- **零外部服务**：不需要代理、不需要任何云 API
- 可选：任意 OpenAI 兼容视觉端点（供 `vlocate` 第 3 层兜底），**不配也能完整使用**

```
python >= 3.10
pillow / numpy / opencv-python / rapidocr-onnxruntime / requests / mss
```

---

## 八、目录结构

```
pixel-locator/
├── toolkit.py              统一入口（list / doctor / paths / <工具>）
├── smoke.py                冒烟测试（自带素材，克隆即可跑；--ci 供 CI 使用）
├── check_platform.py       跨平台导入自检（防止 Windows 专有导入混入）
├── install.bat / install.sh  一键安装（检查 Python → 装依赖 → 自检 → 冒烟）
├── make_fixtures.py        重新生成测试素材
├── paths.example.json      配置模板（全部字段可空，不创建也能跑）
├── requirements.txt
├── README.md / README.en.md
├── CHANGELOG.md
├── LICENSE                 MIT
├── .github/workflows/      GitHub Actions 冒烟 CI
├── tools/
│   ├── vlocate.py          混合定位（招牌）
│   ├── findtext.py         OCR 定位（缓存 + 守护进程）
│   ├── tmatch.py           模板匹配
│   ├── guibot.py           GUI 流程编排
│   ├── winctl.py           窗口控制
│   └── headless_check.py   无头验收
├── examples/               四个可直接运行的示例
├── benchmarks/             两个基准脚本（复现 README 里的全部数字）
├── docs/                   演示图 + 可复现的生成脚本
└── fixtures/               冒烟测试素材
```

---

## 九、已知限制（诚实声明）

1. **OCR 的 1.2 秒下限**：单次推理无法更低，这是模型前向的固定开销，不是实现问题
2. **VLM 只能兜底**：视觉模型坐标精度不足以直接点击，本项目只把它当"侦察兵"
3. **模板匹配需要模板**：无文字元素第一次需人工给一次模板（或 `tmatch collect` 采一次），之后才能自动
4. **动态界面**：画面剧烈变化时缓存失效，会退回真实 OCR 耗时
5. **Windows 偏向**：窗口相关能力依赖 Win32，跨平台需要替换这一层（接口已隔离）
6. **不做 UIA/Accessibility**：本项目走**纯像素路线**，这是刻意选择——为了覆盖
   UIA 抓不到的自绘界面、游戏、Canvas 应用

---

## 十、许可

MIT License，见 `LICENSE`。

---

**English documentation**: see [`README.en.md`](README.en.md)

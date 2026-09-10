# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-09-10

首个公开版本。主题聚焦：**给 AI 精准的屏幕坐标**。

### 核心工具

- **`vlocate`** 混合定位（招牌）
  - 五条定位路径按"从便宜到昂贵"依次尝试：区域+模板 → 锚点+模板 → 锚点+几何偏移 → 候选清单 → VLM 兜底
  - 返回 **置信度分级**（`high` / `medium` / `low`）与 `verified` 标志，调用方据此决定是否点击
  - **VLM 的输出永不直接作为点击坐标**，只用于缩小搜索范围
  - 纯 OCR 路径降级为 `low` 并附 `warning`（同名多匹配时明确提示不可靠）
  - 内置**模板质量预检**：灰度标准差 < 8 时警告纯色模板会产生满分误匹配
  - 实测最快路径（区域+模板）端到端 **4 ms**

- **`findtext`** OCR 定位
  - 输出像素级 bbox + 置信度
  - md5 内容缓存（同画面命中约 12 倍提速）
  - 守护进程 `--serve`（引擎常驻，实测约 35 倍提速：7082 ms → 175~225 ms）

- **`tmatch`** 模板匹配
  - 定位无文字元素（图标/图形），输出像素框 + 相似度分
  - 零 GPU 硬需求；检测到 CUDA 自动加速，无卡自动降级 CPU，结果一致
  - 锚点窗口内匹配实测 **~4 ms**（全屏 147 ms，净提速约 37 倍，坐标完全一致）

- **`guibot`** 声明式 GUI 流程（JSON：find / click / type / key / wait / assert / if …）
- **`winctl`** 窗口控制：列表 / 置顶 / 移动 + 免激活后台截图
- **`headless_check`** 无头验收：渲染 → 截图 → OCR 核对，并自证全程未抢用户焦点

### 工程与文档

- `toolkit.py` 统一入口：`list` / `doctor` / `paths` / 信封模式 `--json`
- `smoke.py` 冒烟测试：10 个用例（`--ci` 模式 8 个，可在无桌面环境运行）
- `docs/` 演示图与**可复现**的生成脚本（图里的坐标来自工具真实输出）
- `examples/` 四个可直接运行的示例
- `benchmarks/` 两个基准脚本，可复现 README 中的全部速度数据
- `README.md`（中文）+ `README.en.md`（English），含 **10 条防误用清单**
- GitHub Actions 冒烟 CI（Python 3.10 / 3.12）

### 设计取舍

- **纯像素路线，不做 UIA / Accessibility**：为覆盖自绘界面、游戏、Canvas 应用等
  无障碍树抓不到的场景
- **零 GPU、零模型训练、零外部服务**：主路径完全本地运行，不消耗任何 API token
- **没有视觉模型也能完整使用**：模板、锚点、候选清单三条路径全部本地计算

[1.0.0]: https://github.com/lyjlcbhtq/pixel-locator/releases/tag/v1.0.0

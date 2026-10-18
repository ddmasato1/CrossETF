# CrossETF 移动端适配 Design QA

## 结论

- 验收日期：2026-10-18
- 最终结果：**通过**
- 实施范围：`static/index.html`、`test_review_criteria.py`
- 设计基准：`docs/mobile-adaptation/demo.html`
- 正式页面：`http://127.0.0.1:8000/?mobile-qa=2`

## 对照依据

- 设计基准截图：
  - `docs/mobile-adaptation/evidence/05-demo-closed.jpg`
  - `docs/mobile-adaptation/evidence/06-demo-expanded.jpg`
- 正式实现截图：
  - `docs/mobile-adaptation/evidence/implementation/01-mobile-closed.jpg`
  - `docs/mobile-adaptation/evidence/implementation/02-mobile-expanded.jpg`
  - `docs/mobile-adaptation/evidence/implementation/03-mobile-history.jpg`
  - `docs/mobile-adaptation/evidence/implementation/04-tablet-desktop.jpg`
  - `docs/mobile-adaptation/evidence/implementation/07-mobile-light.jpg`
  - `docs/mobile-adaptation/evidence/implementation/08-mobile-final.jpg`
  - `docs/mobile-adaptation/evidence/implementation/09-calculator-amount-layout.jpg`
  - `docs/mobile-adaptation/evidence/implementation/10-calculator-amount-320.jpg`
  - `docs/mobile-adaptation/evidence/implementation/12-calculator-select-font.jpg`
- 并排对照证据：
  - `docs/mobile-adaptation/evidence/implementation/05-qa-comparison.jpg`
  - `docs/mobile-adaptation/evidence/implementation/06-qa-comparison-expanded.jpg`
  - `docs/mobile-adaptation/evidence/implementation/11-calculator-layout-comparison.jpg`
  - `docs/mobile-adaptation/evidence/implementation/13-select-font-comparison.jpg`

截图密度为 1x。320×568 浏览器截图的实际内容区域为 305×541；390×844 视口截图的实际内容区域为 375×812（浏览器滚动条与外层视口占位所致）。平板/桌面切换证据使用 768×1024 视口。

## 响应式验证

| 视口 | 手机列表 | 桌面表格 | 文档横向溢出 | 手机主要控件最小高度 |
| --- | --- | --- | --- | --- |
| 320×568 | 可见 | 隐藏 | 无 | 44px |
| 375×667 | 可见 | 隐藏 | 无 | 44px |
| 390×844 | 可见 | 隐藏 | 无 | 44px |
| 430×932 | 可见 | 隐藏 | 无 | 44px |
| 768×1024 | 隐藏 | 可见 | 无 | 桌面紧凑模式 |

断点行为符合方案：`<640px` 使用手机卡片，`>=640px` 保留完整桌面表格。

## 信息与视觉核对

- 默认卡片只展示 ETF 代码与简称、实时溢价率、涨跌幅、现价。
- 点击卡片后行内展示费率、IOPV、净值、成交额、市场、跟踪指数和风险建议。
- 字体层级、间距、圆角、色彩语义与 Demo 一致；正式页面保留真实品牌头部、状态与操作区。
- 深色与浅色模式均已验证；现有 favicon 清晰，无新增位图素材或拉伸问题。
- 文案使用正式业务字段，数值由实时数据驱动，因此与静态 Demo 示例值不同属于预期差异。

## 交互与可访问性核对

- 同时只允许一条 ETF 展开；点击已展开项可收起。
- 切换纳指/标普 Tab 会清空展开状态。
- 卡片为原生按钮，具备 `aria-expanded` 和 `aria-controls`。
- Tab 具备 `tablist`、`tab`、`aria-selected` 语义。
- 历史弹窗具备 dialog 语义、背景滚动锁定、Escape 关闭与焦点恢复。
- 手机端主要操作与刷新周期选择器的触控高度均为 44px。
- 浏览器控制台无运行错误；仅保留项目原有 Tailwind CDN 生产环境提示。

## QA 发现与修复记录

1. **P2：长 ETF 简称与跟踪指数被截断。** 已改为自然换行，并在 320px 复验通过。
2. **P2：刷新周期下拉框的原生点击区域只有 16px。** 已将 select 与自定义秒数输入的手机端最小高度提升至 44px。
3. **P2：价差金额单位在窄屏被挤到下一行。** 来源截图为 `docs/mobile-adaptation/evidence/price-layout-before.png`。已将手机端结果改为两列指标块，标签与数值分层，并为金额值添加不换行约束。320×568 CSS 视口、1x 密度下，实际内容截图为 305×541，金额“≈ 137.00 元”宽 94px、高 24px且保持单行；修改前后聚焦对照见 `11-calculator-layout-comparison.jpg`。
4. **P2：价差测算选框使用等宽字体且字号偏大。** 来源截图为 `docs/mobile-adaptation/evidence/select-font-before.png`。已移除 `font-mono`，改用页面统一的系统无衬线字体，并将字号设为 14px、字重设为 500；两个 select 仍保持 44px 触控高度。390×844 CSS 视口、1x 密度下复验通过，聚焦对照见 `13-select-font-comparison.jpg`。

最终复验未发现剩余 P0、P1 或 P2 问题。

## 自动化回归

- `python3 test_review_criteria.py`：8/8 通过。
- `git diff --check`：通过。
- 新增静态结构检查覆盖手机列表、桌面表格、核心字段、展开状态和关键无障碍标记。

## 剩余非阻塞验证

- 尚未在实体 iOS/Android 设备与不同系统字体缩放下做人工验收。
- 横屏仅按可用宽度自然切换断点，不属于本次专项优化范围。

final result: passed

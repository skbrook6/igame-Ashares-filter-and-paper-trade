# A-share K-line Trading MVP

一个面向 A 股个人主观交易训练的 K 线决策与模拟交易系统。

本项目用于搭建一个类似“K 线训练营”的 discretionary trading workflow：
通过 K 线图快速浏览股票，使用键盘快捷键打上买入、卖出、观望、持有标签，再由系统根据模拟账户现金和仓位规则生成待下单列表，并执行 paper trading。系统同时提供持仓可视化、交易记录、筛选器和技术指标辅助。

> 当前版本为个人自用 MVP / paper trading 版本。
> 不构成投资建议，不保证行情数据完整性、实时性或交易结果准确性。

---

## Features

### 1. K-lines Gamer

* 显示 A 股日 K 线。
* 默认使用 TDX / pytdx 免费行情源。
* 支持前复权 K 线。
* K 线图不显示周末和节假日空档，保持交易日连续。
* K 线图固定视觉比例，避免窗口拉伸导致主观误判。
* 上涨为红色空心蜡烛，下跌为绿色实心蜡烛。
* 成交量图独立显示在 K 线图下方。
* MACD 独立显示在成交量图下方。
* 所有股票图支持 MA 开关。

### 2. Keyboard-first Workflow

系统支持键盘操作，尽量减少鼠标使用。

默认快捷键：

| Key | Function                        |
| --- | ------------------------------- |
| A   | K 线窗口向左平移，查看更早 K 线 |
| D   | K 线窗口向右平移，回到更新 K 线 |
| W   | previous，上一只股票            |
| S   | next，下一只股票                |
| J   | 左侧动作                        |
| K   | 右侧动作                        |

在无持仓股票上：

| Key | Action |
| --- | ------ |
| J   | BUY    |
| K   | WATCH  |

在已持仓股票上：

| Key | Action |
| --- | ------ |
| J   | SELL   |
| K   | HOLD   |

快捷键可以在侧边栏的 `key settings` 中调整。

---

## Trading Logic

### 当前标签逻辑

系统记录的是“当前标签”，不是每次点击动作。

例如：

```text
000725 当前标签 = BUY
```

连续按多次 `J` 不会生成多条 BUY 记录。
如果将标签从 BUY 改成 WATCH，该股票会从 `to_operate_list` 中移除。

标签逻辑：

| 状态   | 可选操作    |
| ------ | ----------- |
| 无持仓 | BUY / WATCH |
| 已持仓 | SELL / HOLD |

只有以下标签会进入 `to_operate_list`：

```text
BUY
SELL
```

以下标签只用于记录判断，不进入待下单列表：

```text
WATCH
HOLD
```

---

## Main Pages

### K-lines-gamer

用于快速浏览待判断股票池并打标签。

功能包括：

* 显示当前股票 K 线。
* 显示 MA5 / MA10 / MA20。
* 显示成交量及成交量 MA5 / MA10。
* 显示 MACD。
* 支持 A / D 平移 K 线窗口。
* 支持 W / S 切换股票。
* 支持 J / K 打标签。
* 买卖成交记录会显示在 K 线图 x 轴附近，用 B / S 标记对应交易日，不遮挡 K 线。

### to_operate_list

用于查看当前所有待操作标签。

功能包括：

* 只展示 BUY / SELL 标签。
* 自动根据仓位规则计算下单数量。
* 支持直接在表格中勾选删除。
* 点击下单后执行 paper trading。
* 下单成功后，对应 BUY / SELL 标签会自动从 to_operate_list 中移除。

### visualization

用于查看当前模拟持仓。

功能包括：

* 显示当前持仓列表。
* 显示每只股票：

  * 持仓数量
  * 成本
  * 当前价格
  * 市值
  * 持仓盈亏
  * 持仓盈亏百分比
  * 开仓时间
* 盈利用红色显示，亏损用绿色显示。
* 点击持仓列表中的一行，可以跳转到对应股票 K 线图。
* 支持 W / S 切换持仓股票。
* 支持 A / D 平移 K 线窗口。
* 支持 J / K 对持仓股票打 SELL / HOLD 标签。

> 当前版本已经移除账户级 PnL 历史曲线，以提升 visualization 页面打开速度。
> 账户现金和账户当前价值仍显示在侧边栏。

### trading_log

用于查看历史 paper trading 订单记录。

### settings

用于调整系统参数。

目前包括：

* K 线颜色设置。
* MACD 参数设置。
* 缩量筛选阈值设置。
* 快捷键设置。
* 刷新 TDX 股票池缓存。
* 重置 paper trading 账户。

---

## Market Data

当前版本默认使用：

```text
TDX / pytdx
```

用于获取：

* 股票列表
* 股票名称
* 实时 / 准实时行情快照
* 日 K 数据
* bid / ask
* 成交量
* 成交额

TDX 数据适合个人 MVP 和研究使用，但不适合商业化或高频交易场景。

### Data Source Notes

TDX / pytdx 不是交易所级逐笔实时行情。
它更接近主动轮询的行情快照，适合：

* 手动选股
* 收盘前截面判断
* Paper trading
* 主观 K 线训练

当前版本不做自动高频刷新。
建议使用方式是：在需要判断时手动打开或刷新页面，获取当时截面数据。

---

## Technical Indicators

### Price Moving Averages

K 线图支持：

| Indicator | Color  |
| --------- | ------ |
| MA5       | Yellow |
| MA10      | Blue   |
| MA20      | Purple |

可以通过图表上的 `MA: ON / OFF` 开关显示或隐藏。

### Volume Moving Averages

成交量图支持：

| Indicator   | Color  |
| ----------- | ------ |
| Volume MA5  | Yellow |
| Volume MA10 | Blue   |

### MACD

MACD 显示在成交量图下方。

默认参数：

```text
fast = 12
slow = 26
signal = 9
```

可以在 `settings` 页面修改。

MACD 柱使用竖线显示，而不是宽柱。

---

## Filter

系统提供股票池筛选器，用于生成待判断清单。

当前支持：

* 价格区间筛选
* 今日涨跌幅区间筛选
* 排除涨停股票
* 排除 ST / *ST 股票
* 排除特殊代码前缀 / 板块
* 缩量筛选
* 排序方式
* 最多股票数限制

### Prefix / Board Filter

可以通过代码前缀排除板块，例如：

```text
300
301
688
000
002
600
601
603
605
```

### Volume Contraction Filter

缩量筛选逻辑：

```text
预测当日成交量 < 成交量 MA5 × 阈值
并且
预测当日成交量 < 成交量 MA10 × 阈值
```

默认阈值：

```text
2 / 3
```

该参数可以在 `settings` 页面调整。

为了降低 TDX 请求压力，建议先使用价格、涨跌幅、板块等轻量条件缩小股票池，再开启缩量筛选。

---

## Paper Trading

当前系统默认是 paper trading。

侧边栏显示：

* 模拟现金
* 账户当前价值
* 股票市值
* 行情源
* 股票池来源
* K 线复权状态

### Reset Paper Account

侧边栏左下角有：

```text
reset paper account
```

点击确认后会清空：

* 当前标签
* 订单记录
* 持仓记录
* 模拟现金状态

并将本金重置为配置中的初始值。

---

## Project Structure

```text
kline_trading_mvp/
├── app/
│   ├── data_provider.py
│   ├── storage.py
│   ├── position_manager.py
│   ├── order_pusher.py
│   └── market_data/
│       ├── __init__.py
│       └── tdx_provider.py
├── data/
├── logs/
├── main.py
├── config.yaml
├── requirements.txt
├── README.md
└── .gitignore
```

---

## Installation

### 1. Create virtual environment

```powershell
python -m venv .venv
```

### 2. Activate virtual environment

Windows PowerShell:

```powershell
.\.venv\Scripts\activate
```

### 3. Install dependencies

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

---

## Run

```powershell
streamlit run main.py
```

Then open:

```text
http://localhost:8501
```

---

## Recommended Workflow

### Before market close

1. Open the app.
2. Go to `K-lines-gamer`.
3. Use `filter` to generate a candidate pool.
4. Browse stocks using `W / S`.
5. Shift K line window using `A / D`.
6. Use `J / K` to label BUY / WATCH or SELL / HOLD.
7. Go to `to_operate_list`.
8. Review BUY / SELL list.
9. Delete unwanted operations by selecting rows.
10. Execute paper trading orders.

### After trading

1. Go to `visualization`.
2. Review current positions.
3. Click a holding row to inspect its K line.
4. Use `trading_log` for review.

---

## Git / GitHub Notes

Do not upload local runtime data or secrets.

Recommended `.gitignore` should exclude:

```gitignore
.venv/
__pycache__/
logs/
*.log
*.db
*.sqlite
*.sqlite3
data/tdx_universe.csv
data/tdx_quote_cache.csv
data/tdx_finance_cache.csv
.streamlit/secrets.toml
```

If using GitHub, it is recommended to keep the repository private while the project contains personal trading workflow logic.

---

## Disclaimer

This project is for personal research, discretionary trading workflow design, and paper trading only.

It does not provide investment advice.
It does not guarantee market data accuracy or timeliness.
It does not connect to a real broker in the current version.
Any future real-money trading integration should include proper risk control, audit logs, permission management, and broker-side validation.

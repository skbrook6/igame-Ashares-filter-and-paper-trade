# K-lines Gamer Trading MVP v4

个人免费数据源版本：TDX-only。

## v4 改动

- 去掉 AKShare/东方财富股票池强依赖，避免代理/网络导致股票池回退 sample。
- TDX 同时负责：
  - A 股股票池生成
  - 实时最新价 / bid1 / ask1 / 成交额 / 成交量
  - 日 K 线
- `filter` 支持：
  - 价格范围
  - 可选市值范围（当前 TDX-only 可能没有市值，无法获取时自动跳过）
  - 排序方式：成交额、价格、代码、市值
  - 最多股票数：0 表示不限制
- 新增 TDX 股票池缓存：`data/tdx_universe.csv`

## 安装

```powershell
python -m pip install -r requirements.txt
```

## 运行

```powershell
streamlit run main.py
```

## 注意

TDX 免费行情适合个人工具和原型，不是交易所级实时，也不建议作为商业化唯一数据源。

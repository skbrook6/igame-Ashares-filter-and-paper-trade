from __future__ import annotations

import pandas as pd


class AkshareUniverseProvider:
    """免费全市场股票池/市值筛选适配器。

    主要用于 selector。实时K线和盘口优先由 TDX 提供。
    """

    @staticmethod
    def load_a_share_spot() -> pd.DataFrame:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ImportError("未安装 akshare，请先执行：pip install akshare") from exc

        raw = ak.stock_zh_a_spot_em()
        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw.copy()
        # 东财接口常见中文字段；做容错映射。
        col_map = {
            "代码": "code",
            "名称": "name",
            "最新价": "close",
            "成交额": "amount",
            "总市值": "market_cap",
            "流通市值": "float_market_cap",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        required = ["code", "name", "close", "amount", "market_cap"]
        for col in required:
            if col not in df.columns:
                df[col] = None
        df = df[required].copy()
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["name"] = df["name"].astype(str)
        for col in ["close", "amount", "market_cap"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["code", "close"])
        return df.sort_values("amount", ascending=False).reset_index(drop=True)

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import math
import pandas as pd


DEFAULT_TDX_SERVERS = [
    # 你本机测试成功的服务器放前面
    ("180.153.18.170", 7709),
    ("119.147.212.81", 7709),
    ("180.153.18.171", 7709),
    ("202.108.253.130", 7709),
    ("47.103.48.45", 7709),
    ("106.14.95.149", 7709),
    ("114.80.63.12", 7709),
]


@dataclass
class TdxQuote:
    code: str
    name: str | None
    last: float | None
    open: float | None
    high: float | None
    low: float | None
    pre_close: float | None
    volume_shares: float | None
    amount: float | None
    bid1: float | None
    ask1: float | None
    bid_size1: float | None
    ask_size1: float | None


class TdxDataProvider:
    """Free TDX行情适配器。

    v4 目标：尽量只依赖 TDX，避免 AKShare/东财代理问题。
    - quote / 盘口 / 日K 来自 TDX
    - universe 股票池也来自 TDX get_security_list + 分批 get_security_quotes
    - market_cap 暂时不强依赖；TDX finance 若可用再估算，否则 NaN

    说明：pytdx quote 成交量字段通常按“手”返回，这里统一转换成“股”。
    """

    def __init__(self, host: str | None = None, port: int = 7709, auto_retry: bool = True):
        try:
            from pytdx.hq import TdxHq_API
        except ImportError as exc:
            raise ImportError("未安装 pytdx，请先执行：pip install pytdx") from exc

        self.host = host or None
        self.port = int(port)
        self.auto_retry = auto_retry
        self.api = TdxHq_API(heartbeat=True, auto_retry=auto_retry)
        self.connected_server: tuple[str, int] | None = None
        self._connect()

    def _connect(self):
        servers: list[tuple[str, int]] = []
        if self.host:
            servers.append((self.host, self.port))
        for s in DEFAULT_TDX_SERVERS:
            if s not in servers:
                servers.append(s)

        last_error = None
        for host, port in servers:
            try:
                ok = self.api.connect(host, int(port))
                if ok:
                    self.connected_server = (host, int(port))
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"无法连接通达信行情服务器，最后错误：{repr(last_error)}")

    @staticmethod
    def market_id(code: str) -> int:
        code = str(code).zfill(6)
        if code.startswith(("5", "6", "9")):
            return 1  # 上海
        return 0      # 深圳。北交所后续单独做，不在当前 v4 范围内。

    @staticmethod
    def _safe_float(value):
        try:
            if value is None:
                return None
            if isinstance(value, str) and value.strip() == "":
                return None
            x = float(value)
            if math.isnan(x):
                return None
            return x
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe_get(item, *keys):
        for key in keys:
            if isinstance(item, dict) and key in item:
                return item.get(key)
            if hasattr(item, key):
                return getattr(item, key)
        return None

    @staticmethod
    def is_a_share_code(code: str) -> bool:
        code = str(code).zfill(6)
        return code.startswith((
            "600", "601", "603", "605", "688",  # SH A shares / 科创板
            "000", "001", "002", "003", "300", "301",  # SZ 主板/中小板/创业板
        ))

    def get_realtime_quote(self, codes: Iterable[str]) -> pd.DataFrame:
        codes = [str(c).zfill(6) for c in codes]
        if not codes:
            return pd.DataFrame()

        query = [(self.market_id(c), c) for c in codes]
        data = self.api.get_security_quotes(query) or []

        rows = []
        for item in data:
            code = str(self._safe_get(item, "code") or "").zfill(6)
            if not code or code == "000000":
                continue
            vol_hands = self._safe_float(self._safe_get(item, "vol", "volume"))
            rows.append(
                {
                    "code": code,
                    "name": self._safe_get(item, "name"),
                    "last": self._safe_float(self._safe_get(item, "price", "last")),
                    "open": self._safe_float(self._safe_get(item, "open")),
                    "high": self._safe_float(self._safe_get(item, "high")),
                    "low": self._safe_float(self._safe_get(item, "low")),
                    "pre_close": self._safe_float(self._safe_get(item, "last_close", "pre_close")),
                    "volume_shares": None if vol_hands is None else vol_hands * 100.0,
                    "amount": self._safe_float(self._safe_get(item, "amount")),
                    "bid1": self._safe_float(self._safe_get(item, "bid1")),
                    "ask1": self._safe_float(self._safe_get(item, "ask1")),
                    "bid_size1": self._safe_float(self._safe_get(item, "bid_vol1", "bid_size1")),
                    "ask_size1": self._safe_float(self._safe_get(item, "ask_vol1", "ask_size1")),
                    "source": "tdx",
                }
            )
        df = pd.DataFrame(rows)
        if not df.empty:
            df["last"] = pd.to_numeric(df["last"], errors="coerce")
            df["pre_close"] = pd.to_numeric(df["pre_close"], errors="coerce")
            df["pct_chg"] = (df["last"] / df["pre_close"] - 1.0) * 100.0
        return df

    def get_xdxr_info_df(self, code: str) -> pd.DataFrame:
        """Return TDX ex-right/dividend events if the server exposes them.

        pytdx versions differ slightly. When unavailable, return an empty DataFrame
        and the caller will use raw bars.
        """
        code = str(code).zfill(6)
        try:
            if not hasattr(self.api, "get_xdxr_info"):
                return pd.DataFrame()
            data = self.api.get_xdxr_info(self.market_id(code), code) or []
            if not data:
                return pd.DataFrame()
            df = self.api.to_df(data).copy()
            if df.empty:
                return df
            if {"year", "month", "day"}.issubset(df.columns):
                df["date"] = pd.to_datetime(
                    df["year"].astype(int).astype(str)
                    + "-"
                    + df["month"].astype(int).astype(str).str.zfill(2)
                    + "-"
                    + df["day"].astype(int).astype(str).str.zfill(2),
                    errors="coerce",
                )
            elif "datetime" in df.columns:
                df["date"] = pd.to_datetime(df["datetime"], errors="coerce")
            else:
                return pd.DataFrame()
            return df.dropna(subset=["date"]).sort_values("date")
        except Exception:  # noqa: BLE001
            return pd.DataFrame()

    @staticmethod
    def _first_existing(row, names, default=0.0):
        for name in names:
            try:
                if name in row and pd.notna(row[name]):
                    return row[name]
            except Exception:  # noqa: BLE001
                pass
        return default

    def _apply_qfq(self, bars: pd.DataFrame, code: str) -> pd.DataFrame:
        """Approximate 前复权 using TDX xdxr events.

        For every ex-dividend/ex-right date inside the bar window, prices before the
        event are multiplied by the theoretical ex-right price / previous close.
        This keeps the latest prices unchanged and adjusts old prices downward,
        which fixes visible dividend gaps such as Ping An ex-dividend days.
        """
        if bars.empty:
            return bars
        events = self.get_xdxr_info_df(code)
        if events.empty:
            return bars

        df = bars.copy().sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
        if not price_cols:
            return df

        for _, ev in events.iterrows():
            ex_date = pd.to_datetime(ev["date"]).normalize()
            idxs = df.index[df["date"] >= ex_date].tolist()
            if not idxs:
                continue
            idx = idxs[0]
            prev_idx = idx - 1
            if prev_idx < 0:
                continue
            prev_close = pd.to_numeric(df.loc[prev_idx, "close"], errors="coerce")
            if pd.isna(prev_close) or float(prev_close) <= 0:
                continue

            # TDX commonly stores these per 10 shares.
            cash_div = float(self._first_existing(ev, ["fenhong", "dividend", "fh"], 0.0) or 0.0) / 10.0
            bonus = float(self._first_existing(ev, ["songzhuangu", "songgu", "zhuanzeng", "sg"], 0.0) or 0.0) / 10.0
            rights = float(self._first_existing(ev, ["peigu", "rights", "pg"], 0.0) or 0.0) / 10.0
            rights_price = float(self._first_existing(ev, ["peigujia", "rights_price", "pgj"], 0.0) or 0.0)

            denom = 1.0 + bonus + rights
            if denom <= 0:
                continue
            theoretical_ex = (float(prev_close) - cash_div + rights_price * rights) / denom
            if theoretical_ex <= 0:
                continue
            factor = theoretical_ex / float(prev_close)
            if not (0.5 <= factor <= 1.5):
                continue

            df.loc[:prev_idx, price_cols] = df.loc[:prev_idx, price_cols] * factor
            # Volume adjustment is less important for visual discretionary charting;
            # keep raw volume to avoid confusing turnover interpretation.

        return df

    def get_daily_bars(self, code: str, n: int = 60, adjust: str = "qfq") -> pd.DataFrame:
        code = str(code).zfill(6)
        data = self.api.get_security_bars(
            category=9,  # 日K
            market=self.market_id(code),
            code=code,
            start=0,
            count=int(n),
        ) or []
        if not data:
            return pd.DataFrame()

        df = self.api.to_df(data).copy()
        rename_map = {"datetime": "date", "vol": "volume"}
        df = df.rename(columns=rename_map)
        df["code"] = code
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100.0
        if "amount" not in df.columns:
            df["amount"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["volume"], errors="coerce")
        df = df[["date", "code", "open", "high", "low", "close", "volume", "amount"]].sort_values("date")
        if str(adjust).lower() in {"qfq", "front", "前复权"}:
            df = self._apply_qfq(df, code)
        return df

    def _security_list_one_market(self, market: int) -> pd.DataFrame:
        try:
            count = int(self.api.get_security_count(market) or 0)
        except Exception:  # noqa: BLE001
            count = 0
        rows = []
        # pytdx 每次通常返回最多 1000 条；用 1000 步长足够
        for start in range(0, max(count, 1), 1000):
            try:
                data = self.api.get_security_list(market, start) or []
            except Exception:  # noqa: BLE001
                data = []
            if not data:
                continue
            for item in data:
                code = str(self._safe_get(item, "code") or "").zfill(6)
                name = self._safe_get(item, "name") or ""
                if self.is_a_share_code(code):
                    rows.append({"market": market, "code": code, "name": str(name)})
        return pd.DataFrame(rows)

    def list_a_share_securities(self) -> pd.DataFrame:
        parts = [self._security_list_one_market(1), self._security_list_one_market(0)]
        df = pd.concat([p for p in parts if p is not None and not p.empty], ignore_index=True) if parts else pd.DataFrame()
        if df.empty:
            return pd.DataFrame(columns=["market", "code", "name"])
        df = df.drop_duplicates("code")
        df = df[df["name"].astype(str).str.strip() != ""]
        # 排除明显非交易/测试名称；ST 不排除，后面可作为 filter 选项
        return df.sort_values("code").reset_index(drop=True)

    def get_quotes_batched(self, codes: Iterable[str], batch_size: int = 80) -> pd.DataFrame:
        codes = [str(c).zfill(6) for c in codes]
        frames = []
        for i in range(0, len(codes), int(batch_size)):
            q = self.get_realtime_quote(codes[i:i + int(batch_size)])
            if q is not None and not q.empty:
                frames.append(q)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).drop_duplicates("code")

    def build_universe(self, cache_path: str | Path | None = None, force_refresh: bool = False) -> pd.DataFrame:
        """Build A-share universe from TDX only.

        cache_path 存在且 force_refresh=False 时直接读缓存。
        market_cap 当前默认 NaN；如果后续接 finance cache，可以在这里补充。
        """
        cache_path = Path(cache_path) if cache_path else None
        if cache_path is not None and cache_path.exists() and not force_refresh:
            df = pd.read_csv(cache_path, dtype={"code": str})
            df["code"] = df["code"].astype(str).str.zfill(6)
            return df

        base = self.list_a_share_securities()
        if base.empty:
            return pd.DataFrame(columns=["code", "name", "close", "amount", "market_cap", "market"])

        quotes = self.get_quotes_batched(base["code"].tolist(), batch_size=80)
        if quotes.empty:
            u = base.copy()
            u["close"] = pd.NA
            u["amount"] = 0.0
        else:
            quote_cols = [c for c in ["code", "last", "amount", "pre_close", "pct_chg"] if c in quotes.columns]
            q = quotes[quote_cols].rename(columns={"last": "close"})
            u = base.merge(q, on="code", how="left")

        u["close"] = pd.to_numeric(u["close"], errors="coerce")
        u["amount"] = pd.to_numeric(u["amount"], errors="coerce").fillna(0.0)
        if "pre_close" not in u.columns:
            u["pre_close"] = pd.NA
        u["pre_close"] = pd.to_numeric(u["pre_close"], errors="coerce")
        if "pct_chg" not in u.columns:
            u["pct_chg"] = (u["close"] / u["pre_close"] - 1.0) * 100.0
        u["pct_chg"] = pd.to_numeric(u["pct_chg"], errors="coerce")
        u["market_cap"] = pd.NA

        def _is_limit_up(row):
            code = str(row.get("code", "")).zfill(6)
            name = str(row.get("name", ""))
            close = row.get("close")
            pre_close = row.get("pre_close")
            if pd.isna(close) or pd.isna(pre_close) or float(pre_close) <= 0:
                return False
            if "ST" in name.upper() or "退" in name:
                limit = 0.05
            elif code.startswith(("300", "301", "688")):
                limit = 0.20
            else:
                limit = 0.10
            return float(close) >= float(pre_close) * (1 + limit - 0.003)

        u["is_limit_up"] = u.apply(_is_limit_up, axis=1)
        u = u[["code", "name", "close", "amount", "market_cap", "market", "pre_close", "is_limit_up", "pct_chg"]]
        u = u.sort_values("amount", ascending=False, na_position="last").reset_index(drop=True)

        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            u.to_csv(cache_path, index=False)
        return u

    def close(self):
        try:
            self.api.disconnect()
        except Exception:  # noqa: BLE001
            pass

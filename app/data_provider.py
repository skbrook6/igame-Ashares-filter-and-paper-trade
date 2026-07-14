from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo
from pathlib import Path
import numpy as np
import pandas as pd


class DataProvider:
    """Unified data provider used by the Streamlit UI.

    v4: TDX-first free personal-use version.
    - TDX: quote / order book / daily bars / A-share universe
    - sample: local fallback only
    - AKShare is no longer required because user's network/proxy blocks Eastmoney.
    """

    def __init__(self, sample_path="data/sample_ohlcv.csv", market_data_cfg: dict | None = None):
        self.sample_path = Path(sample_path)
        self.market_data_cfg = market_data_cfg or {"provider": "sample"}
        self.provider_name = str(self.market_data_cfg.get("provider", "sample")).lower()
        self.selector_provider = str(self.market_data_cfg.get("selector_universe", self.provider_name)).lower()
        self.fallback_to_sample = bool(self.market_data_cfg.get("fallback_to_sample", True))
        self.tdx = None
        self.provider_status = "sample"
        self.universe_status = "sample"
        self.last_provider_error = ""
        self.last_universe_error = ""
        self.adjust = str(self.market_data_cfg.get("adjust", "qfq")).lower()

        if not self.sample_path.exists():
            self._make_sample()
        self.df = self._load_sample()

        if self.provider_name == "tdx" or self.selector_provider == "tdx":
            self._init_tdx()

    def _load_sample(self) -> pd.DataFrame:
        df = pd.read_csv(
            self.sample_path,
            parse_dates=["date"],
            dtype={"code": str, "name": str},
        )
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["name"] = df["name"].astype(str)

        if "market_cap" not in df.columns:
            code_factor = df["code"].astype(str).str[-3:].astype(int).replace(0, 1)
            df["market_cap"] = (df["close"] * (2e7 + code_factor * 8e5)).astype(float)
            df.to_csv(self.sample_path, index=False)
        return df

    def _init_tdx(self):
        try:
            from app.market_data.tdx_provider import TdxDataProvider

            cfg = self.market_data_cfg.get("tdx", {}) or {}
            host = cfg.get("host") or None
            self.tdx = TdxDataProvider(
                host=host,
                port=int(cfg.get("port", 7709)),
                auto_retry=bool(cfg.get("auto_retry", True)),
                timeout=float(cfg.get("timeout", 1.5)),
            )
            self.provider_status = f"tdx {self.tdx.connected_server}"
        except Exception as exc:  # noqa: BLE001
            self.tdx = None
            self.last_provider_error = str(exc)
            self.provider_status = "sample fallback after tdx failure"
            if not self.fallback_to_sample:
                raise

    def _make_sample(self):
        self.sample_path.parent.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(7)
        rows = []
        codes = [f"{i:06d}" for i in range(600000, 600080)] + [f"{i:06d}" for i in range(1, 80)]
        for idx, code in enumerate(codes):
            name = f"样本{idx+1:03d}"
            base = rng.uniform(5, 60)
            dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=120)
            price = base
            free_float_shares = rng.uniform(2e7, 8e8)
            for d in dates:
                ret = rng.normal(0.0005, 0.025)
                open_ = price * (1 + rng.normal(0, 0.01))
                close = max(1, price * (1 + ret))
                high = max(open_, close) * (1 + rng.uniform(0, 0.03))
                low = min(open_, close) * (1 - rng.uniform(0, 0.03))
                vol = int(rng.uniform(2e5, 8e6))
                amount = vol * close
                market_cap = close * free_float_shares
                rows.append([d, code, name, open_, high, low, close, vol, amount, market_cap])
                price = close
        pd.DataFrame(
            rows,
            columns=["date", "code", "name", "open", "high", "low", "close", "volume", "amount", "market_cap"],
        ).to_csv(self.sample_path, index=False)

    def data_source_label(self) -> str:
        return self.provider_status

    def universe_source_label(self) -> str:
        return self.universe_status

    def _sample_universe(self) -> pd.DataFrame:
        latest = self.df.sort_values("date").groupby("code", as_index=False).tail(1).copy()
        latest["code"] = latest["code"].astype(str).str.zfill(6)
        latest["name"] = latest["name"].astype(str)
        latest["close"] = pd.to_numeric(latest["close"], errors="coerce")
        latest["amount"] = pd.to_numeric(latest["amount"], errors="coerce").fillna(0)
        latest["market_cap"] = pd.to_numeric(latest["market_cap"], errors="coerce")
        self.universe_status = "sample"
        
        if "pre_close" not in latest.columns:
            latest["pre_close"] = pd.NA
        if "is_limit_up" not in latest.columns:
            latest["is_limit_up"] = False
        latest["pct_chg"] = pd.to_numeric(latest.get("pct_chg", pd.Series(pd.NA, index=latest.index)), errors="coerce")
        return latest[["code", "name", "close", "amount", "market_cap", "pre_close", "is_limit_up", "pct_chg"]].sort_values("amount", ascending=False)

    def universe(self, force_refresh: bool = False):
        if self.selector_provider == "tdx" and self.tdx is not None:
            cache_path = Path(self.market_data_cfg.get("tdx_universe_cache_path", "data/tdx_universe.csv"))
            try:
                u = self.tdx.build_universe(cache_path=cache_path, force_refresh=force_refresh)
                if u is not None and not u.empty:
                    u["code"] = u["code"].astype(str).str.zfill(6)
                    u["name"] = u["name"].astype(str)
                    u["close"] = pd.to_numeric(u["close"], errors="coerce")
                    u["amount"] = pd.to_numeric(u["amount"], errors="coerce").fillna(0)
                    if "market_cap" not in u.columns:
                        u["market_cap"] = pd.NA
                    if "pre_close" not in u.columns:
                        u["pre_close"] = pd.NA
                    if "is_limit_up" not in u.columns:
                        u["is_limit_up"] = u.apply(self._is_limit_up_row, axis=1) if "pre_close" in u.columns else False
                    u["market_cap"] = pd.to_numeric(u["market_cap"], errors="coerce")
                    u["pre_close"] = pd.to_numeric(u["pre_close"], errors="coerce")
                    if "pct_chg" not in u.columns:
                        u["pct_chg"] = (u["close"] / u["pre_close"] - 1.0) * 100.0
                    u["pct_chg"] = pd.to_numeric(u["pct_chg"], errors="coerce")
                    self.universe_status = f"tdx universe ({len(u)} stocks)"
                    return u[["code", "name", "close", "amount", "market_cap", "pre_close", "is_limit_up", "pct_chg"]].sort_values("amount", ascending=False, na_position="last")
            except Exception as exc:  # noqa: BLE001
                self.last_universe_error = f"TDX universe failed: {exc}"
                if not self.fallback_to_sample:
                    raise
        return self._sample_universe()

    def search(self, keyword: str):
        u = self.universe()
        if not keyword:
            return u.head(50).reset_index(drop=True)
        keyword = keyword.strip()
        return u[
            u["code"].astype(str).str.contains(keyword, regex=False)
            | u["name"].astype(str).str.contains(keyword, case=False, regex=False)
        ].head(50).reset_index(drop=True)

    @staticmethod
    def has_valid_market_cap(u: pd.DataFrame) -> bool:
        if u.empty or "market_cap" not in u.columns:
            return False
        mc = pd.to_numeric(u["market_cap"], errors="coerce")
        return bool(mc.notna().any() and (mc.fillna(0) > 0).any())

    @staticmethod
    def _is_limit_up_row(row) -> bool:
        code = str(row.get("code", "")).zfill(6)
        name = str(row.get("name", ""))
        close = pd.to_numeric(row.get("close"), errors="coerce")
        pre_close = pd.to_numeric(row.get("pre_close"), errors="coerce")
        if pd.isna(close) or pd.isna(pre_close) or float(pre_close) <= 0:
            return False
        if "ST" in name.upper() or "退" in name:
            limit = 0.05
        elif code.startswith(("300", "301", "688")):
            limit = 0.20
        else:
            limit = 0.10
        return float(close) >= float(pre_close) * (1 + limit - 0.003)

    def _passes_shrink_volume_filter(self, code: str, ratio: float = 2.0 / 3.0) -> bool:
        """Return True if predicted/current daily volume is below both VOL-MA5 and VOL-MA10 times ratio.

        This is intentionally computed from daily bars plus the current quote, so it can
        work in the TDX-only personal-use mode. For intraday use, estimate_close_volume()
        turns current cumulative volume into an estimated full-day volume. After close,
        it returns final volume.
        """
        try:
            code = str(code).zfill(6)
            df = self.get_ohlcv(code, 30)
            if df.empty or "volume" not in df.columns:
                return False

            tmp = df.copy()
            tmp["date"] = pd.to_datetime(tmp["date"], errors="coerce")
            tmp["volume"] = pd.to_numeric(tmp["volume"], errors="coerce")
            tmp = tmp.dropna(subset=["date", "volume"]).sort_values("date")
            if tmp.empty:
                return False

            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            hist = tmp[tmp["date"].dt.date < today]
            if len(hist) < 10:
                # If the data source only exposes the latest completed bars, fall back
                # to excluding the last bar, so today's partial volume does not pollute MA.
                hist = tmp.iloc[:-1]
            if len(hist) < 10:
                return False

            vma5 = float(hist["volume"].tail(5).mean())
            vma10 = float(hist["volume"].tail(10).mean())
            pred = float(self.estimate_close_volume(code))
            threshold = float(ratio)
            return pred < vma5 * threshold and pred < vma10 * threshold
        except Exception as exc:  # noqa: BLE001
            self.last_universe_error = f"缩量筛选计算失败 {code}: {exc}"
            return False

    def _passes_ema_filter(
        self,
        code: str,
        short_period: int = 20,
        mid_period: int = 50,
        long_period: int = 100,
        require_short_above_mid: bool = True,
        require_mid_above_long: bool = True,
        require_price_above_short: bool = True,
    ) -> bool:
        try:
            short_period = max(1, int(short_period))
            mid_period = max(1, int(mid_period))
            long_period = max(1, int(long_period))
            fetch_n = max(short_period, mid_period, long_period) * 4
            fetch_n = max(fetch_n, max(short_period, mid_period, long_period) + 20)

            df = self.get_ohlcv(str(code).zfill(6), fetch_n)
            if df.empty or "close" not in df.columns:
                return False

            close = pd.to_numeric(df["close"], errors="coerce").dropna()
            if len(close) < max(short_period, mid_period, long_period):
                return False

            ema_short = float(close.ewm(span=short_period, adjust=False).mean().iloc[-1])
            ema_mid = float(close.ewm(span=mid_period, adjust=False).mean().iloc[-1])
            ema_long = float(close.ewm(span=long_period, adjust=False).mean().iloc[-1])
            latest_price = float(close.iloc[-1])

            if bool(require_short_above_mid) and not (ema_short > ema_mid):
                return False
            if bool(require_mid_above_long) and not (ema_mid > ema_long):
                return False
            if bool(require_price_above_short) and not (latest_price > ema_short):
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_universe_error = f"EMA筛选计算失败 {code}: {exc}"
            return False

    def filter_universe(
        self,
        min_price=0.0,
        max_price=9999.0,
        min_pct_chg=-999.0,
        max_pct_chg=999.0,
        min_market_cap_e8=0.0,
        max_market_cap_e8=999999.0,
        use_market_cap_filter=False,
        exclude_limit_up=False,
        excluded_prefixes=None,
        exclude_st=False,
        use_shrink_volume=False,
        shrink_volume_ratio=2.0 / 3.0,
        use_ema_filter=False,
        ema_short_period=20,
        ema_mid_period=50,
        ema_long_period=100,
        ema_require_short_above_mid=True,
        ema_require_mid_above_long=True,
        ema_require_price_above_short=True,
        top_n=0,
        sort_by="成交额从高到低",
        force_refresh=False,
    ):
        u = self.universe(force_refresh=force_refresh).copy()

        u["code"] = u["code"].astype(str).str.zfill(6)
        u["name"] = u["name"].astype(str)
        u["close"] = pd.to_numeric(u["close"], errors="coerce")
        u["amount"] = pd.to_numeric(u["amount"], errors="coerce").fillna(0)
        if "market_cap" not in u.columns:
            u["market_cap"] = pd.NA
        if "pre_close" not in u.columns:
            u["pre_close"] = pd.NA
        u["market_cap"] = pd.to_numeric(u["market_cap"], errors="coerce")
        u["pre_close"] = pd.to_numeric(u["pre_close"], errors="coerce")
        if "pct_chg" not in u.columns:
            u["pct_chg"] = (u["close"] / u["pre_close"] - 1.0) * 100.0
        u["pct_chg"] = pd.to_numeric(u["pct_chg"], errors="coerce")

        u = u[u["close"].between(float(min_price), float(max_price))].copy()
        if "pct_chg" in u.columns and u["pct_chg"].notna().any():
            u = u[u["pct_chg"].between(float(min_pct_chg), float(max_pct_chg))].copy()

        prefixes = [str(x).strip() for x in (excluded_prefixes or []) if str(x).strip()]
        if prefixes:
            mask = False
            for pref in prefixes:
                mask = mask | u["code"].str.startswith(pref)
            u = u[~mask].copy()

        if bool(exclude_st):
            name_upper = u["name"].astype(str).str.upper()
            u = u[~name_upper.str.contains("ST", regex=False)].copy()

        if bool(exclude_limit_up):
            if "is_limit_up" in u.columns:
                u = u[~u["is_limit_up"].fillna(False).astype(bool)].copy()
            else:
                u = u[~u.apply(self._is_limit_up_row, axis=1)].copy()

        if bool(use_market_cap_filter) and self.has_valid_market_cap(u):
            min_mc = float(min_market_cap_e8) * 1e8
            max_mc = float(max_market_cap_e8) * 1e8
            u = u[u["market_cap"].between(min_mc, max_mc)].copy()
        elif bool(use_market_cap_filter):
            self.last_universe_error = "当前 TDX-only 股票池暂无可用市值字段，已自动跳过市值筛选。"

        sort_map = {
            "成交额从高到低": "amount",
            "价格从高到低": "close",
            "代码从小到大": "code",
            "市值从高到低": "market_cap",
        }
        col = sort_map.get(sort_by, "amount")
        if col == "market_cap" and not self.has_valid_market_cap(u):
            col = "amount"
            self.last_universe_error = "当前 TDX-only 股票池暂无可用市值字段，已改为按成交额排序。"

        if col == "code":
            u = u.sort_values(col, ascending=True)
        else:
            u = u.sort_values(col, ascending=False, na_position="last")

        if int(top_n) > 0:
            u = u.head(int(top_n))

        if bool(use_shrink_volume):
            codes = u["code"].astype(str).str.zfill(6).tolist()
            keep_map = {code: self._passes_shrink_volume_filter(code, float(shrink_volume_ratio)) for code in codes}
            u = u[u["code"].map(keep_map).fillna(False)].copy()

        if bool(use_ema_filter):
            codes = u["code"].astype(str).str.zfill(6).tolist()
            keep_map = {
                code: self._passes_ema_filter(
                    code,
                    short_period=int(ema_short_period),
                    mid_period=int(ema_mid_period),
                    long_period=int(ema_long_period),
                    require_short_above_mid=bool(ema_require_short_above_mid),
                    require_mid_above_long=bool(ema_require_mid_above_long),
                    require_price_above_short=bool(ema_require_price_above_short),
                )
                for code in codes
            }
            u = u[u["code"].map(keep_map).fillna(False)].copy()

        return u.reset_index(drop=True)

    def get_ohlcv(self, code: str, n=60):
        code = str(code).zfill(6)
        if self.tdx is not None:
            try:
                df = self.tdx.get_daily_bars(code, n=int(n), adjust=self.adjust)
                if df is not None and not df.empty:
                    u = self.universe()
                    row = u[u["code"].astype(str).str.zfill(6) == code]
                    df["name"] = row["name"].iloc[0] if not row.empty else code
                    df["market_cap"] = float(row["market_cap"].iloc[0]) if not row.empty and pd.notna(row["market_cap"].iloc[0]) else np.nan
                    return df.sort_values("date").tail(n).copy()
            except Exception as exc:  # noqa: BLE001
                self.last_provider_error = f"TDX get_daily_bars failed: {exc}"
                if not self.fallback_to_sample:
                    raise
        return self.df[self.df["code"] == code].sort_values("date").tail(n).copy()

    def latest_quote(self, code: str) -> dict:
        code = str(code).zfill(6)
        if self.tdx is not None:
            try:
                q = self.tdx.get_realtime_quote([code])
                if q is not None and not q.empty:
                    return q.iloc[0].to_dict()
            except Exception as exc:  # noqa: BLE001
                self.last_provider_error = f"TDX quote failed: {exc}"
        df = self.get_ohlcv(code, 1)
        if df.empty:
            return {"code": code, "last": None, "volume_shares": None, "amount": None, "source": "empty"}
        r = df.iloc[-1]
        return {
            "code": code,
            "last": float(r["close"]),
            "open": float(r.get("open", r["close"])),
            "high": float(r.get("high", r["close"])),
            "low": float(r.get("low", r["close"])),
            "pre_close": None,
            "volume_shares": float(r.get("volume", 0.0)),
            "amount": float(r.get("amount", 0.0)),
            "bid1": None,
            "ask1": None,
            "source": "sample",
        }

    def latest_price(self, code: str) -> float:
        q = self.latest_quote(code)
        if q.get("last") is not None and not pd.isna(q.get("last")):
            return float(q["last"])
        x = self.get_ohlcv(code, 1)
        return float(x["close"].iloc[-1])

    def estimate_today_close(self, code: str) -> float:
        # 保留给 position-manager 作为临时下单参考价；界面不显示它。
        q = self.latest_quote(code)
        if q.get("last") is not None and not pd.isna(q.get("last")):
            return float(q["last"])
        df = self.get_ohlcv(code, 20)
        last = float(df["close"].iloc[-1])
        mom5 = df["close"].pct_change(5).iloc[-1]
        return float(last * (1 + np.clip(mom5 / 5, -0.02, 0.02)))

    @staticmethod
    def _trading_minutes_elapsed(now: datetime | None = None) -> int:
        # Always use China A-share market time, regardless of the user's local timezone.
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            now = now.astimezone(ZoneInfo("Asia/Shanghai"))

        if now.weekday() >= 5:
            return 240

        t = now.time()
        total = 0
        if t > time(9, 30):
            end = min(now, now.replace(hour=11, minute=30, second=0, microsecond=0))
            start = now.replace(hour=9, minute=30, second=0, microsecond=0)
            total += max(0, int((end - start).total_seconds() // 60))
        if t > time(13, 0):
            end = min(now, now.replace(hour=15, minute=0, second=0, microsecond=0))
            start = now.replace(hour=13, minute=0, second=0, microsecond=0)
            total += max(0, int((end - start).total_seconds() // 60))
        return min(total, 240)

    def estimate_close_volume(self, code: str) -> float:
        """Estimate today's closing daily volume in shares."""
        q = self.latest_quote(code)
        current_volume = q.get("volume_shares")
        elapsed = self._trading_minutes_elapsed()
        if current_volume is not None and not pd.isna(current_volume) and elapsed > 0:
            linear_est = float(current_volume) / max(elapsed, 1) * 240.0
            if elapsed >= 240:
                return float(current_volume)
            return float(max(current_volume, linear_est))

        df = self.get_ohlcv(code, 20)
        if df.empty:
            return 0.0
        v5 = df["volume"].tail(5).mean()
        v20 = df["volume"].tail(20).mean()
        last = df["volume"].iloc[-1]
        estimate = 0.5 * v5 + 0.3 * v20 + 0.2 * last
        return float(max(0, estimate))

    def pre_selector(self, min_turnover=5e7, min_price=3, max_price=80, top_n=80):
        u = self.universe().copy()
        u = u[(u["amount"] >= min_turnover) & (u["close"].between(min_price, max_price))]
        return u.head(top_n).reset_index(drop=True)

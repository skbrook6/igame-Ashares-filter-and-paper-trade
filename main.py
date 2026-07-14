import json
from datetime import datetime
from pathlib import Path
import yaml
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

from app.data_provider import DataProvider
from app.storage import (
    get_conn,
    df_query,
    add_decision,
    delete_decision,
    clear_decisions,
    deduplicate_decisions,
    get_cash,
    reset_paper_account,
    log_account_equity_snapshot,
    get_account_equity_curve,
    clear_operation_decisions_for_results,
)
from app.position_manager import PositionManager
from app.adapters.paper_broker import PaperBroker
from app.order_pusher import OrderPusher

st.set_page_config(page_title="K-lines Gamer Trading MVP", layout="wide")

@st.cache_resource
def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()
conn = get_conn(cfg["app"]["database_path"])
deduplicate_decisions(conn)
dp = DataProvider(cfg["app"]["sample_data_path"], cfg.get("market_data", {}))


FILTER_PRESETS_PATH = Path("data/filter_presets.json")
DEFAULT_FILTER_PARAMS = {
    "min_price": 3.0,
    "max_price": 80.0,
    "min_pct_chg": -30.0,
    "max_pct_chg": 30.0,
    "use_market_cap_filter": False,
    "min_market_cap_e8": 20.0,
    "max_market_cap_e8": 5000.0,
    "exclude_limit_up": True,
    "exclude_st": True,
    "excluded_prefix_labels": [],
    "use_shrink_volume": False,
    "use_ema_filter": False,
    "ema_short_period": 20,
    "ema_mid_period": 50,
    "ema_long_period": 100,
    "ema_require_short_above_mid": True,
    "ema_require_mid_above_long": True,
    "ema_require_price_above_short": True,
    "top_n": 0,  # 0 = no limit
    "sort_by": "成交额从高到低",
    "force_refresh": False,
}


def normalize_filter_params(params: dict | None) -> dict:
    merged = DEFAULT_FILTER_PARAMS.copy()
    if isinstance(params, dict):
        merged.update({k: v for k, v in params.items() if k in merged})
    merged["excluded_prefix_labels"] = list(merged.get("excluded_prefix_labels") or [])
    return merged


def load_filter_preset_store() -> dict:
    if not FILTER_PRESETS_PATH.exists():
        return {"last_used": DEFAULT_FILTER_PARAMS.copy(), "presets": {}}
    try:
        data = json.loads(FILTER_PRESETS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"last_used": DEFAULT_FILTER_PARAMS.copy(), "presets": {}}
    presets = data.get("presets", {}) if isinstance(data, dict) else {}
    if not isinstance(presets, dict):
        presets = {}
    return {
        "last_used": normalize_filter_params(data.get("last_used") if isinstance(data, dict) else None),
        "presets": {str(k): normalize_filter_params(v) for k, v in presets.items() if isinstance(v, dict)},
    }


def save_filter_preset_store(store: dict):
    FILTER_PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_used": normalize_filter_params(store.get("last_used")),
        "presets": {
            str(k): normalize_filter_params(v)
            for k, v in (store.get("presets") or {}).items()
            if str(k).strip()
        },
    }
    FILTER_PRESETS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_last_filter_params(params: dict):
    store = load_filter_preset_store()
    store["last_used"] = normalize_filter_params(params)
    save_filter_preset_store(store)


# ---------- session state ----------
def init_state():
    if "judge_pool" not in st.session_state:
        st.session_state.judge_pool = pd.DataFrame()
    if "judge_idx" not in st.session_state:
        st.session_state.judge_idx = 0
    if "position_idx" not in st.session_state:
        st.session_state.position_idx = 0
    if "non_operation_idx" not in st.session_state:
        st.session_state.non_operation_idx = 0
    if "kline_offset" not in st.session_state:
        st.session_state.kline_offset = 0
    if "last_code" not in st.session_state:
        st.session_state.last_code = ""
    if "filter_params" not in st.session_state:
        st.session_state.filter_params = load_filter_preset_store()["last_used"]
    if "filter_form_version" not in st.session_state:
        st.session_state.filter_form_version = 0
    if "hotkeys" not in st.session_state:
        # v5.1 default: A/D shift kline window, W/S prev/next stock, J/K action buttons.
        st.session_state.hotkeys = {
            "shift_left": "A",
            "shift_right": "D",
            "previous": "W",
            "next": "S",
            "left_action": "J",
            "right_action": "K",
        }
    if "show_ma" not in st.session_state:
        st.session_state.show_ma = True
    if "chart_colors" not in st.session_state:
        # A-share default: red = up / profit, green = down / loss.
        st.session_state.chart_colors = {
            "up": "#e74c3c",
            "down": "#2ecc71",
        }
    if "indicator_settings" not in st.session_state:
        st.session_state.indicator_settings = {
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "shrink_volume_ratio": 2.0 / 3.0,
        }


init_state()


BOARD_PREFIX_OPTIONS = {
    "000 深市主板": "000",
    "001 深市主板": "001",
    "002 深市/中小板": "002",
    "003 深市主板": "003",
    "300 创业板": "300",
    "301 创业板": "301",
    "600 沪市主板": "600",
    "601 沪市主板": "601",
    "603 沪市主板": "603",
    "605 沪市主板": "605",
    "688 科创板": "688",
}


def reset_judge_pool():
    st.session_state.judge_pool = pd.DataFrame()
    st.session_state.judge_idx = 0
    st.session_state.kline_offset = 0


def reset_chart_offset_if_code_changed(code: str):
    code = str(code).zfill(6)
    if st.session_state.last_code != code:
        st.session_state.kline_offset = 0
        st.session_state.last_code = code


@st.dialog("键位设置")
def hotkey_dialog():
    st.caption("键盘全流程：A/D 平移K线窗口，W/S 切换股票，J/K 执行动作。")
    h = st.session_state.hotkeys
    c1, c2 = st.columns(2)
    with c1:
        shift_left = st.text_input("K线左移：去掉最新K线，补61天前", value=h.get("shift_left", "A"), max_chars=1)
        previous = st.text_input("上一只股票", value=h.get("previous", "W"), max_chars=1)
        left_action = st.text_input("左侧动作：买入 / 卖出", value=h.get("left_action", "J"), max_chars=1)
    with c2:
        shift_right = st.text_input("K线右移：回到更新K线", value=h.get("shift_right", "D"), max_chars=1)
        next_key = st.text_input("下一只股票", value=h.get("next", "S"), max_chars=1)
        right_action = st.text_input("右侧动作：观望 / 持有", value=h.get("right_action", "K"), max_chars=1)

    c3, c4 = st.columns(2)
    if c3.button("保存键位", type="primary", use_container_width=True):
        new = {
            "shift_left": (shift_left or "A").strip().upper()[:1],
            "shift_right": (shift_right or "D").strip().upper()[:1],
            "previous": (previous or "W").strip().upper()[:1],
            "next": (next_key or "S").strip().upper()[:1],
            "left_action": (left_action or "J").strip().upper()[:1],
            "right_action": (right_action or "K").strip().upper()[:1],
        }
        vals = list(new.values())
        if len(vals) != len(set(vals)):
            st.error("键位不能重复。")
            st.stop()
        st.session_state.hotkeys = new
        st.rerun()
    if c4.button("恢复默认 A/D/W/S/J/K", use_container_width=True):
        st.session_state.hotkeys = {
            "shift_left": "A",
            "shift_right": "D",
            "previous": "W",
            "next": "S",
            "left_action": "J",
            "right_action": "K",
        }
        st.rerun()


@st.dialog("重置 paper trading 账户")
def reset_paper_dialog():
    initial_cash = float(cfg.get("paper_trading", {}).get("initial_cash", 1000000.0))
    st.warning("这会删除 paper trading 的交易记录、持仓、操作标签，并把现金重置为初始本金。")
    st.write(f"初始本金：{initial_cash:,.2f}")
    confirm = st.checkbox("我确认要重置 paper trading 账户")
    c1, c2 = st.columns(2)
    if c1.button("确认重置", type="primary", use_container_width=True, disabled=not confirm):
        reset_paper_account(conn, initial_cash)
        reset_judge_pool()
        st.success("paper trading 账户已重置。")
        st.rerun()
    if c2.button("取消", use_container_width=True):
        st.rerun()


@st.dialog("生成待判断股票清单")
def filter_dialog():
    p = normalize_filter_params(st.session_state.filter_params)
    st.session_state.filter_params = p
    form_version = int(st.session_state.get("filter_form_version", 0))
    def fk(name: str) -> str:
        return f"filter_{name}_{form_version}"

    st.caption("v5.2 使用 TDX 真实股票池。最多股票数填 0 表示不限制；排序方式决定清单顺序。")

    store = load_filter_preset_store()
    preset_names = sorted(store.get("presets", {}).keys())
    if preset_names:
        preset_col, load_col, delete_col = st.columns([2, 1, 1])
        selected_preset = preset_col.selectbox("筛选预设", preset_names)
        if load_col.button("加载预设", use_container_width=True):
            st.session_state.filter_params = normalize_filter_params(store["presets"][selected_preset])
            st.session_state.filter_form_version = form_version + 1
            save_last_filter_params(st.session_state.filter_params)
            st.rerun()
        if delete_col.button("删除预设", use_container_width=True):
            store["presets"].pop(selected_preset, None)
            save_filter_preset_store(store)
            st.rerun()
    else:
        st.caption("暂无筛选预设；设置好参数后可在下方保存。")

    min_price = st.number_input("最低价格", min_value=0.0, value=float(p["min_price"]), step=0.5, key=fk("min_price"))
    max_price = st.number_input("最高价格", min_value=0.0, value=float(p["max_price"]), step=0.5, key=fk("max_price"))

    st.markdown("**今日涨跌幅筛选（%）**")
    pct_c1, pct_c2 = st.columns(2)
    with pct_c1:
        min_pct_chg = st.number_input(
            "最低今日涨跌幅 %",
            value=float(p.get("min_pct_chg", -30.0)),
            step=0.5,
            help="例如 -3 表示今日跌幅不低于 -3%。",
            key=fk("min_pct_chg"),
        )
    with pct_c2:
        max_pct_chg = st.number_input(
            "最高今日涨跌幅 %",
            value=float(p.get("max_pct_chg", 30.0)),
            step=0.5,
            help="例如 7 表示今日涨幅不高于 7%。",
            key=fk("max_pct_chg"),
        )

    use_market_cap_filter = st.checkbox(
        "启用市值筛选（TDX-only 当前可能无市值，无法获取时会自动跳过）",
        value=bool(p.get("use_market_cap_filter", False)),
        key=fk("use_market_cap_filter"),
    )

    min_mc = st.number_input(
        "最低市值（亿元）",
        min_value=0.0,
        value=float(p["min_market_cap_e8"]),
        step=10.0,
        disabled=not use_market_cap_filter,
        key=fk("min_market_cap_e8"),
    )
    max_mc = st.number_input(
        "最高市值（亿元）",
        min_value=0.0,
        value=float(p["max_market_cap_e8"]),
        step=10.0,
        disabled=not use_market_cap_filter,
        key=fk("max_market_cap_e8"),
    )

    exclude_limit_up = st.checkbox("排除已涨停股票", value=bool(p.get("exclude_limit_up", True)), key=fk("exclude_limit_up"))
    exclude_st = st.checkbox("排除 ST / *ST 股票", value=bool(p.get("exclude_st", True)), key=fk("exclude_st"))
    excluded_prefix_labels = st.multiselect(
        "排除特殊板块 / 代码前缀",
        list(BOARD_PREFIX_OPTIONS.keys()),
        default=p.get("excluded_prefix_labels", []),
        help="例如排除 300/301 创业板、688 科创板，或按你的交易范围排除某些代码段。",
        key=fk("excluded_prefix_labels"),
    )

    shrink_ratio = float(st.session_state.indicator_settings.get("shrink_volume_ratio", 2.0 / 3.0))
    use_shrink_volume = st.checkbox(
        f"筛选缩量：预测成交量 < MA5/MA10 × {shrink_ratio:.2f}",
        value=bool(p.get("use_shrink_volume", False)),
        help="用当前/预测日成交量分别和历史成交量5日、10日均线比较。阈值参数可在 settings 调整。",
        key=fk("use_shrink_volume"),
    )

    st.markdown("**EMA / EXPMA 筛选**")
    use_ema_filter = st.checkbox(
        "启用 EMA 趋势筛选",
        value=bool(p.get("use_ema_filter", False)),
        help="按日 K 收盘价计算 EMA。默认条件：短期EMA > 中期EMA，中期EMA > 长期EMA，现价 > 短期EMA。",
        key=fk("use_ema_filter"),
    )
    ema_c1, ema_c2, ema_c3 = st.columns(3)
    with ema_c1:
        ema_short_period = st.number_input(
            "短期 EMA",
            min_value=1,
            max_value=250,
            value=int(p.get("ema_short_period", 20)),
            step=1,
            disabled=not use_ema_filter,
            key=fk("ema_short_period"),
        )
    with ema_c2:
        ema_mid_period = st.number_input(
            "中期 EMA",
            min_value=1,
            max_value=500,
            value=int(p.get("ema_mid_period", 50)),
            step=1,
            disabled=not use_ema_filter,
            key=fk("ema_mid_period"),
        )
    with ema_c3:
        ema_long_period = st.number_input(
            "长期 EMA",
            min_value=1,
            max_value=1000,
            value=int(p.get("ema_long_period", 100)),
            step=1,
            disabled=not use_ema_filter,
            key=fk("ema_long_period"),
        )
    ema_rule_c1, ema_rule_c2, ema_rule_c3 = st.columns(3)
    with ema_rule_c1:
        ema_require_short_above_mid = st.checkbox(
            "短期 EMA > 中期 EMA",
            value=bool(p.get("ema_require_short_above_mid", True)),
            disabled=not use_ema_filter,
            key=fk("ema_require_short_above_mid"),
        )
    with ema_rule_c2:
        ema_require_mid_above_long = st.checkbox(
            "中期 EMA > 长期 EMA",
            value=bool(p.get("ema_require_mid_above_long", True)),
            disabled=not use_ema_filter,
            key=fk("ema_require_mid_above_long"),
        )
    with ema_rule_c3:
        ema_require_price_above_short = st.checkbox(
            "现价 > 短期 EMA",
            value=bool(p.get("ema_require_price_above_short", True)),
            disabled=not use_ema_filter,
            key=fk("ema_require_price_above_short"),
        )

    sort_options = ["成交额从高到低", "价格从高到低", "代码从小到大", "市值从高到低"]
    sort_by = st.selectbox(
        "排序方式",
        sort_options,
        index=sort_options.index(p.get("sort_by", "成交额从高到低"))
        if p.get("sort_by", "成交额从高到低") in sort_options else 0,
        key=fk("sort_by"),
    )

    top_n = st.number_input(
        "最多股票数（0 = 不限制）",
        min_value=0,
        value=int(p.get("top_n", 0)),
        step=50,
        key=fk("top_n"),
    )

    force_refresh = st.checkbox(
        "强制刷新 TDX 股票池缓存",
        value=bool(p.get("force_refresh", False)),
        key=fk("force_refresh"),
    )

    current_filter_params = {
        "min_price": min_price,
        "max_price": max_price,
        "min_pct_chg": min_pct_chg,
        "max_pct_chg": max_pct_chg,
        "use_market_cap_filter": use_market_cap_filter,
        "min_market_cap_e8": min_mc,
        "max_market_cap_e8": max_mc,
        "exclude_limit_up": exclude_limit_up,
        "exclude_st": exclude_st,
        "excluded_prefix_labels": excluded_prefix_labels,
        "use_shrink_volume": use_shrink_volume,
        "use_ema_filter": use_ema_filter,
        "ema_short_period": int(ema_short_period),
        "ema_mid_period": int(ema_mid_period),
        "ema_long_period": int(ema_long_period),
        "ema_require_short_above_mid": ema_require_short_above_mid,
        "ema_require_mid_above_long": ema_require_mid_above_long,
        "ema_require_price_above_short": ema_require_price_above_short,
        "top_n": int(top_n),
        "sort_by": sort_by,
        "force_refresh": force_refresh,
    }

    st.markdown("**预设**")
    preset_name = st.text_input("预设名称", value="我的筛选", key=fk("preset_name"))
    save_col, last_col = st.columns(2)
    if save_col.button("保存当前为预设", use_container_width=True):
        name = preset_name.strip()
        if not name:
            st.error("预设名称不能为空。")
            st.stop()
        store = load_filter_preset_store()
        store.setdefault("presets", {})[name] = normalize_filter_params(current_filter_params)
        store["last_used"] = normalize_filter_params(current_filter_params)
        save_filter_preset_store(store)
        st.session_state.filter_params = normalize_filter_params(current_filter_params)
        st.session_state.filter_form_version = form_version + 1
        st.success(f"已保存预设：{name}")
        st.rerun()
    if last_col.button("保存为下次默认", use_container_width=True):
        st.session_state.filter_params = normalize_filter_params(current_filter_params)
        st.session_state.filter_form_version = form_version + 1
        save_last_filter_params(current_filter_params)
        st.success("已保存为下次默认筛选。")
        st.rerun()

    c1, c2 = st.columns(2)
    if c1.button("确认生成", type="primary", use_container_width=True):
        excluded_prefixes = [BOARD_PREFIX_OPTIONS[x] for x in excluded_prefix_labels]
        st.session_state.filter_params = normalize_filter_params(current_filter_params)
        save_last_filter_params(current_filter_params)
        if use_shrink_volume and int(top_n) <= 0:
            st.warning("已开启缩量筛选且未限制最多股票数，可能需要几分钟。")
        with st.spinner("正在生成待判断股票清单，请稍等..."):
            st.session_state.judge_pool = dp.filter_universe(
                min_price=min_price,
                max_price=max_price,
                min_pct_chg=min_pct_chg,
                max_pct_chg=max_pct_chg,
                min_market_cap_e8=min_mc,
                max_market_cap_e8=max_mc,
                use_market_cap_filter=use_market_cap_filter,
                exclude_limit_up=exclude_limit_up,
                exclude_st=exclude_st,
                excluded_prefixes=excluded_prefixes,
                use_shrink_volume=use_shrink_volume,
                shrink_volume_ratio=shrink_ratio,
                use_ema_filter=use_ema_filter,
                ema_short_period=int(ema_short_period),
                ema_mid_period=int(ema_mid_period),
                ema_long_period=int(ema_long_period),
                ema_require_short_above_mid=ema_require_short_above_mid,
                ema_require_mid_above_long=ema_require_mid_above_long,
                ema_require_price_above_short=ema_require_price_above_short,
                top_n=int(top_n),
                sort_by=sort_by,
                force_refresh=force_refresh,
            )
        st.session_state.judge_idx = 0
        st.session_state.kline_offset = 0
        st.rerun()
    if c2.button("取消", use_container_width=True):
        st.rerun()


def get_current_pool(keyword: str):
    pool = st.session_state.judge_pool
    if not pool.empty:
        return pool.copy(), True
    return dp.search(keyword), False


def slice_kline_window(code: str, window: int = 60, fetch_n: int = 260):
    """Fetch enough bars, calculate MA on the full raw series, then slice the visible 60 bars.

    MA5/MA10/MA20 need history before the first visible bar, so they must be computed
    before slicing.
    """
    raw = dp.get_ohlcv(code, fetch_n)
    if raw.empty:
        return raw, 0

    raw = raw.sort_values("date").reset_index(drop=True)
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce")
    raw["ma5"] = raw["close"].rolling(5, min_periods=5).mean()
    raw["ma10"] = raw["close"].rolling(10, min_periods=10).mean()
    raw["ma20"] = raw["close"].rolling(20, min_periods=20).mean()
    raw["vma5"] = raw["volume"].rolling(5, min_periods=5).mean()
    raw["vma10"] = raw["volume"].rolling(10, min_periods=10).mean()

    ind = st.session_state.indicator_settings
    fast = max(1, int(ind.get("macd_fast", 12)))
    slow = max(fast + 1, int(ind.get("macd_slow", 26)))
    signal = max(1, int(ind.get("macd_signal", 9)))
    ema_fast = raw["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = raw["close"].ewm(span=slow, adjust=False).mean()
    raw["macd_dif"] = ema_fast - ema_slow
    raw["macd_dea"] = raw["macd_dif"].ewm(span=signal, adjust=False).mean()
    raw["macd_hist"] = (raw["macd_dif"] - raw["macd_dea"]) * 2

    max_offset = max(0, len(raw) - int(window))
    st.session_state.kline_offset = max(0, min(int(st.session_state.kline_offset), max_offset))
    offset = int(st.session_state.kline_offset)
    end = len(raw) - offset
    start = max(0, end - int(window))
    return raw.iloc[start:end].copy(), max_offset


def trade_markers_for_code(code: str, df_plot: pd.DataFrame) -> pd.DataFrame:
    orders = df_query(
        conn,
        "SELECT code, side, price, shares, status, created_at FROM orders WHERE code=? AND status='FILLED' ORDER BY created_at ASC",
        (str(code).zfill(6),),
    )
    if orders.empty or df_plot.empty:
        return pd.DataFrame()

    chart_dates = pd.to_datetime(df_plot["date"]).dt.normalize().tolist()
    chart_str = df_plot["date_str"].tolist()
    if not chart_dates:
        return pd.DataFrame()

    first_visible = chart_dates[0]
    last_visible = chart_dates[-1]
    rows = []
    for r in orders.itertuples():
        od = pd.to_datetime(r.created_at, errors="coerce")
        if pd.isna(od):
            continue
        od_norm = od.normalize()

        # Fix: if the chart window has been shifted back before the fill date,
        # do not pin the marker to the chart edge. Only show fills within the
        # currently visible date range.
        if od_norm < first_visible or od_norm > last_visible:
            continue

        idx = None
        for i, d in enumerate(chart_dates):
            if d == od_norm:
                idx = i
                break
        if idx is None:
            prior = [i for i, d in enumerate(chart_dates) if d <= od_norm]
            if not prior:
                continue
            idx = prior[-1]
        rows.append({"date_str": chart_str[idx], "side": r.side, "price": float(r.price), "shares": int(r.shares)})
    return pd.DataFrame(rows)


def plot_kline(
    df,
    code=None,
    show_ma: bool = True,
    current_volume: float | None = None,
    estimated_close_volume: float | None = None,
):
    """Draw price K-line and volume in two stacked panels.

    v6 chart rules:
    - x axis is categorical so weekends / holidays do not create gaps.
    - fixed visual proportion: width:height target ≈ 2.7:1 when Streamlit uses full width.
    - K-line panel : volume panel height ≈ 2:1.
    - red = up, green = down by default; colors can be changed in settings.
    - B/S fills are shown near the K-line x-axis only, not on candle bodies.
    """
    up_color = st.session_state.chart_colors.get("up", "#e74c3c")
    down_color = st.session_state.chart_colors.get("down", "#2ecc71")

    df_plot = df.copy()
    df_plot["date"] = pd.to_datetime(df_plot["date"])
    df_plot["date_str"] = df_plot["date"].dt.strftime("%Y-%m-%d")

    for col in ["open", "high", "low", "close", "volume"]:
        df_plot[col] = pd.to_numeric(df_plot[col], errors="coerce")

    df_plot["is_up"] = df_plot["close"] >= df_plot["open"]
    candle_colors = [up_color if x else down_color for x in df_plot["is_up"]]
    volume_fill_colors = ["rgba(0,0,0,0)" if x else down_color for x in df_plot["is_up"]]
    volume_line_colors = [up_color if x else down_color for x in df_plot["is_up"]]

    hover_text = []
    for r in df_plot.itertuples():
        hover_text.append(
            f"时间: {r.date_str}<br>"
            f"Open: {float(r.open):.2f}<br>"
            f"Close: {float(r.close):.2f}<br>"
            f"High: {float(r.high):.2f}<br>"
            f"Low: {float(r.low):.2f}<br>"
            f"Volume: {float(r.volume):,.0f}"
        )

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.56, 0.25, 0.19],  # K-line, volume, MACD
    )

    fig.add_trace(
        go.Candlestick(
            x=df_plot["date_str"],
            open=df_plot["open"],
            high=df_plot["high"],
            low=df_plot["low"],
            close=df_plot["close"],
            name="K线",
            # Up candles are hollow; down candles remain solid.
            increasing=dict(line=dict(color=up_color, width=1.2), fillcolor="rgba(0,0,0,0)"),
            decreasing=dict(line=dict(color=down_color, width=1.2), fillcolor=down_color),
            hovertext=hover_text,
            hoverinfo="text",
        ),
        row=1,
        col=1,
    )

    if show_ma:
        ma_specs = [
            ("ma5", "MA5", "#f1c40f"),   # yellow
            ("ma10", "MA10", "#3498db"), # blue
            ("ma20", "MA20", "#9b59b6"), # purple
        ]
        for col, label, color in ma_specs:
            if col in df_plot.columns:
                fig.add_trace(
                    go.Scatter(
                        x=df_plot["date_str"],
                        y=df_plot[col],
                        mode="lines",
                        name=label,
                        line=dict(width=1.8, color=color),
                        connectgaps=False,
                        hoverinfo="skip",
                    ),
                    row=1,
                    col=1,
                )

    solid_volume = df_plot["volume"].astype(float).copy()
    predicted_extra = pd.Series(0.0, index=df_plot.index)

    if len(df_plot) > 0:
        last_idx = df_plot.index[-1]
        cur_vol = solid_volume.loc[last_idx]
        if current_volume is not None and pd.notna(current_volume):
            cur_vol = max(0.0, float(current_volume))
            solid_volume.loc[last_idx] = cur_vol

        est_vol = None
        if estimated_close_volume is not None and pd.notna(estimated_close_volume):
            est_vol = max(0.0, float(estimated_close_volume))

        # If market is closed, estimate_close_volume returns current/final volume,
        # so predicted_extra is zero and the whole bar stays solid.
        if est_vol is not None and est_vol > cur_vol:
            predicted_extra.loc[last_idx] = est_vol - cur_vol

    fig.add_trace(
        go.Bar(
            x=df_plot["date_str"],
            y=solid_volume,
            name="成交量",
            # Up-volume bars are hollow; down-volume bars remain solid.
            marker=dict(
                color=volume_fill_colors,
                line=dict(color=volume_line_colors, width=1.1),
            ),
            hovertemplate="时间: %{x}<br>成交量: %{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    if show_ma:
        volume_ma_specs = [
            ("vma5", "VOL MA5", "#f1c40f"),
            ("vma10", "VOL MA10", "#3498db"),
        ]
        for col, label, color in volume_ma_specs:
            if col in df_plot.columns:
                fig.add_trace(
                    go.Scatter(
                        x=df_plot["date_str"],
                        y=df_plot[col],
                        mode="lines",
                        name=label,
                        line=dict(width=1.4, color=color),
                        connectgaps=False,
                        hoverinfo="skip",
                    ),
                    row=2,
                    col=1,
                )

    if predicted_extra.max() > 0:
        last_color = candle_colors[-1] if candle_colors else up_color
        fig.add_trace(
            go.Bar(
                x=df_plot["date_str"],
                y=predicted_extra,
                base=solid_volume,
                name="预测增量",
                marker=dict(
                    color="rgba(0,0,0,0)",
                    line=dict(color=last_color, width=1.6),
                    pattern=dict(shape="/", fgcolor=last_color, bgcolor="rgba(0,0,0,0)", solidity=0.18),
                ),
                hovertemplate="时间: %{x}<br>预测增量: %{y:,.0f}<extra></extra>",
            ),
            row=2,
            col=1,
        )

    if {"macd_dif", "macd_dea", "macd_hist"}.issubset(df_plot.columns):
        # MACD histogram is drawn as thin vertical lines rather than bars.
        macd_pos_x, macd_pos_y = [], []
        macd_neg_x, macd_neg_y = [], []
        for x_value, hist_value in zip(df_plot["date_str"], df_plot["macd_hist"].fillna(0.0)):
            hist_value = float(hist_value)
            if hist_value >= 0:
                macd_pos_x += [x_value, x_value, None]
                macd_pos_y += [0, hist_value, None]
            else:
                macd_neg_x += [x_value, x_value, None]
                macd_neg_y += [0, hist_value, None]

        fig.add_trace(
            go.Scatter(
                x=macd_pos_x,
                y=macd_pos_y,
                mode="lines",
                name="MACD+",
                line=dict(color=up_color, width=1.2),
                hoverinfo="skip",
            ),
            row=3,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=macd_neg_x,
                y=macd_neg_y,
                mode="lines",
                name="MACD-",
                line=dict(color=down_color, width=1.2),
                hoverinfo="skip",
            ),
            row=3,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df_plot["date_str"],
                y=df_plot["macd_dif"],
                mode="lines",
                name="DIF",
                line=dict(width=1.3, color="#f1c40f"),
                hovertemplate="时间: %{x}<br>DIF: %{y:.4f}<extra></extra>",
            ),
            row=3,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=df_plot["date_str"],
                y=df_plot["macd_dea"],
                mode="lines",
                name="DEA",
                line=dict(width=1.3, color="#3498db"),
                hovertemplate="时间: %{x}<br>DEA: %{y:.4f}<extra></extra>",
            ),
            row=3,
            col=1,
        )

    # B/S markers: show them on the price chart x-axis line only.
    if code:
        markers = trade_markers_for_code(str(code).zfill(6), df_plot)
        if not markers.empty:
            for r in markers.itertuples():
                side = str(r.side).upper()
                txt = "B" if side == "BUY" else "S"
                color = up_color if side == "BUY" else down_color
                fig.add_annotation(
                    x=r.date_str,
                    y=0.335,
                    xref="x",
                    yref="paper",
                    text=txt,
                    showarrow=False,
                    font=dict(color="white", size=10),
                    bgcolor=color,
                    bordercolor=color,
                    borderpad=3,
                    opacity=0.95,
                    hovertext=f"{side}<br>时间: {r.date_str}<br>价格: {float(r.price):.2f}",
                )

    fig.update_layout(
        width=1350,
        height=620,
        barmode="stack",
        bargap=0.25,
        hovermode="x unified",
        hoverlabel=dict(align="left"),
        margin=dict(l=20, r=20, t=20, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(type="category", rangeslider=dict(visible=False), row=1, col=1, showticklabels=False)
    fig.update_xaxes(type="category", row=2, col=1, showticklabels=False)
    fig.update_xaxes(type="category", nticks=8, tickangle=0, row=3, col=1)
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1, showgrid=True)
    fig.update_yaxes(title_text="MACD", row=3, col=1, showgrid=True, zeroline=True)
    return fig

def is_holding_stock(code: str):
    pos = df_query(
        conn,
        "SELECT code, name, shares, avg_cost, opened_at FROM positions WHERE code=?",
        (str(code).zfill(6),),
    )
    is_holding = (not pos.empty) and int(pos.iloc[0]["shares"]) > 0
    return is_holding, pos


def account_total_value():
    cash = get_cash(conn)
    positions = df_query(conn, "SELECT * FROM positions")
    stock_value = 0.0
    if not positions.empty:
        for r in positions.itertuples():
            try:
                stock_value += float(dp.latest_price(r.code)) * int(r.shares)
            except Exception:
                stock_value += float(r.avg_cost) * int(r.shares)
    return cash + stock_value, stock_value


def pnl_html(pnl: float, pct: float, prefix: str = ""):
    up_color = st.session_state.chart_colors.get("up", "#e74c3c")
    down_color = st.session_state.chart_colors.get("down", "#2ecc71")
    color = up_color if pnl >= 0 else down_color  # A股习惯：红涨绿跌
    sign = "+" if pnl >= 0 else ""
    return f"<span style='font-size:24px;font-weight:700;color:{color}'>{prefix}{sign}{pnl:,.2f} ({sign}{pct*100:.2f}%)</span>"


def style_pnl_table(df: pd.DataFrame):
    def color_pnl(v):
        try:
            x = float(v)
            up_color = st.session_state.chart_colors.get("up", "#e74c3c")
            down_color = st.session_state.chart_colors.get("down", "#2ecc71")
            return f"color: {up_color}; font-weight: 700" if x >= 0 else f"color: {down_color}; font-weight: 700"
        except Exception:
            return ""
    return df.style.format({
        "avg_cost": "{:.2f}",
        "last_price": "{:.2f}",
        "market_value": "{:,.2f}",
        "pnl": "{:,.2f}",
        "pnl_pct": "{:.2%}",
    }).map(color_pnl, subset=["pnl", "pnl_pct"])


def _price_series_for_code(code: str, start_date, end_date):
    """Return a daily close series indexed by calendar date, forward-filled across non-trading days."""
    code = str(code).zfill(6)
    try:
        bars = dp.get_ohlcv(code, 1200)
    except Exception:
        bars = pd.DataFrame()

    if bars is None or bars.empty or "date" not in bars.columns or "close" not in bars.columns:
        idx = pd.date_range(start_date, end_date, freq="D")
        return pd.Series(index=idx, dtype="float64")

    x = bars.copy()
    x["date"] = pd.to_datetime(x["date"], errors="coerce").dt.normalize()
    x["close"] = pd.to_numeric(x["close"], errors="coerce")
    x = x.dropna(subset=["date", "close"]).sort_values("date")
    if x.empty:
        idx = pd.date_range(start_date, end_date, freq="D")
        return pd.Series(index=idx, dtype="float64")

    ser = x.drop_duplicates("date", keep="last").set_index("date")["close"]
    idx = pd.date_range(start_date, end_date, freq="D")
    ser = ser.reindex(idx).ffill()

    # For today, use the latest snapshot price if available. This keeps the last point current
    # while the rest of the curve remains daily-granularity.
    today = pd.Timestamp.today().normalize()
    if today in ser.index:
        try:
            latest = float(dp.latest_price(code))
            if latest > 0:
                ser.loc[today] = latest
        except Exception:
            pass
    return ser


def build_account_daily_pnl_curve(conn, initial_cash: float = 1000000.0):
    """Rebuild account-level daily PnL from the trading log.

    The old v6.4 equity table only started recording after that version was installed,
    so it cannot show earlier history by itself. This function reconstructs history
    from filled paper-trading orders:
      cash changes come from BUY/SELL fills;
      holdings are marked to daily closes;
      missing calendar days are forward-filled so the curve connects smoothly.
    """
    orders = df_query(
        conn,
        """
        SELECT code, name, side, price, shares, status, created_at
        FROM orders
        WHERE status='FILLED'
        ORDER BY datetime(created_at) ASC, id ASC
        """,
    )

    initial_cash = float(initial_cash)
    today = pd.Timestamp.today().normalize()

    if orders.empty:
        # No trading log to reconstruct from. Fall back to a single current account point.
        cash = get_cash(conn)
        total, stock_val = account_total_value()
        return pd.DataFrame([
            {
                "date": today,
                "total_value": float(total),
                "cash": float(cash),
                "stock_value": float(stock_val),
                "pnl": float(total) - initial_cash,
                "pnl_pct": (float(total) - initial_cash) / initial_cash if initial_cash else 0.0,
            }
        ])

    orders = orders.copy()
    orders["created_at"] = pd.to_datetime(orders["created_at"], errors="coerce")
    orders = orders.dropna(subset=["created_at"])
    if orders.empty:
        return pd.DataFrame()

    orders["trade_date"] = orders["created_at"].dt.normalize()
    orders["code"] = orders["code"].astype(str).str.zfill(6)
    orders["side"] = orders["side"].astype(str).str.upper()
    orders["price"] = pd.to_numeric(orders["price"], errors="coerce").fillna(0.0)
    orders["shares"] = pd.to_numeric(orders["shares"], errors="coerce").fillna(0).astype(int)

    start_date = min(orders["trade_date"].min(), today)
    dates = pd.date_range(start_date, today, freq="D")

    codes = sorted(orders["code"].unique().tolist())
    price_cache = {code: _price_series_for_code(code, start_date, today) for code in codes}

    cash = initial_cash
    holdings = {code: 0 for code in codes}
    last_trade_price = {code: None for code in codes}
    rows = []

    for d in dates:
        todays_orders = orders[orders["trade_date"] == d]
        for o in todays_orders.itertuples():
            code = str(o.code).zfill(6)
            side = str(o.side).upper()
            price = float(o.price)
            shares = int(o.shares)
            value = price * shares
            last_trade_price[code] = price
            if side == "BUY":
                cash -= value
                holdings[code] = holdings.get(code, 0) + shares
            elif side == "SELL":
                cash += value
                holdings[code] = holdings.get(code, 0) - shares
                if holdings[code] < 0:
                    holdings[code] = 0

        stock_value = 0.0
        for code, shares in holdings.items():
            if shares <= 0:
                continue
            ser = price_cache.get(code)
            px = None
            if ser is not None and d in ser.index and pd.notna(ser.loc[d]):
                px = float(ser.loc[d])
            elif last_trade_price.get(code) is not None:
                px = float(last_trade_price[code])
            if px is not None and px > 0:
                stock_value += shares * px

        total_value = cash + stock_value
        rows.append(
            {
                "date": d,
                "total_value": total_value,
                "cash": cash,
                "stock_value": stock_value,
                "pnl": total_value - initial_cash,
                "pnl_pct": (total_value - initial_cash) / initial_cash if initial_cash else 0.0,
            }
        )

    curve = pd.DataFrame(rows)

    # Keep the visualization's latest daily point consistent with the sidebar.
    # The reconstructed curve uses historical orders and daily close marks; for today,
    # the sidebar is the source of truth because it uses current cash + current holdings
    # marked by the latest TDX snapshot. If today already exists, overwrite it; otherwise append it.
    try:
        current_cash = float(get_cash(conn))
        current_total, current_stock_value = account_total_value()
        today_row = {
            "date": today,
            "total_value": float(current_total),
            "cash": current_cash,
            "stock_value": float(current_stock_value),
            "pnl": float(current_total) - initial_cash,
            "pnl_pct": (float(current_total) - initial_cash) / initial_cash if initial_cash else 0.0,
        }
        if curve.empty:
            curve = pd.DataFrame([today_row])
        else:
            curve["date"] = pd.to_datetime(curve["date"], errors="coerce").dt.normalize()
            today_mask = curve["date"] == today
            if today_mask.any():
                for k, v in today_row.items():
                    curve.loc[today_mask, k] = v
            else:
                curve = pd.concat([curve, pd.DataFrame([today_row])], ignore_index=True)
            curve = curve.sort_values("date").reset_index(drop=True)
    except Exception:
        pass

    return curve


def plot_account_pnl_curve(equity_df: pd.DataFrame):
    """Daily account-level PnL curve since the latest paper-account reset."""
    up_color = st.session_state.chart_colors.get("up", "#e74c3c")
    down_color = st.session_state.chart_colors.get("down", "#2ecc71")
    df = equity_df.copy()
    if df.empty:
        return go.Figure()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    df["date_label"] = df["date"].dt.strftime("%Y-%m-%d")
    df["line_color"] = df["pnl"].apply(lambda x: up_color if float(x) >= 0 else down_color)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["pnl"],
            mode="lines+markers",
            name="Account PnL",
            line=dict(color=up_color if float(df["pnl"].iloc[-1]) >= 0 else down_color, width=2),
            marker=dict(size=5, color=df["line_color"]),
            customdata=df[["date_label", "total_value", "cash", "stock_value", "pnl_pct"]].values,
            hovertemplate=(
                "日期: %{customdata[0]}<br>"
                "账户PnL: %{y:,.2f}<br>"
                "账户总价值: %{customdata[1]:,.2f}<br>"
                "现金: %{customdata[2]:,.2f}<br>"
                "股票市值: %{customdata[3]:,.2f}<br>"
                "收益率: %{customdata[4]:.2%}<extra></extra>"
            ),
        )
    )
    fig.add_hline(y=0, line_dash="dot", line_width=1)
    fig.update_layout(
        height=280,
        margin=dict(l=20, r=20, t=20, b=30),
        hovermode="x unified",
        showlegend=False,
    )
    fig.update_xaxes(title_text="日期", nticks=8)
    fig.update_yaxes(title_text="账户PnL")
    return fig


def inject_hotkeys(labels: dict, keys: dict, newest: bool = False):
    payload = {"labels": labels, "keys": keys, "newest": bool(newest)}
    components.html(
        f"""
        <script>
        const cfg = {json.dumps(payload, ensure_ascii=False)};
        function isTypingTarget(el) {{
            if (!el) return false;
            const tag = (el.tagName || '').toLowerCase();
            return tag === 'input' || tag === 'textarea' || el.isContentEditable;
        }}
        function showToast(msg) {{
            const doc = window.parent.document;
            let t = doc.getElementById('kline-key-toast');
            if (!t) {{
                t = doc.createElement('div');
                t.id = 'kline-key-toast';
                t.style.position = 'fixed';
                t.style.top = '72px';
                t.style.left = '50%';
                t.style.transform = 'translateX(-50%)';
                t.style.background = 'rgba(30,30,30,0.86)';
                t.style.color = 'white';
                t.style.padding = '10px 18px';
                t.style.borderRadius = '10px';
                t.style.zIndex = '999999';
                t.style.fontSize = '15px';
                t.style.transition = 'opacity 0.35s ease';
                doc.body.appendChild(t);
            }}
            t.innerText = msg;
            t.style.opacity = '1';
            clearTimeout(window.__klineToastTimer);
            window.__klineToastTimer = setTimeout(() => {{ t.style.opacity = '0'; }}, 1350);
        }}
        function clickButtonByText(text) {{
            const doc = window.parent.document;
            const buttons = Array.from(doc.querySelectorAll('button'));
            const target = buttons.find(b => (b.innerText || '').includes(text) && !b.disabled);
            if (target) {{ target.click(); return true; }}
            return false;
        }}
        window.parent.document.removeEventListener('keydown', window.__klineHotkeyHandler || (() => {{}}));
        window.__klineHotkeyHandler = function(e) {{
            if (isTypingTarget(e.target)) return;
            const key = (e.key || '').toUpperCase();
            const mapping = cfg.keys || {{}};
            const labels = cfg.labels || {{}};
            if (key === mapping.shiftLeft) {{
                e.preventDefault(); clickButtonByText(labels.shiftLeft);
            }} else if (key === mapping.shiftRight) {{
                e.preventDefault();
                const ok = clickButtonByText(labels.shiftRight);
                if (!ok || cfg.newest) showToast('This is already the newest line');
            }} else if (key === mapping.previous) {{
                e.preventDefault(); clickButtonByText(labels.previous);
            }} else if (key === mapping.next) {{
                e.preventDefault(); clickButtonByText(labels.next);
            }} else if (key === mapping.leftAction) {{
                e.preventDefault(); clickButtonByText(labels.leftAction);
            }} else if (key === mapping.rightAction) {{
                e.preventDefault(); clickButtonByText(labels.rightAction);
            }}
        }};
        window.parent.document.addEventListener('keydown', window.__klineHotkeyHandler);
        </script>
        """,
        height=0,
    )


def ma_toggle_button(key: str):
    label = "MA: ON" if st.session_state.show_ma else "MA: OFF"
    if st.button(label, key=key, help="显示/隐藏价格MA5/MA10/MA20与成交量MA5/MA10"):
        st.session_state.show_ma = not st.session_state.show_ma
        st.rerun()


def hotkey_payload():
    h = st.session_state.hotkeys
    return {
        "shiftLeft": h.get("shift_left", "A").upper(),
        "shiftRight": h.get("shift_right", "D").upper(),
        "previous": h.get("previous", "W").upper(),
        "next": h.get("next", "S").upper(),
        "leftAction": h.get("left_action", "J").upper(),
        "rightAction": h.get("right_action", "K").upper(),
    }


st.title("K-lines Gamer：主观K线训练 + 自动委托 MVP")
st.caption("当前版本默认 paper trading；真实下单请接入正式券商 API，并增加风控、权限、审计。")

page = st.sidebar.radio("模块", ["K-lines-gamer", "to_operate_list", "visualization", "trading_log", "settings"])

cash_now = get_cash(conn)
total_value, stock_value = account_total_value()
initial_cash_cfg = float(cfg.get("paper_trading", {}).get("initial_cash", 1000000.0))
log_account_equity_snapshot(
    conn,
    total_value=total_value,
    cash=cash_now,
    stock_value=stock_value,
    initial_cash=initial_cash_cfg,
    note="CURRENT",
)
st.sidebar.metric("模拟现金", f"{cash_now:,.2f}")
st.sidebar.metric("账户当前价值", f"{total_value:,.2f}", delta=f"股票市值 {stock_value:,.2f}")
st.sidebar.caption(f"行情源：{dp.data_source_label()}")
st.sidebar.caption(f"股票池：{dp.universe_source_label()}")
st.sidebar.caption(f"K线复权：{cfg.get('market_data', {}).get('adjust', 'qfq')}")
if dp.last_provider_error:
    st.sidebar.warning(dp.last_provider_error)
if dp.last_universe_error:
    st.sidebar.warning(dp.last_universe_error)

st.sidebar.divider()
if st.sidebar.button("key settings", use_container_width=True):
    hotkey_dialog()
h = st.session_state.hotkeys
st.sidebar.caption(
    f"快捷键：A/D={h['shift_left']}/{h['shift_right']} 平移K线；W/S={h['previous']}/{h['next']} 切换；J/K={h['left_action']}/{h['right_action']} 操作"
)

st.sidebar.divider()
if st.sidebar.button("reset paper account", use_container_width=True):
    reset_paper_dialog()

if page == "K-lines-gamer":
    st.subheader("K-lines-gamer")

    with st.sidebar:
        keyword = st.text_input("搜索代码/名称", "")
        c1, c2 = st.columns(2)
        if c1.button("filter", use_container_width=True):
            filter_dialog()
        if c2.button("reset", use_container_width=True):
            reset_judge_pool()
            st.rerun()

        pool, using_pool = get_current_pool(keyword)
        if using_pool:
            st.caption(f"待判断清单：{len(pool)} 只；当前第 {st.session_state.judge_idx + 1} 只")
        else:
            st.caption("当前使用搜索结果；点击 filter 可生成待判断清单。")

        if pool.empty:
            st.warning("没有符合条件的股票。请调整筛选条件或搜索关键词。")
            st.stop()

        options = pool["code"].astype(str).str.zfill(6) + " " + pool["name"].astype(str)
        default_idx = max(0, min(st.session_state.judge_idx, len(options) - 1))
        selected = st.selectbox("股票", options.tolist(), index=default_idx)

        selected_code = selected.split(" ", 1)[0]
        match_idx = pool.index[pool["code"].astype(str).str.zfill(6) == selected_code]
        if len(match_idx) > 0:
            st.session_state.judge_idx = int(pool.index.get_loc(match_idx[0]))

    code, name = selected.split(" ", 1)
    reset_chart_offset_if_code_changed(code)
    df, max_offset = slice_kline_window(code, 60, 260)

    if df.empty:
        st.error(f"没有找到 {code} {name} 的K线数据。")
        st.stop()

    latest_row = dp.universe().query("code == @code")
    market_cap = None
    if not latest_row.empty and "market_cap" in latest_row.columns and pd.notna(latest_row["market_cap"].iloc[0]):
        market_cap = float(latest_row["market_cap"].iloc[0])

    quote = dp.latest_quote(code)
    latest_price = quote.get("last") if quote.get("last") is not None else df["close"].iloc[-1]
    quote_amount = quote.get("amount") if quote.get("amount") is not None else float(df["amount"].iloc[-1])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("最新价", f"{float(latest_price):.2f}")
    c2.metric("当前成交量", f"{float(quote.get('volume_shares') or df['volume'].iloc[-1]) / 1e4:.2f} 万股")
    c3.metric("60日涨跌", f"{(df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100:.2f}%")
    c4.metric("市值", "-" if market_cap is None else f"{market_cap / 1e8:.2f} 亿")

    q1, q2, q3 = st.columns(3)
    q1.caption(f"盘口 bid1: {quote.get('bid1') if quote.get('bid1') is not None else '-'}")
    q2.caption(f"盘口 ask1: {quote.get('ask1') if quote.get('ask1') is not None else '-'}")
    q3.caption(f"当前成交额: {float(quote_amount) / 1e8:.2f} 亿 | quote source: {quote.get('source', 'unknown')}")

    chart_col, nav_col = st.columns([10, 1])
    with chart_col:
        ma_toggle_button("ma_toggle_gamer")
        st.plotly_chart(
            plot_kline(
                df,
                code,
                show_ma=st.session_state.show_ma,
                current_volume=quote.get("volume_shares"),
                estimated_close_volume=dp.estimate_close_volume(code),
            ),
            use_container_width=False,
        )
    with nav_col:
        st.write("")
        st.write("")
        prev_label = f"previous ({h['previous']})"
        next_label = f"next ({h['next']})"
        left_shift_label = f"← older ({h['shift_left']})"
        right_shift_label = f"newer → ({h['shift_right']})"
        if st.button(prev_label, use_container_width=True, disabled=(st.session_state.judge_idx <= 0)):
            st.session_state.judge_idx -= 1
            st.session_state.kline_offset = 0
            st.rerun()
        if st.button(next_label, use_container_width=True, disabled=(st.session_state.judge_idx >= len(pool) - 1)):
            st.session_state.judge_idx += 1
            st.session_state.kline_offset = 0
            st.rerun()
        st.caption(f"{st.session_state.judge_idx + 1}/{len(pool)}")
        st.divider()
        if st.button(left_shift_label, use_container_width=True, disabled=(st.session_state.kline_offset >= max_offset)):
            st.session_state.kline_offset += 1
            st.rerun()
        if st.button(right_shift_label, use_container_width=True, disabled=(st.session_state.kline_offset <= 0)):
            st.session_state.kline_offset -= 1
            st.rerun()
        st.caption(f"offset {st.session_state.kline_offset}/{max_offset}")

    reason = st.text_input("操作备注/形态理由", "")
    is_holding, pos = is_holding_stock(code)

    labels_for_keys = {
        "previous": prev_label,
        "next": next_label,
        "shiftLeft": left_shift_label,
        "shiftRight": right_shift_label,
    }

    if is_holding:
        st.info(
            f"当前状态：已持仓 {code} {name}，持仓数量 {int(pos.iloc[0]['shares'])} 股，"
            f"成本 {float(pos.iloc[0]['avg_cost']):.2f}"
        )
        left_label = f"卖出 SELL ({h['left_action']})"
        right_label = f"持有 HOLD ({h['right_action']})"
        b1, b2 = st.columns(2)
        if b1.button(left_label, use_container_width=True, type="primary"):
            add_decision(conn, code, name, "SELL", reason)
            st.warning("已加入卖出列表")
        if b2.button(right_label, use_container_width=True):
            add_decision(conn, code, name, "HOLD", reason)
            st.info("已记录持有；不会进入 to_operate_list")
        labels_for_keys.update({"leftAction": left_label, "rightAction": right_label})
    else:
        st.info(f"当前状态：无持仓 {code} {name}")
        left_label = f"买入 BUY ({h['left_action']})"
        right_label = f"观望 WATCH ({h['right_action']})"
        b1, b2 = st.columns(2)
        if b1.button(left_label, use_container_width=True, type="primary"):
            add_decision(conn, code, name, "BUY", reason)
            st.success("已加入买入列表")
        if b2.button(right_label, use_container_width=True):
            add_decision(conn, code, name, "WATCH", reason)
            st.info("已记录观望；不会进入 to_operate_list")
        labels_for_keys.update({"leftAction": left_label, "rightAction": right_label})

    inject_hotkeys(labels_for_keys, hotkey_payload(), newest=(st.session_state.kline_offset <= 0))

elif page == "to_operate_list":
    st.subheader("to_operate_list：待操作清单与仓位计算")
    decisions_all = df_query(
        conn,
        """
        SELECT *
        FROM decisions
        WHERE id IN (
            SELECT MAX(id)
            FROM decisions
            GROUP BY code
        )
        ORDER BY id DESC
        """,
    )
    decisions = decisions_all[decisions_all["action"].isin(["BUY", "SELL"])].copy() if not decisions_all.empty else decisions_all

    if decisions.empty:
        st.info("暂无 BUY / SELL 操作标签。WATCH / HOLD 只用于复盘记录，不进入 to_operate_list。")
    else:
        st.caption("这里只显示 BUY / SELL。WATCH / HOLD 不属于 operation。勾选行后点击删除即可移除对应操作标签。")

        editable_decisions = decisions.copy()
        editable_decisions.insert(0, "delete", False)

        edited_decisions = st.data_editor(
            editable_decisions,
            use_container_width=True,
            hide_index=True,
            disabled=[c for c in editable_decisions.columns if c != "delete"],
            column_config={
                "delete": st.column_config.CheckboxColumn(
                    "删除",
                    help="勾选后点击下方删除按钮，将该股票从 to_operate_list 移除",
                    default=False,
                ),
                "id": st.column_config.NumberColumn("id", disabled=True),
                "code": st.column_config.TextColumn("code", disabled=True),
                "name": st.column_config.TextColumn("name", disabled=True),
                "action": st.column_config.TextColumn("action", disabled=True),
                "reason": st.column_config.TextColumn("reason", disabled=True),
                "created_at": st.column_config.TextColumn("created_at", disabled=True),
            },
            key="to_operate_delete_editor",
        )

        selected_delete_ids = (
            edited_decisions.loc[edited_decisions["delete"], "id"].astype(int).tolist()
            if not edited_decisions.empty
            else []
        )

        c_del1, c_del2 = st.columns([1, 4])
        with c_del1:
            if st.button(
                f"删除勾选项 ({len(selected_delete_ids)})",
                disabled=len(selected_delete_ids) == 0,
                use_container_width=True,
            ):
                for i in selected_delete_ids:
                    delete_decision(conn, int(i))
                st.success(f"已删除 {len(selected_delete_ids)} 条 operation 标签。")
                st.rerun()

        pm = PositionManager(conn, cfg["risk"]["max_cash_per_stock"], cfg["risk"]["min_lot_size"])
        suggestions = pm.suggest(decisions, dp)
        sug_df = pd.DataFrame([s.__dict__ for s in suggestions])
        st.markdown("### position-manager 建议")
        st.dataframe(sug_df, use_container_width=True)
        st.warning("下方按钮会执行当前适配器。默认 paper broker 只做模拟成交。成交成功后，对应 BUY/SELL 标签会自动从 to_operate_list 删除。")
        if st.button("下单 / Push Orders", type="primary"):
            broker = PaperBroker(conn)
            pusher = OrderPusher(conn, broker)
            results = pusher.push(suggestions)
            clear_operation_decisions_for_results(conn, results)
            st.dataframe(pd.DataFrame([r.__dict__ for r in results]), use_container_width=True)
            st.success("已执行下单；已删除成交成功的 operation 标签。")
            st.rerun()

    st.markdown("### 非操作标签 WATCH / HOLD")
    non_operations = (
        decisions_all[decisions_all["action"].isin(["WATCH", "HOLD"])].copy()
        if not decisions_all.empty
        else decisions_all
    )
    if non_operations.empty:
        st.info("暂无 WATCH / HOLD 标签。")
    else:
        non_operations = non_operations.reset_index(drop=True)
        st.caption("点击 WATCH / HOLD 列表中的任意一行，可直接查看该股票 K 线，并可将其转为 BUY。")
        nonop_selection_event = st.dataframe(
            non_operations[["id", "code", "name", "action", "reason", "created_at"]],
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="non_operation_select_table",
        )

        selected_nonop_rows = []
        try:
            selected_nonop_rows = list(nonop_selection_event.selection.rows)
        except Exception:
            try:
                selected_nonop_rows = list(nonop_selection_event.get("selection", {}).get("rows", []))
            except Exception:
                selected_nonop_rows = []

        if selected_nonop_rows:
            selected_idx = int(selected_nonop_rows[0])
            if 0 <= selected_idx < len(non_operations):
                if selected_idx != st.session_state.non_operation_idx:
                    st.session_state.non_operation_idx = selected_idx
                    st.session_state.kline_offset = 0

        st.session_state.non_operation_idx = max(0, min(st.session_state.non_operation_idx, len(non_operations) - 1))
        cur_nonop = non_operations.iloc[st.session_state.non_operation_idx]
        code = str(cur_nonop["code"]).zfill(6)
        name = str(cur_nonop["name"])
        reset_chart_offset_if_code_changed(code)
        df, max_offset = slice_kline_window(code, 60, 260)

        st.markdown(f"### 观察K线：{code} {name}")
        if df.empty:
            st.warning(f"没有找到 {code} {name} 的 K 线数据。")
        else:
            chart_col, side_col = st.columns([10, 2])
            with chart_col:
                ma_toggle_button("ma_toggle_non_operation")
                nonop_quote = dp.latest_quote(code)
                st.plotly_chart(
                    plot_kline(
                        df,
                        code,
                        show_ma=st.session_state.show_ma,
                        current_volume=nonop_quote.get("volume_shares"),
                        estimated_close_volume=dp.estimate_close_volume(code),
                    ),
                    use_container_width=False,
                )
            with side_col:
                st.write(f"当前标签：{cur_nonop['action']}")
                st.write(f"记录时间：{cur_nonop['created_at']}")
                st.caption("备注/形态理由")
                st.write(str(cur_nonop.get("reason", "")) or "-")
                st.divider()

                prev_label = f"previous ({h['previous']})"
                next_label = f"next ({h['next']})"
                left_shift_label = f"← older ({h['shift_left']})"
                right_shift_label = f"newer → ({h['shift_right']})"
                if st.button(prev_label, use_container_width=True, disabled=(st.session_state.non_operation_idx <= 0), key="nonop_prev"):
                    st.session_state.non_operation_idx -= 1
                    st.session_state.kline_offset = 0
                    st.rerun()
                if st.button(next_label, use_container_width=True, disabled=(st.session_state.non_operation_idx >= len(non_operations) - 1), key="nonop_next"):
                    st.session_state.non_operation_idx += 1
                    st.session_state.kline_offset = 0
                    st.rerun()
                st.caption(f"{st.session_state.non_operation_idx + 1}/{len(non_operations)}")
                st.divider()
                if st.button(left_shift_label, use_container_width=True, disabled=(st.session_state.kline_offset >= max_offset), key="nonop_shift_left"):
                    st.session_state.kline_offset += 1
                    st.rerun()
                if st.button(right_shift_label, use_container_width=True, disabled=(st.session_state.kline_offset <= 0), key="nonop_shift_right"):
                    st.session_state.kline_offset -= 1
                    st.rerun()
                st.caption(f"offset {st.session_state.kline_offset}/{max_offset}")
                st.divider()

                buy_reason = st.text_area(
                    "买入备注/形态理由",
                    value=str(cur_nonop.get("reason", "")),
                    key=f"nonop_buy_reason_{int(cur_nonop['id'])}",
                )
                buy_label = f"转为买入 BUY ({h['left_action']})"
                if st.button(buy_label, use_container_width=True, type="primary", key="nonop_buy"):
                    add_decision(conn, code, name, "BUY", buy_reason)
                    st.success("已转为 BUY，并加入待操作清单。")
                    st.rerun()

            inject_hotkeys(
                {
                    "previous": prev_label,
                    "next": next_label,
                    "shiftLeft": left_shift_label,
                    "shiftRight": right_shift_label,
                    "leftAction": buy_label,
                },
                hotkey_payload(),
                newest=(st.session_state.kline_offset <= 0),
            )

elif page == "visualization":
    st.subheader("visualization：账户与持仓")
    st.caption("已移除账户PnL历史曲线，避免每次打开 visualization 时重建历史导致卡顿。左侧 sidebar 仍显示当前现金与账户当前价值。")

    st.markdown("### 当前持仓")
    positions = df_query(conn, "SELECT * FROM positions")
    if positions.empty:
        st.info("暂无持仓。")
    else:
        latest_decisions = df_query(
            conn,
            """
            SELECT *
            FROM decisions
            WHERE id IN (
                SELECT MAX(id)
                FROM decisions
                GROUP BY code
            )
            """,
        )
        reason_map = {}
        action_map = {}
        if not latest_decisions.empty:
            latest_decisions["code"] = latest_decisions["code"].astype(str).str.zfill(6)
            reason_map = latest_decisions.set_index("code")["reason"].fillna("").to_dict()
            action_map = latest_decisions.set_index("code")["action"].fillna("").to_dict()

        rows = []
        for r in positions.itertuples():
            code_str = str(r.code).zfill(6)
            price = dp.latest_price(r.code)
            pnl = (price - r.avg_cost) * r.shares
            rows.append(
                {
                    "code": code_str,
                    "name": r.name,
                    "reason": reason_map.get(code_str, ""),
                    "shares": r.shares,
                    "avg_cost": r.avg_cost,
                    "last_price": price,
                    "market_value": price * r.shares,
                    "pnl": pnl,
                    "pnl_pct": price / r.avg_cost - 1,
                    "opened_at": r.opened_at,
                }
            )
        pos_df = pd.DataFrame(rows).reset_index(drop=True)

        st.caption("点击持仓列表中的任意一行，可直接跳转到该股票的 K 线图。")
        selection_event = st.dataframe(
            style_pnl_table(pos_df),
            use_container_width=True,
            on_select="rerun",
            selection_mode="single-row",
            key="positions_select_table",
        )

        selected_rows = []
        try:
            selected_rows = list(selection_event.selection.rows)
        except Exception:
            try:
                selected_rows = list(selection_event.get("selection", {}).get("rows", []))
            except Exception:
                selected_rows = []

        if selected_rows:
            selected_idx = int(selected_rows[0])
            if 0 <= selected_idx < len(pos_df):
                if selected_idx != st.session_state.position_idx:
                    st.session_state.position_idx = selected_idx
                    st.session_state.kline_offset = 0

        st.session_state.position_idx = max(0, min(st.session_state.position_idx, len(pos_df) - 1))
        cur = pos_df.iloc[st.session_state.position_idx]
        code = str(cur["code"]).zfill(6)
        name = str(cur["name"])
        reset_chart_offset_if_code_changed(code)
        df, max_offset = slice_kline_window(code, 60, 260)

        st.markdown(f"### 持仓K线：{code} {name}")
        chart_col, side_col = st.columns([10, 2])
        with chart_col:
            ma_toggle_button("ma_toggle_positions")
            pos_quote = dp.latest_quote(code)
            st.plotly_chart(
                plot_kline(
                    df,
                    code,
                    show_ma=st.session_state.show_ma,
                    current_volume=pos_quote.get("volume_shares"),
                    estimated_close_volume=dp.estimate_close_volume(code),
                ),
                use_container_width=False,
            )
        with side_col:
            st.markdown(pnl_html(float(cur["pnl"]), float(cur["pnl_pct"]), "持仓盈亏 "), unsafe_allow_html=True)
            st.write(f"持仓数量：{int(cur['shares'])} 股")
            st.write(f"成本：{float(cur['avg_cost']):.2f}")
            st.write(f"现价：{float(cur['last_price']):.2f}")
            st.write(f"市值：{float(cur['market_value']):,.2f}")
            st.caption(f"当前标签：{action_map.get(code, 'HOLD') or 'HOLD'}")
            position_reason = st.text_area(
                "备注/形态理由",
                value=str(cur.get("reason", "")),
                key=f"position_reason_{code}_{action_map.get(code, 'HOLD')}",
            )
            if st.button("保存备注", use_container_width=True, key=f"save_position_reason_{code}"):
                add_decision(conn, code, name, action_map.get(code, "HOLD") or "HOLD", position_reason)
                st.success("已保存备注。")
                st.rerun()
            st.divider()
            prev_label = f"previous ({h['previous']})"
            next_label = f"next ({h['next']})"
            left_shift_label = f"← older ({h['shift_left']})"
            right_shift_label = f"newer → ({h['shift_right']})"
            if st.button(prev_label, use_container_width=True, disabled=(st.session_state.position_idx <= 0), key="pos_prev"):
                st.session_state.position_idx -= 1
                st.session_state.kline_offset = 0
                st.rerun()
            if st.button(next_label, use_container_width=True, disabled=(st.session_state.position_idx >= len(pos_df) - 1), key="pos_next"):
                st.session_state.position_idx += 1
                st.session_state.kline_offset = 0
                st.rerun()
            st.caption(f"{st.session_state.position_idx + 1}/{len(pos_df)}")
            st.divider()
            if st.button(left_shift_label, use_container_width=True, disabled=(st.session_state.kline_offset >= max_offset), key="pos_shift_left"):
                st.session_state.kline_offset += 1
                st.rerun()
            if st.button(right_shift_label, use_container_width=True, disabled=(st.session_state.kline_offset <= 0), key="pos_shift_right"):
                st.session_state.kline_offset -= 1
                st.rerun()
            st.caption(f"offset {st.session_state.kline_offset}/{max_offset}")
            st.divider()
            sell_label = f"卖出 SELL ({h['left_action']})"
            hold_label = f"持有 HOLD ({h['right_action']})"
            if st.button(sell_label, use_container_width=True, type="primary", key="pos_sell"):
                add_decision(conn, code, name, "SELL", "从持仓页发起卖出")
                st.success("已加入卖出清单")
            if st.button(hold_label, use_container_width=True, key="pos_hold"):
                add_decision(conn, code, name, "HOLD", "从持仓页记录持有")
                st.info("已记录持有；不会进入 to_operate_list")

        inject_hotkeys(
            {
                "previous": prev_label,
                "next": next_label,
                "shiftLeft": left_shift_label,
                "shiftRight": right_shift_label,
                "leftAction": sell_label,
                "rightAction": hold_label,
            },
            hotkey_payload(),
            newest=(st.session_state.kline_offset <= 0),
        )

elif page == "trading_log":
    st.subheader("trading_log：历史交易记录")
    orders = df_query(conn, "SELECT * FROM orders ORDER BY id DESC")
    st.dataframe(orders, use_container_width=True)

elif page == "settings":
    st.subheader("settings")
    st.json(cfg)
    c1, c2 = st.columns(2)
    if c1.button("清空操作标签"):
        clear_decisions(conn)
        st.success("已清空")
    if c2.button("重置待判断清单"):
        reset_judge_pool()
        st.success("已重置")

    st.markdown("### 图表颜色")
    st.caption("默认使用 A 股习惯：红色代表上涨/盈利，绿色代表下跌/亏损。")
    color_c1, color_c2, color_c3 = st.columns(3)
    with color_c1:
        up_color = st.color_picker("上涨 / 盈利颜色", st.session_state.chart_colors.get("up", "#e74c3c"))
    with color_c2:
        down_color = st.color_picker("下跌 / 亏损颜色", st.session_state.chart_colors.get("down", "#2ecc71"))
    with color_c3:
        if st.button("恢复红涨绿跌", use_container_width=True):
            st.session_state.chart_colors = {"up": "#e74c3c", "down": "#2ecc71"}
            st.rerun()
    st.session_state.chart_colors = {"up": up_color, "down": down_color}

    st.markdown("### 指标参数")
    ind = st.session_state.indicator_settings.copy()
    i1, i2, i3, i4 = st.columns(4)
    with i1:
        macd_fast = st.number_input("MACD fast", min_value=1, max_value=60, value=int(ind.get("macd_fast", 12)), step=1)
    with i2:
        macd_slow = st.number_input("MACD slow", min_value=2, max_value=120, value=int(ind.get("macd_slow", 26)), step=1)
    with i3:
        macd_signal = st.number_input("MACD signal", min_value=1, max_value=60, value=int(ind.get("macd_signal", 9)), step=1)
    with i4:
        shrink_ratio = st.number_input("缩量阈值倍数", min_value=0.05, max_value=2.0, value=float(ind.get("shrink_volume_ratio", 2.0 / 3.0)), step=0.05, help="筛选缩量时使用：预测成交量 < 成交量MA5和MA10 × 此倍数。默认 2/3。")

    if int(macd_slow) <= int(macd_fast):
        st.warning("MACD slow 应大于 fast；系统会在计算时自动修正为 fast+1。")
    st.session_state.indicator_settings = {
        "macd_fast": int(macd_fast),
        "macd_slow": int(macd_slow),
        "macd_signal": int(macd_signal),
        "shrink_volume_ratio": float(shrink_ratio),
    }

    st.markdown("### 数据源")
    st.write(f"当前行情源：{dp.data_source_label()}")
    st.write(f"当前股票池：{dp.universe_source_label()}")
    st.write(f"K线复权：{cfg.get('market_data', {}).get('adjust', 'qfq')}")
    if dp.last_provider_error:
        st.warning(dp.last_provider_error)
    if dp.last_universe_error:
        st.warning(dp.last_universe_error)

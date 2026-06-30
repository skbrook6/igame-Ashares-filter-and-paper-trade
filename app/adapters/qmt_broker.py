"""
QMT / xtquant 真实下单适配器骨架。
注意：启用前必须在券商/迅投 MiniQMT 正式开通量化交易权限，并完成登录、风控、回报校验。
不同券商 QMT 环境、账号类型、交易市场、价格类型参数可能不同，因此这里保留为显式 TODO。
"""

class QMTBroker:
    def __init__(self, account_id: str, qmt_path: str = ""):
        try:
            from xtquant import xttrader
            from xtquant.xttype import StockAccount
        except Exception as e:
            raise RuntimeError("未安装或未配置 xtquant/QMT 环境，不能启用真实下单。") from e
        self.xttrader = xttrader
        self.StockAccount = StockAccount
        self.account_id = account_id
        self.qmt_path = qmt_path
        raise NotImplementedError("请根据你的券商 QMT 参数补全 connect / subscribe / order_stock / callback。")

    def place_order(self, code: str, name: str, side: str, price: float, shares: int):
        raise NotImplementedError("真实下单前必须补全 QMT 适配器和风控。")

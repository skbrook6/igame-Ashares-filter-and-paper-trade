from app.models import PositionSuggestion
from app.storage import get_cash, df_query

class PositionManager:
    def __init__(self, conn, max_cash_per_stock=20000, lot_size=100):
        self.conn = conn
        self.max_cash_per_stock = float(max_cash_per_stock)
        self.lot_size = int(lot_size)

    def suggest(self, decisions_df, data_provider):
        cash = get_cash(self.conn)
        suggestions = []
        positions = df_query(self.conn, "SELECT * FROM positions")
        pos_map = {r.code: int(r.shares) for r in positions.itertuples()} if not positions.empty else {}
        buy_count = int((decisions_df["action"] == "BUY").sum()) if not decisions_df.empty else 0
        cash_each = min(self.max_cash_per_stock, cash / max(buy_count, 1))
        for r in decisions_df.itertuples():
            if r.action not in ["BUY", "SELL"]:
                continue
            price = data_provider.estimate_today_close(r.code)
            if r.action == "BUY":
                shares = int(cash_each // (price * self.lot_size)) * self.lot_size
                reason = f"按每股估计收盘价 {price:.2f}，单票上限 {cash_each:.0f} 元计算"
            else:
                shares = (pos_map.get(r.code, 0) // self.lot_size) * self.lot_size
                reason = "卖出当前可用整手持仓"
            if shares > 0:
                suggestions.append(PositionSuggestion(r.code, r.name, r.action, price, shares, price * shares, reason))
        return suggestions

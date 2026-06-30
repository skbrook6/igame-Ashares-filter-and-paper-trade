import uuid
from app.models import OrderResult
from app.storage import get_cash, set_cash, upsert_position

class PaperBroker:
    def __init__(self, conn):
        self.conn = conn

    def place_order(self, code: str, name: str, side: str, price: float, shares: int):
        cash = get_cash(self.conn)
        value = price * shares
        if side == "BUY" and value > cash:
            return OrderResult(code, name, side, price, shares, "REJECTED", "现金不足")
        if side == "BUY":
            set_cash(self.conn, cash - value)
        else:
            set_cash(self.conn, cash + value)
        upsert_position(self.conn, code, name, side, price, shares)
        return OrderResult(code, name, side, price, shares, "FILLED", "纸面成交", str(uuid.uuid4())[:8])

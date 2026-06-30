from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

Action = Literal["BUY", "SELL", "HOLD", "WATCH"]
OrderSide = Literal["BUY", "SELL"]

@dataclass
class Decision:
    code: str
    name: str
    action: Action
    reason: str = ""
    created_at: str = datetime.now().isoformat(timespec="seconds")

@dataclass
class PositionSuggestion:
    code: str
    name: str
    side: OrderSide
    price: float
    shares: int
    cash_value: float
    reason: str

@dataclass
class OrderResult:
    code: str
    name: str
    side: OrderSide
    price: float
    shares: int
    status: str
    message: str
    broker_order_id: Optional[str] = None

from abc import ABC, abstractmethod

class BrokerAdapter(ABC):
    @abstractmethod
    def place_order(self, code: str, name: str, side: str, price: float, shares: int):
        raise NotImplementedError

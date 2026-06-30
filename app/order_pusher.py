from app.storage import log_order

class OrderPusher:
    def __init__(self, conn, broker):
        self.conn = conn
        self.broker = broker

    def push(self, suggestions):
        results = []
        for s in suggestions:
            result = self.broker.place_order(s.code, s.name, s.side, s.price, s.shares)
            log_order(self.conn, result)
            results.append(result)
        return results

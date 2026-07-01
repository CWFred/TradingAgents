"""Contract: Broker.close_position is abstract; concrete subclasses must implement."""
from decimal import Decimal
from ops.broker.base import Broker


def test_broker_close_position_is_abstract():
    class Incomplete(Broker):
        def get_cash(self): return Decimal("0")
        def get_equity(self): return Decimal("0")
        def get_positions(self): return []
        def get_quote(self, symbol): return Decimal("1")
        def place_order(self, order): raise NotImplementedError
    import pytest
    with pytest.raises(TypeError, match="close_position"):
        Incomplete()  # type: ignore[abstract]

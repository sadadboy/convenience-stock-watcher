from dataclasses import dataclass
from datetime import datetime, timezone

from app.models.product import ProductSource


@dataclass(frozen=True)
class MockInventoryCheck:
    checked_at: datetime
    in_stock: bool
    quantity: int
    message: str


def check_mock_inventory(source: ProductSource) -> MockInventoryCheck:
    seed = sum(ord(character) for character in source.external_product_code)
    quantity = seed % 7
    in_stock = quantity > 0
    message = "재고 있음" if in_stock else "재고 없음"

    return MockInventoryCheck(
        checked_at=datetime.now(timezone.utc),
        in_stock=in_stock,
        quantity=quantity,
        message=message,
    )

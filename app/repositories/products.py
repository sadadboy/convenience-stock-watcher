from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session

from app.models.product import Product, ProductSource
from app.schemas.product import ProductCreate, ProductSourceCreate


def list_products(db: Session) -> list[Product]:
    return list(
        db.scalars(
            select(Product)
            .options(selectinload(Product.sources))
            .order_by(Product.created_at.desc())
        )
    )


def get_product(db: Session, product_id: int) -> Product | None:
    return db.scalar(
        select(Product)
        .options(selectinload(Product.sources))
        .where(Product.id == product_id)
    )


def create_product(db: Session, product_in: ProductCreate) -> Product:
    product = Product(
        display_name=product_in.display_name.strip(),
        search_keyword=product_in.search_keyword.strip(),
        memo=product_in.memo.strip() if product_in.memo else None,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def set_product_enabled(db: Session, product_id: int, enabled: bool) -> Product | None:
    product = db.get(Product, product_id)
    if product is None:
        return None

    product.enabled = enabled
    db.commit()
    db.refresh(product)
    return product


def delete_product(db: Session, product_id: int) -> bool:
    product = db.get(Product, product_id)
    if product is None:
        return False
    # Product.sources cascades to product_sources; stock_watches FK is
    # ON DELETE CASCADE, so dependent watches are removed at the DB level.
    db.delete(product)
    db.commit()
    return True


def create_product_source(
    db: Session,
    product_id: int,
    source_in: ProductSourceCreate,
) -> ProductSource | None:
    product = db.get(Product, product_id)
    if product is None:
        return None

    source = ProductSource(
        product_id=product_id,
        brand=source_in.brand.strip(),
        external_product_code=source_in.external_product_code.strip(),
        external_product_name=source_in.external_product_name.strip(),
        barcode=source_in.barcode.strip() if source_in.barcode else None,
        image_url=source_in.image_url.strip() if source_in.image_url else None,
        price=source_in.price,
        confidence=100,
        confirmed_by_user=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def delete_product_source(db: Session, source_id: int) -> bool:
    source = db.get(ProductSource, source_id)
    if source is None:
        return False
    db.delete(source)
    db.commit()
    return True


def set_product_source_enabled(
    db: Session,
    source_id: int,
    enabled: bool,
) -> ProductSource | None:
    source = db.get(ProductSource, source_id)
    if source is None:
        return None

    source.enabled = enabled
    db.commit()
    db.refresh(source)
    return source


EXPORT_FORMAT = "convenience-stock-watcher/products"
EXPORT_VERSION = 1


def export_products(db: Session, product_ids: list[int] | None = None) -> dict:
    """Serialize products and their brand mappings for a later re-import."""
    products = list_products(db)
    if product_ids:
        wanted = set(product_ids)
        products = [product for product in products if product.id in wanted]
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "products": [
            {
                "display_name": product.display_name,
                "search_keyword": product.search_keyword,
                "memo": product.memo,
                "enabled": product.enabled,
                "sources": [
                    {
                        "brand": source.brand,
                        "external_product_code": source.external_product_code,
                        "external_product_name": source.external_product_name,
                        "barcode": source.barcode,
                        "image_url": source.image_url,
                        "price": source.price,
                        "enabled": source.enabled,
                    }
                    for source in sorted(product.sources, key=lambda s: (s.brand, s.external_product_name))
                ],
            }
            for product in reversed(products)  # oldest first, so re-import keeps order
        ],
    }


def import_products(db: Session, payload: dict) -> dict[str, int]:
    """Re-register products from ``export_products`` output. Idempotent.

    A product whose display name already exists is merged into (only mappings
    it lacks, by brand + product code, are added); otherwise it is created.
    Raises ValueError when the payload is not an export file.
    """
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise ValueError("상품 내보내기 파일이 아닙니다.")
    items = payload.get("products")
    if not isinstance(items, list):
        raise ValueError("상품 목록이 비어 있거나 형식이 잘못되었습니다.")

    existing = {product.display_name: product for product in list_products(db)}
    stats = {"created": 0, "merged": 0, "sources": 0, "skipped": 0}

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or "").strip()
        if not name:
            continue
        product = existing.get(name)
        if product is None:
            product = Product(
                display_name=name,
                search_keyword=str(item.get("search_keyword") or name).strip(),
                memo=(str(item.get("memo")).strip() or None) if item.get("memo") else None,
                enabled=bool(item.get("enabled", True)),
            )
            db.add(product)
            db.flush()
            existing[name] = product
            have: set[tuple[str, str]] = set()
            stats["created"] += 1
        else:
            have = {(s.brand, s.external_product_code) for s in product.sources}
            stats["merged"] += 1

        for raw in item.get("sources") or []:
            if not isinstance(raw, dict):
                continue
            brand = str(raw.get("brand") or "").strip()
            code = str(raw.get("external_product_code") or "").strip()
            if not brand or not code:
                continue
            if (brand, code) in have:
                stats["skipped"] += 1
                continue
            price = raw.get("price")
            db.add(
                ProductSource(
                    product_id=product.id,
                    brand=brand,
                    external_product_code=code,
                    external_product_name=str(raw.get("external_product_name") or code).strip(),
                    barcode=raw.get("barcode") or None,
                    image_url=raw.get("image_url") or None,
                    price=price if isinstance(price, int) else None,
                    enabled=bool(raw.get("enabled", True)),
                    confidence=100,
                    confirmed_by_user=True,
                )
            )
            have.add((brand, code))
            stats["sources"] += 1

    db.commit()
    return stats


def bulk_update_sources(db: Session, product_id: int, source_ids: list[int], action: str) -> int:
    """Delete / enable / disable several of one product's sources at once."""
    sources = list(
        db.scalars(
            select(ProductSource).where(
                ProductSource.product_id == product_id,
                ProductSource.id.in_(source_ids),
            )
        )
    )
    for source in sources:
        if action == "delete":
            db.delete(source)
        else:
            source.enabled = action == "enable"
    db.commit()
    return len(sources)


def delete_products(db: Session, product_ids: list[int]) -> int:
    """Delete several products (their sources and watches cascade)."""
    products = list(db.scalars(select(Product).where(Product.id.in_(product_ids))))
    for product in products:
        db.delete(product)
    db.commit()
    return len(products)

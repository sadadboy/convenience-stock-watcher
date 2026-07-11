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

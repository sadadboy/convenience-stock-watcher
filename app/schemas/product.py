from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    search_keyword: str = Field(min_length=1, max_length=200)
    memo: str | None = None


class ProductRead(BaseModel):
    id: int
    display_name: str
    search_keyword: str
    memo: str | None
    enabled: bool

    model_config = ConfigDict(from_attributes=True)


class ProductSourceCreate(BaseModel):
    brand: str = Field(min_length=1, max_length=40)
    external_product_code: str = Field(min_length=1, max_length=120)
    external_product_name: str = Field(min_length=1, max_length=300)
    barcode: str | None = Field(default=None, max_length=120)
    image_url: str | None = Field(default=None, max_length=1000)
    price: int | None = None

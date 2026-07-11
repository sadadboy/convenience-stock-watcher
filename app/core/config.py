from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Convenience Stock Watcher"
    database_url: str = "postgresql+psycopg://stock:stock@db:5432/stock"

    # GS25 product search (unofficial woodongs total-search endpoint).
    # Disable to skip live network calls (search then returns no candidates).
    gs25_search_enabled: bool = True
    gs25_api_base_url: str = "https://b2c-apigw.woodongs.com"
    gs25_search_path: str = "/search/v3/totalSearch"
    gs25_request_timeout: float = 10.0
    gs25_search_limit: int = 20

    # GS25 real-time store stock lookup (itemCode + coordinates).
    gs25_stock_enabled: bool = True
    gs25_bff_base_url: str = "https://b2c-bff.woodongs.com"
    gs25_stock_path: str = "/api/bff/v2/store/stock"
    # Default search center used by the "실제 재고 조회" button (Seoul City Hall).
    gs25_default_latitude: float = 37.5665
    gs25_default_longitude: float = 126.9780
    gs25_default_radius_meters: int = 1000
    # The stock BFF requires a woodongs app bearer token (login not built here).
    # Leave blank to keep stock lookups as a graceful no-op / failure.
    gs25_auth_token: str = ""

    # Shared HTTP timeout for the 7-Eleven / Emart24 adapters (seconds).
    adapter_request_timeout: float = 10.0

    # 7-Eleven product search (public /open/ search endpoint, no auth).
    seveneleven_search_enabled: bool = True
    seveneleven_base_url: str = "https://new.7-elevenapp.co.kr"
    seveneleven_search_path: str = "/api/v1/open/search/goods"
    seveneleven_search_limit: int = 20

    # 7-Eleven real-time stock (itemCd -> smCd meta -> store search -> real-stock).
    seveneleven_stock_enabled: bool = True
    seveneleven_product_meta_path: str = "/api/v1/open/product/search/stock"
    seveneleven_store_search_path: str = "/api/v1/open/search/store"
    seveneleven_real_stock_path: str = "/api/v1/open/real-stock/multi/01/stocks"
    seveneleven_store_limit: int = 30
    # Default store-search keyword for the "실시간 재고 조회" button.
    seveneleven_default_store_keyword: str = "강남"

    # Emart24 product search (public everse endpoint, no auth).
    emart24_search_enabled: bool = True
    emart24_base_url: str = "https://everse.emart24.co.kr"
    emart24_search_path: str = "/stock/stock/search"
    emart24_search_limit: int = 20

    # Emart24 real-time stock (pluCd + store keyword -> bizNo list -> qty).
    emart24_stock_enabled: bool = True
    emart24_web_base_url: str = "https://emart24.co.kr"
    emart24_store_search_path: str = "/api1/store"
    emart24_stock_search_path: str = "/api/stock/v2/stock-search/store"
    emart24_store_limit: int = 30
    emart24_default_store_keyword: str = "강남"

    # Restock watcher scheduler.
    watcher_enabled: bool = True
    watcher_poll_seconds: int = 60
    watcher_default_interval_seconds: int = 300
    # Max watches checked per scheduler tick (bounds API bursts at scale).
    watcher_max_checks_per_tick: int = 25

    # Discord push alerts. Fallback if no webhook is saved in the UI.
    discord_webhook_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

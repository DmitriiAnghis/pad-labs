from pad_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "gateway"
    auth_url: str = "http://auth-service:8000"
    catalog_url: str = "http://catalog-service:8000"
    inventory_url: str = "http://inventory-service:8000"
    order_url: str = "http://order-service:8000"
    fulfillment_url: str = "http://fulfillment-service:8000"
    notification_url: str = "http://notification-service:8000"
    rag_url: str = "http://rag-assistant:8000"
    # Сколько ждать ответа сервиса. Ассистенту нужно больше: LLM отвечает секундами.
    upstream_timeout: float = 10.0
    rag_timeout: float = 120.0
    # Ограничение частоты: запросов в минуту на одного пользователя (или IP без токена).
    rate_limit_per_minute: int = 120

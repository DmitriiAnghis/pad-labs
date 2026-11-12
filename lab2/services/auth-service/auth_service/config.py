from pad_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "auth-service"
    # Закрытый ключ RSA (PEM) для подписи JWT. Секрет: только в .env / Kubernetes Secret.
    # Пусто = ключ генерируется при старте (только для разработки: после перезапуска все токены
    # становятся недействительными, а две копии сервиса получат разные ключи).
    jwt_private_key: str = ""
    access_token_ttl_s: int = 900          # 15 минут
    refresh_token_ttl_s: int = 7 * 24 * 3600
    # Первый администратор создаётся при старте, если в базе ещё нет пользователей.
    admin_username: str = "admin"
    admin_password: str = ""

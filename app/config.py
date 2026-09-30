import os


class Settings:
    def __init__(self) -> None:
        self.database_path = os.getenv("DATABASE_PATH", "/data/museum.db")


settings = Settings()

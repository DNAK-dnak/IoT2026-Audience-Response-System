from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str    
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_tls: bool = False
    mqtt_client_id: str = "ars-backend"
    room: str = "room1"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")   

settings = Settings()

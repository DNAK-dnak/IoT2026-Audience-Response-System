"""Settings read from the .env file (one place for every host, port and password).

pydantic-settings matches each field to a line in .env by name (case does not matter)
and converts it to the declared type. Start the server from the `backend` folder so
that ".env" is found.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = ""          # empty = no username (local Mosquitto with anonymous access)
    mqtt_pass: str = ""
    mqtt_tls: bool = False       # True for HiveMQ Cloud (port 8883)
    mqtt_client_id: str = "ars-backend"
    room: str = "room1"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

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
    # AI analysis of results (app/analyze.py). No key at all = the feature is off.
    # With both keys set, ai_provider picks one ("gemini" or "anthropic"); empty = Gemini first.
    ai_provider: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"          # quota used up? try gemini-3.5-flash-lite
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    anthropic_api_key: str = ""
    analyze_model: str = "claude-sonnet-5"
    anthropic_base_url: str = "https://api.anthropic.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RZP_SIM_", env_file=".env", extra="ignore")

    key_id: str = "rzp_test_simulator"
    key_secret: str = "simulator_secret"
    account_id: str = "acc_simulator00001"

    # Where payment/order/refund events are POSTed. Empty = events are only logged.
    webhook_url: str = ""
    webhook_secret: str = "simulator_webhook_secret"
    webhook_retries: int = 3
    webhook_timeout_seconds: float = 5.0

    # Razorpay auto-capture setting: when true a successful payment goes straight to "captured".
    auto_capture: bool = True
    fee_percent: float = 2.0


settings = Settings()

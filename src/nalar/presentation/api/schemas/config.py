from pydantic import BaseModel


class ConfigOut(BaseModel):
    password_reset_enabled: bool
    account_email_enabled: bool
    weekly_digest_enabled: bool

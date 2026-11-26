import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Account A makes pictures with Bing Image Creator (bing.com/images/create), logged in with Bing's "_U" cookie.
# It wins over BING_U from .env or Render. Replace it here when Microsoft logs the account out.
BING_HARDCODED_U = ("11_EQ-Lj4qPk1G11Rm_EFWi-RVzVqq0dgYM57K_gxUXD1haAdvlffgXsHFEJ2VAqPcVf-V2mtPHpzadJSzaVkxsrRZ5ggbX_6VF6Wg0qub"
                    "OplCAgLW7IJ0NREkoOEV1QzJSFbzwK0FIdNR_2vyljhr9xoiSRRctj2JbHO-DcdkQOZq-o5hjGMPcAryT3NbtNBHt1tSvv3U0QFnnDW8KGErYP4"
                    "UPTDsnW22GnHDRR-JPk")
# Always told when a picture cookie (Bing or Gemini) stops working, besides ALERT_EMAIL_TO / REVIEW_EMAIL_TO.
COOKIE_ALERT_EMAIL = "abdullahfida645@gmail.com"

# Gemini cookies written straight into the code: {account: (__Secure-1PSID, __Secure-1PSIDTS)}.
# They win over GEMINI_*_1PSID / _1PSIDTS from .env or Render. Replace them here when Google logs an account out.
# Account A is Bing (above) whenever a Bing cookie is set.
GEMINI_HARDCODED = {
    "B": ("g.a000EQnWVyqXjIiIcNg7ozGmFRl-Z2L9szWC-3uFXMKGisokG50aiRfjlynQjKth_TqnMy-t9gACgYKAQkSARESFQHGX2MiAg6vzUZSZ4l21OUW2-nDzBoVAUF8yKoQA8a1NYhAu-eVMxgANbep0076",
          "sidts-CjIBnJZzav2gY8MKD94hxx6yuG7W_3OMV-UHLb4A39pyMsJa2dxiDKr5yxwvUP9AWDu8VxAA"),  # backup when A stops working
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_secret: str = "change-me"
    dashboard_password: str = "admin"
    public_base_url: str = "http://localhost:8000"
    brand_id: str = "amanasoft"
    data_dir: Path = ROOT / "data"
    # Keep-awake: the app visits its own /health this often (minutes) so Render's free plan (which sleeps after
    # 15 quiet minutes) never sleeps it. Only runs on a public https address (on Render, not on your computer). 0 = off.
    heartbeat_minutes: float = 5

    # Text AI: Groq first, OpenRouter as backup
    groq_api_key: str = ""
    groq_models: str = "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b"
    # models that can look at pictures (for the client's own photos and videos); up to 3 pictures per request
    groq_vision_models: str = "qwen/qwen3.8-27b,meta-llama/llama-4-scout-17b-16e-instruct"
    groq_whisper_model: str = "whisper-large-v3-turbo"  # listens to what is said in an uploaded video
    openrouter_api_key: str = ""
    openrouter_models: str = "openrouter/free,google/gemma-4-31b-it:free"

    # Picture accounts: A, then B, then C. A is Bing Image Creator when a Bing "_U" cookie is set
    # (BING_HARDCODED_U above, or BING_U here), otherwise Gemini. B and C are Gemini.
    # Cookies written in app/config.py (above) win over these environment values.
    bing_u: str = ""
    gemini_a_1psid: str = ""
    gemini_a_1psidts: str = ""
    gemini_b_1psid: str = ""
    gemini_b_1psidts: str = ""
    gemini_c_1psid: str = ""
    gemini_c_1psidts: str = ""
    gemini_model: str = ""  # empty = account default
    gemini_proxy: str = ""  # optional, e.g. http://user:pass@host:port if Google blocks the server's address
    gemini_health_check_hours: float = 6

    # Email
    email_mode: str = "apps_script"  # apps_script | smtp | gmail_api
    email_relay_url: str = ""
    email_relay_secret: str = ""
    gmail_sender: str = ""
    gmail_app_password: str = ""
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    review_email_to: str = ""
    alert_email_to: str = ""

    # Supabase (optional locally; required on Render)
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_bucket: str = "posts"
    # largest video the client can upload. Supabase's free plan stores files up to 50 MB; raise it on a paid plan.
    max_video_mb: int = 50

    # Buffer
    buffer_api_key: str = ""
    buffer_channel_ids: str = ""  # optional; empty = every Facebook, X and Threads channel in Buffer
    publish_mode: str = "shareNow"  # shareNow | addToQueue
    # captions are written for these platforms; a Buffer channel of another kind gets the Facebook text
    # Normally the platforms are read from the channels connected in Buffer; this list is used only if Buffer
    # cannot be reached while a post is being written.
    post_platforms: str = "facebook,instagram,linkedin"
    dry_run_publish: bool = False  # true = approve does everything except posting

    @field_validator("*", mode="before")
    @classmethod
    def _clean(cls, value):
        """Forgive copy-paste slips in hosting dashboards: spaces, quotes and line breaks around a value."""
        if isinstance(value, str):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
        return value

    @property
    def groq_model_list(self) -> list[str]:
        return [m.strip() for m in self.groq_models.split(",") if m.strip()]

    @property
    def groq_vision_model_list(self) -> list[str]:
        return [m.strip() for m in self.groq_vision_models.split(",") if m.strip()]

    @property
    def openrouter_model_list(self) -> list[str]:
        return [m.strip() for m in self.openrouter_models.split(",") if m.strip()]

    @property
    def post_platform_list(self) -> list[str]:
        known = ("facebook", "instagram", "linkedin", "x", "threads", "tiktok", "pinterest", "googlebusiness",
                 "bluesky", "mastodon", "substack")
        chosen = [p.strip().lower() for p in self.post_platforms.split(",") if p.strip().lower() in known]
        return list(dict.fromkeys(["facebook", *chosen]))  # Facebook text is always there (the fallback)

    @property
    def buffer_channel_id_list(self) -> list[str]:
        return [c.strip() for c in self.buffer_channel_ids.split(",") if c.strip()]

    @property
    def review_recipients(self) -> list[str]:
        return [e.strip() for e in self.review_email_to.split(",") if e.strip()]

    @property
    def alert_recipients(self) -> list[str]:
        raw = self.alert_email_to or self.review_email_to
        return [e.strip() for e in raw.split(",") if e.strip()]

    @property
    def use_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)

    @property
    def base_url(self) -> str:
        chosen = self.public_base_url
        render_external = os.environ.get("RENDER_EXTERNAL_URL")
        if chosen in ("", "http://localhost:8000") and render_external and render_external.startswith("https://"):
            chosen = render_external
        return chosen.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()

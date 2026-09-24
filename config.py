from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API keys
    JWT_SECRET: str = "change-me-in-prod"
    GOOGLE_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_TRACING_V2: str = "true"
    PAGEINDEX_API_KEY: str = ""

    # Service URLs
    GPTCACHE_URL: str = "http://gptcache:8001"
    RIVAL_URL: str = "http://rival-service:8002"
    MONGODB_URI: str = "mongodb://mongodb:27017"
    POSTGRES_DSN: str = "postgresql://postgres:postgres@postgres:5432/support_bot"

    # Tuning
    # Bhairava returns is_attack alongside a confidence that, measured on this
    # deployment, sits in a 0.514-0.531 band for BOTH benign questions and real
    # jailbreaks. Trusting its boolean rejected ordinary users ("How do I set up
    # Face ID?" scored 0.521 and was blocked). Until the detector is evaluated
    # properly, require a confidence above this floor before terminating a request.
    ATTACK_CONFIDENCE_THRESHOLD: float = 0.53
    MAX_INPUT_CHARS: int = 4000
    MAX_SESSION_TURNS: int = 10
    FAITHFULNESS_THRESHOLD: float = 0.7
    COMPLETENESS_THRESHOLD: float = 0.6

    # Model selection
    LOW_COMPLEXITY_MODEL: str = "gemini-3.5-flash"
    # Pro models return 404 / free-tier quota 0 on this key, so the high path uses
    # the strongest available flash model. Swap to a pro id once billing is on.
    HIGH_COMPLEXITY_MODEL: str = "gemini-3.8-flash"
    # Cheap model for internal machinery: tree search, query analysis, the
    # completeness judge. Previously hardcoded as "gemini-2.0-flash" in three
    # separate files, which Google retired — every request 404'd.
    UTILITY_MODEL: str = "gemini-3.5-flash"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

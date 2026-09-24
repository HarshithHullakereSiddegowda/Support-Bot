from pydantic_settings import BaseSettings


class EvalSettings(BaseSettings):
    APP_URL: str = "http://localhost:8000"
    JWT_SECRET: str = "change-me-in-prod"
    # Field name has no EVAL_ prefix: env_prefix adds it. Naming this field
    # EVAL_TIMEOUT_MS made the real env var EVAL_EVAL_TIMEOUT_MS, so the value in
    # .env was silently ignored. 30s also cut off every request -- the full graph
    # (2 safety calls, intent, tree search, generation, 2 judges) runs ~50-60s.
    TIMEOUT_MS: int = 180000
    FAITHFULNESS_THRESHOLD: float = 0.7
    COMPLETENESS_THRESHOLD: float = 0.6
    LATENCY_REGRESSION_THRESHOLD: float = 0.2  # 20% regression allowed
    COST_REGRESSION_THRESHOLD: float = 0.3  # 30% cost increase allowed
    BASELINE_FILE: str = "evals/baselines/latest.json"
    DATASET_FILE: str = "evals/datasets/golden.json"

    class Config:
        env_prefix = "EVAL_"
        env_file = ".env"
        # Without this, every non-EVAL_ key in .env is treated as an unexpected
        # input and pydantic raises a ValidationError that PRINTS THE VALUES --
        # i.e. it dumps your API keys to stdout and into CI logs.
        extra = "ignore"


eval_settings = EvalSettings()

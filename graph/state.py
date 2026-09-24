from typing import Annotated, Literal, TypedDict


def append_list(existing: list, new: list) -> list:
    """Reducer that appends to a list — used for parallel sub_responses."""
    return (existing or []) + (new or [])


def take_latest(existing: str, new: str) -> str:
    """Reducer for a scalar written by several concurrent nodes.

    The parallel generate_subquery nodes all report the model they used. Without
    a reducer LangGraph refuses the concurrent write outright:
      INVALID_CONCURRENT_GRAPH_UPDATE: Can receive only one value per step.
    Every fan-out copy routes on the same state["complexity"], so the values are
    identical and keeping the last one is accurate, not a coin flip.
    """
    return new or existing


class SupportBotState(TypedDict):
    # --- input ---
    raw_query: str
    session_id: str
    request_id: str

    # --- safety ---
    scrubbed_query: str
    pii_found: list[str]
    is_attack: bool
    attack_confidence: float

    # --- query intelligence ---
    intent: str
    sub_queries: list[str]
    complexity: Literal["low", "high"]
    needs_decomp: bool
    prompt_version: str
    current_subquery: str  # used per-node in fan-out

    # --- context ---
    session_history: list[dict]
    retrieved_context: list[str]

    # --- execution ---
    # annotated with the append reducer so parallel Send nodes can all write
    sub_responses: Annotated[list[str], append_list]
    raw_response: str
    # Written concurrently by the generate_subquery fan-out — needs a reducer.
    model_used: Annotated[str, take_latest]

    # --- validation ---
    faithfulness_score: float
    completeness_score: float
    validation_passed: bool

    # --- output ---
    final_response: str

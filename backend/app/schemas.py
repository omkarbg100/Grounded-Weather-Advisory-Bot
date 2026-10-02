from typing import List, Optional, Union, Dict, Any, Literal
from pydantic import BaseModel, Field, model_validator

# --- SOP YAML Schemas ---

SeverityLevel = Literal["info", "low", "moderate", "high", "critical"]
ComparisonOp = Literal[">", ">=", "<", "<=", "==", "between", "in"]


class SingleCondition(BaseModel):
    fact: str
    op: ComparisonOp
    value: Union[float, int, str, List[Union[float, int, str]]]
    window: Optional[str] = None


class WhenGroup(BaseModel):
    all: Optional[List[Union[SingleCondition, "WhenGroup"]]] = None
    any: Optional[List[Union[SingleCondition, "WhenGroup"]]] = None

    @model_validator(mode="after")
    def check_at_least_one(self):
        if not self.all and not self.any:
            raise ValueError("WhenGroup must contain either 'all' or 'any'")
        return self


SingleCondition.model_rebuild()
WhenGroup.model_rebuild()


class FuzzyBand(BaseModel):
    min_score: float
    label: str
    advice: str


class FuzzyCriterion(BaseModel):
    fact: str
    weight: float
    op: ComparisonOp
    value: Union[float, int, str, List[Union[float, int, str]]]
    window: Optional[str] = None


class FuzzyScoring(BaseModel):
    criteria: List[FuzzyCriterion]
    bands: List[FuzzyBand]


class SOPModel(BaseModel):
    id: str
    category: str
    title: str
    severity: SeverityLevel
    applies_to: List[str]
    precedence: Optional[Literal["override"]] = None
    when: Optional[WhenGroup] = None
    scoring: Optional[FuzzyScoring] = None
    advice: Optional[str] = None
    tags: Optional[List[str]] = []

    @model_validator(mode="after")
    def validate_when_or_scoring(self):
        if not self.when and not self.scoring:
            raise ValueError(f"SOP '{self.id}' must specify either 'when' or 'scoring'")
        if self.when and self.scoring:
            raise ValueError(f"SOP '{self.id}' cannot specify both 'when' and 'scoring'")
        if self.when and not self.advice:
            raise ValueError(f"SOP '{self.id}' with 'when' condition must specify an 'advice' template")
        return self


class SOPRegistry(BaseModel):
    sops: List[SOPModel]
    taxonomy: List[str]


# --- Location Schema ---

LocationSource = Literal["coordinates", "geocoded", "session"]


class LocationModel(BaseModel):
    lat: float
    lon: float
    label: str
    source: LocationSource


# --- Intent Parsing Schema ---

IntentType = Literal["advice", "followup", "smalltalk", "out_of_scope"]
TimeRef = Literal["now", "today", "this_evening", "tomorrow", "unspecified"]


class IntentParseResult(BaseModel):
    intent_type: IntentType
    activity: str
    city_text: Optional[str] = None
    time_ref: TimeRef = "unspecified"


# --- API Endpoint Schemas ---

# Terminal outcomes. `clarify`, `no_sop`, `out_of_scope`, `location_failed` and
# `weather_failed` come from the model's end_turn(outcome=...) argument;
# `answered` is also what the deterministic fallback produces; `unavailable`
# and `llm_unavailable` are set by the graph when no reply could be formed.
ChatOutcome = Literal[
    "answered",
    "no_sop",
    "clarify",
    "out_of_scope",
    "location_failed",
    "weather_failed",
    "unavailable",
    "llm_unavailable",
]


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    outcome: ChatOutcome
    sop_ids: List[str]
    location: Optional[LocationModel] = None
    facts_used: Dict[str, Any] = Field(default_factory=dict)
    trace: List[str] = Field(default_factory=list)


class SOPSummary(BaseModel):
    id: str
    category: str
    title: str
    severity: SeverityLevel
    applies_to: List[str]
    precedence: Optional[str] = None
    advice_template: str

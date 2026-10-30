from app.agents.base import ReviewAgent
from app.prompts.quality import QUALITY_SYSTEM_PROMPT
from app.schemas.agents import QualityAgentResult, QualityReview
from app.schemas.review import AgentType


class QualityAgent(ReviewAgent):
    agent_type = AgentType.QUALITY
    system_prompt = QUALITY_SYSTEM_PROMPT
    review_schema = QualityReview
    result_schema = QualityAgentResult

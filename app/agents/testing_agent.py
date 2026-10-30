from app.agents.base import ReviewAgent
from app.prompts.testing import TESTING_SYSTEM_PROMPT
from app.schemas.agents import TestingAgentResult, TestingReview
from app.schemas.review import AgentType


class TestingAgent(ReviewAgent):
    agent_type = AgentType.TESTING
    system_prompt = TESTING_SYSTEM_PROMPT
    review_schema = TestingReview
    result_schema = TestingAgentResult

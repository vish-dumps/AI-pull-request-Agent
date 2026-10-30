from app.agents.base import ReviewAgent
from app.agents.documentation_agent import DocumentationAgent
from app.agents.quality_agent import QualityAgent
from app.agents.security_agent import SecurityAgent
from app.agents.testing_agent import TestingAgent
from app.core.config import Settings
from app.schemas.review import AgentType
from app.services.llm_service import LLMProvider


def create_reviewers(provider: LLMProvider, settings: Settings) -> dict[AgentType, ReviewAgent]:
    return {
        reviewer.agent_type: reviewer(provider, settings)
        for reviewer in (
            SecurityAgent,
            QualityAgent,
            TestingAgent,
            DocumentationAgent,
        )
    }

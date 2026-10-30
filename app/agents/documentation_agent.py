from app.agents.base import ReviewAgent
from app.prompts.documentation import DOCUMENTATION_SYSTEM_PROMPT
from app.schemas.agents import DocumentationAgentResult, DocumentationReview
from app.schemas.review import AgentType


class DocumentationAgent(ReviewAgent):
    agent_type = AgentType.DOCUMENTATION
    system_prompt = DOCUMENTATION_SYSTEM_PROMPT
    review_schema = DocumentationReview
    result_schema = DocumentationAgentResult

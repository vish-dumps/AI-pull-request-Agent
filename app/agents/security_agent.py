from app.agents.base import ReviewAgent, prepare_review_input
from app.agents.base import added_line_numbers as added_line_numbers
from app.agents.base import check_finding_evidence as check_finding_evidence
from app.prompts.security import SECURITY_SYSTEM_PROMPT
from app.schemas.agents import SecurityAgentResult, SecurityReview
from app.schemas.review import AgentType

prepare_security_input = prepare_review_input


class SecurityAgent(ReviewAgent):
    agent_type = AgentType.SECURITY
    system_prompt = SECURITY_SYSTEM_PROMPT
    review_schema = SecurityReview
    result_schema = SecurityAgentResult

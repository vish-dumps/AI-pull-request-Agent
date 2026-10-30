from collections.abc import Mapping
from functools import partial
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import ReviewAgent
from app.agents.security_agent import SecurityAgent
from app.core.exceptions import WorkflowConflictError, WorkflowTransientError
from app.graph.nodes import ReviewNodes
from app.graph.state import ReviewState, WorkflowReport
from app.schemas.github import PullRequestContext
from app.schemas.jobs import ReviewJob
from app.schemas.review import AgentType, ReviewStatus
from app.services.context_service import ContextService
from app.services.github_service import PullRequestSource


class ReviewWorkflow:
    def __init__(
        self,
        agent: SecurityAgent | None = None,
        *,
        reviewers: Mapping[AgentType, ReviewAgent] | None = None,
        mock_mode: bool,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        github: PullRequestSource | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        stop_before_persist: bool = False,
        context_service: ContextService | None = None,
    ) -> None:
        self.mock_mode = mock_mode
        self.persist = sessions is not None
        self._returned_existing: set[UUID] = set()
        if (agent is None) == (reviewers is None):
            raise ValueError("Supply either a security agent or a reviewer mapping")
        supplied = {AgentType.SECURITY: agent} if agent is not None else dict(reviewers or {})
        self.reviewers = {kind: supplied[kind] for kind in AgentType if kind in supplied}
        if (
            not self.reviewers
            or len(self.reviewers) != len(supplied)
            or any(reviewer.agent_type != kind for kind, reviewer in self.reviewers.items())
        ):
            raise ValueError("Reviewer mapping must be nonempty and match reviewer categories")
        self.schema_version = (
            3
            if context_service is not None
            else (1 if tuple(self.reviewers) == (AgentType.SECURITY,) else 2)
        )
        self.retrieval_identity = context_service.identity if context_service is not None else None
        self.nodes = ReviewNodes(
            self.reviewers,
            mock_mode=mock_mode,
            sessions=sessions,
            github=github,
            context_service=context_service,
        )
        builder = StateGraph(ReviewState)
        retry = RetryPolicy(
            max_attempts=2,
            initial_interval=0.1,
            max_interval=1,
            jitter=False,
            retry_on=WorkflowTransientError,
        )
        builder.add_node("ingest", self.nodes.ingest, retry_policy=retry)
        builder.add_node("context", self.nodes.context)
        for kind in self.reviewers:
            builder.add_node(kind.value, partial(self.nodes.review, agent_type=kind))
        builder.add_node("aggregate", self.nodes.aggregate)
        builder.add_node("persist", self.nodes.persist, retry_policy=retry)
        builder.add_edge(START, "ingest")
        builder.add_conditional_edges(
            "ingest",
            lambda state: "done" if state.get("reused") else "review",
            {"done": END, "review": "context"},
        )
        for kind in self.reviewers:
            builder.add_edge("context", kind.value)
        builder.add_edge([kind.value for kind in self.reviewers], "aggregate")
        builder.add_edge("aggregate", "persist")
        builder.add_edge("persist", END)
        if checkpointer is None:
            checkpointer = InMemorySaver(
                serde=JsonPlusSerializer(allowed_json_modules=[], allowed_msgpack_modules=[])
            )
        self.graph = builder.compile(
            checkpointer=checkpointer,
            interrupt_before=["persist"] if stop_before_persist else None,
        )

    @staticmethod
    def config(review_id: UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": str(review_id)}}

    def _check_checkpoint(self, state: ReviewState, review_id: UUID) -> None:
        if state.get("schema_version") != self.schema_version:
            raise WorkflowConflictError("Checkpoint workflow version is not supported")
        if state.get("retrieval_identity") != self.retrieval_identity:
            raise WorkflowConflictError("Checkpoint retrieval configuration does not match")
        if state.get("expected_agents", ["security"]) != [kind.value for kind in self.reviewers]:
            raise WorkflowConflictError(
                "Checkpoint reviewer set does not match the configured graph"
            )
        if state.get("review_id") != str(review_id) or state.get("job", {}).get("review_id") != str(
            review_id
        ):
            raise WorkflowConflictError("Checkpoint identity does not match its review thread")
        if (
            state.get("mock_mode") != self.mock_mode
            or state.get("persistence_enabled") != self.persist
        ):
            raise WorkflowConflictError("Resume must use the original mock and persistence modes")

    async def start(self, job: ReviewJob, context: PullRequestContext | None = None) -> ReviewState:
        config = self.config(job.review_id)
        saved = await self.graph.aget_state(config)
        if saved.values:
            self._check_checkpoint(saved.values, job.review_id)
            original = ReviewJob.model_validate(saved.values["job"])
            if original.model_dump(exclude={"created_at"}) != job.model_dump(
                exclude={"created_at"}
            ):
                raise WorkflowConflictError("Review thread already belongs to another job")
            if context is not None and saved.values.get("pr_context") != context.model_dump(
                mode="json"
            ):
                raise WorkflowConflictError("Review thread already contains different PR context")
            if saved.next:
                raise WorkflowConflictError(
                    "Review has a checkpoint; resume it instead of restarting"
                )
            self._returned_existing.add(job.review_id)
            return {**saved.values, "reused": True}
        initial: ReviewState = {
            "schema_version": self.schema_version,
            "retrieval_identity": self.retrieval_identity,
            "expected_agents": [kind.value for kind in self.reviewers],
            "agent_results": {},
            "review_id": str(job.review_id),
            "job": job.model_dump(mode="json"),
            "mock_mode": self.mock_mode,
            "persistence_enabled": self.persist,
            "errors": [],
            "completed_stages": [],
            "status": "QUEUED",
            "persisted": False,
            "reused": False,
        }
        if context is not None:
            initial["pr_context"] = context.model_dump(mode="json")
        return await self.graph.ainvoke(initial, config, durability="sync")

    async def resume(self, review_id: UUID) -> ReviewState:
        config = self.config(review_id)
        saved = await self.graph.aget_state(config)
        if not saved.values:
            raise LookupError("No checkpoint exists for this review ID")
        self._check_checkpoint(saved.values, review_id)
        if saved.next and self.nodes.sessions is not None:
            # A worker may commit failure before its checkpoint write is acknowledged.
            # Canonical terminal failure must not restart unfinished model branches.
            failed = False
            async with self.nodes.transaction() as store:
                current = await store.get_review(review_id)
                if current is not None and current.status == ReviewStatus.FAILED:
                    store._check_job(current, ReviewJob.model_validate(saved.values["job"]))
                    failed = True
            if failed:
                await self.graph.aupdate_state(
                    config,
                    {
                        "status": "FAILED",
                        "persisted": True,
                        "aggregated_review": None,
                        "completed_stages": ["persist"],
                    },
                    as_node="persist",
                )
                saved = await self.graph.aget_state(config)
        if not saved.next:
            self._returned_existing.add(review_id)
            return {**saved.values, "reused": True}
        return await self.graph.ainvoke(None, config, durability="sync")

    async def report(self, review_id: UUID) -> WorkflowReport:
        snapshot = await self.graph.aget_state(self.config(review_id))
        if not snapshot.values:
            raise LookupError("No checkpoint exists for this review ID")
        state = snapshot.values
        self._check_checkpoint(state, review_id)
        return WorkflowReport(
            review_id=review_id,
            status=state["status"],
            mock_mode=state["mock_mode"],
            persisted=state["persisted"],
            reused=state["reused"] or review_id in self._returned_existing,
            paused=bool(snapshot.next),
            completed_stages=state["completed_stages"],
            pending_nodes=list(snapshot.next),
            errors=state["errors"],
            review=state.get("aggregated_review"),
            agent_results=state.get("agent_results", {}),
            context_source=state.get("context_source", "diff_only"),
            retrieved_context=state.get("retrieved_context", []),
            context_warnings=state.get("review_context", {}).get("warnings", []),
        )

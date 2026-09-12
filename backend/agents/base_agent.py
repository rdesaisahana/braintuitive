"""LangChain ReAct base agent.

Every Braintuitive agent inherits from :class:`BaseAgent` and supplies two
things: the tools it may call and the system prompt that governs how it
reasons. The ReAct scaffolding -- the Thought / Action / Observation loop,
the executor, retries, timeouts and tracing -- lives here so no subclass
has to reimplement it.

Subclass template:
    from typing import List
    from langchain.tools import Tool
    from agents.base_agent import BaseAgent

    class QuizGeneratorAgent(BaseAgent):
        def __init__(self) -> None:
            super().__init__(agent_name="QuizGenerator", verbose=True)

        def get_tools(self) -> List[Tool]:
            return [Tool(name="search_curriculum", func=..., description="...")]

        def get_system_prompt(self) -> str:
            return "You are an expert item writer..."

    agent = QuizGeneratorAgent()
    agent.setup_agent()
    result = agent.execute("Generate 10 beginner questions", context={"sub_unit": "1.2"})

The loop the executor runs:
    1. THINK    -- "What do I need to do?"          (Thought:)
    2. ACT      -- call a tool                      (Action: / Action Input:)
    3. OBSERVE  -- read the tool result             (Observation:)
    4. repeat until it has enough
    5. RETURN   -- Final Answer:
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import Tool
from langchain_openai import ChatOpenAI

from config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# ReAct prompt scaffold
# --------------------------------------------------------------------------- #

# Defined inline rather than pulled from LangChain Hub so that agent behaviour
# is deterministic, reviewable in this repo, and works with no network at boot.
REACT_TEMPLATE = """{system_prompt}

You have access to the following tools:

{tools}

Use this exact format:

Question: the input question you must answer
Thought: reason about what to do next
Action: the action to take, must be exactly one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation cycle can repeat as needed)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Rules:
- Emit exactly one Action per Thought, then wait for the Observation.
- Never invent an Observation; only use what a tool actually returned.
- If the tools cannot answer the question, say so plainly in the Final Answer.
- When the answer must be structured data, put valid JSON in the Final Answer.

{context_block}
Begin!

Question: {input}
Thought:{agent_scratchpad}"""


class AgentExecutionError(RuntimeError):
    """Raised when an agent cannot complete its run."""


# --------------------------------------------------------------------------- #
# Base agent
# --------------------------------------------------------------------------- #


class BaseAgent(ABC):
    """Abstract ReAct agent backed by the Nebius LLM.

    Attributes:
        agent_name: Human-readable name, used in logs and traces.
        verbose: Whether the executor prints its reasoning to stdout.
        temperature: Sampling temperature; defaults to ``LLM_TEMPERATURE``.
        max_iterations: Cap on ReAct loops before the executor stops.
    """

    def __init__(
        self,
        agent_name: str,
        verbose: bool | None = None,
        temperature: float | None = None,
        max_iterations: int | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        request_timeout: int | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.verbose = settings.AGENT_VERBOSE if verbose is None else verbose
        self.temperature = settings.LLM_TEMPERATURE if temperature is None else temperature
        self.max_iterations = max_iterations or settings.AGENT_MAX_ITERATIONS
        self.model = model or settings.NEBIUS_MODEL
        # Agents that emit long structured output (a batch of questions with
        # explanations) need far more room than a conversational reply.
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self.request_timeout = request_timeout or settings.LLM_REQUEST_TIMEOUT

        self._llm: ChatOpenAI | None = None
        self._tools: list[Tool] | None = None
        self._executor: AgentExecutor | None = None
        self.logger = logging.getLogger(f"agent.{agent_name}")

    # ------------------------------------------------------------------ #
    # Subclass contract
    # ------------------------------------------------------------------ #

    @abstractmethod
    def get_tools(self) -> list[Tool]:
        """Return the tools this agent may call.

        Tool descriptions are the only thing the model sees when choosing, so
        write them as instructions: what the tool does, what input it expects,
        and when to prefer it.
        """

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt that governs this agent's reasoning."""

    # ------------------------------------------------------------------ #
    # Wiring
    # ------------------------------------------------------------------ #

    def get_llm(self) -> ChatOpenAI:
        """Build (once) the Nebius-backed chat model.

        Nebius exposes an OpenAI-compatible API, so ``ChatOpenAI`` pointed at
        ``NEBIUS_BASE_URL`` is the supported client.

        Raises:
            AgentExecutionError: If ``NEBIUS_API_KEY`` is not configured.
        """
        if self._llm is not None:
            return self._llm

        if not settings.nebius_configured:
            raise AgentExecutionError(
                "NEBIUS_API_KEY is not set - cannot construct the LLM. "
                "Add it to backend/.env before running agents."
            )

        self._llm = ChatOpenAI(
            model=self.model,
            api_key=settings.NEBIUS_API_KEY,
            base_url=settings.NEBIUS_BASE_URL,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.request_timeout,
            max_retries=2,
            # ReAct depends on the model stopping at the observation boundary;
            # without this the model happily hallucinates its own Observation.
            # First-class argument, not model_kwargs, which langchain_openai
            # warns about on every construction.
            stop=["\nObservation:", "\n\tObservation:"],
        )
        return self._llm

    @property
    def tools(self) -> list[Tool]:
        """The tool list, built once and cached."""
        if self._tools is None:
            self._tools = self.get_tools()
        return self._tools

    def build_prompt(self, context_block: str = "") -> PromptTemplate:
        """Assemble the ReAct prompt template for this agent."""
        return PromptTemplate(
            template=REACT_TEMPLATE,
            input_variables=["input", "agent_scratchpad"],
            partial_variables={
                "system_prompt": self.get_system_prompt().strip(),
                "context_block": context_block,
            },
        )

    def setup_agent(self, context_block: str = "") -> AgentExecutor:
        """Construct the ReAct agent and its executor.

        Call once before :meth:`execute`. ``execute`` will call it lazily if
        you forget, but doing it explicitly surfaces configuration errors at
        startup rather than mid-request.

        Returns:
            The configured :class:`AgentExecutor`.
        """
        tools = self.tools
        if not tools:
            raise AgentExecutionError(f"{self.agent_name} declared no tools.")

        agent = create_react_agent(
            llm=self.get_llm(),
            tools=tools,
            prompt=self.build_prompt(context_block),
        )

        self._executor = AgentExecutor(
            agent=agent,
            tools=tools,
            verbose=self.verbose,
            max_iterations=self.max_iterations,
            max_execution_time=settings.AGENT_TIMEOUT,
            # Feed malformed output back to the model instead of raising: small
            # models routinely fumble the ReAct format on the first try.
            handle_parsing_errors=(
                "Your last message did not match the required format. "
                "Reply with either 'Action:' plus 'Action Input:', or 'Final Answer:'."
            ),
            return_intermediate_steps=True,
            early_stopping_method="force",
        )
        self.logger.info(
            "%s ready (model=%s, tools=%d, max_iter=%d)",
            self.agent_name,
            self.model,
            len(tools),
            self.max_iterations,
        )
        return self._executor

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_context(context: dict[str, Any] | None) -> str:
        """Render a context dict into a prompt block the model can read."""
        if not context:
            return ""
        try:
            rendered = json.dumps(context, indent=2, default=str)
        except (TypeError, ValueError):
            rendered = str(context)
        return f"Context for this task:\n{rendered}\n"

    def execute(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the ReAct loop against a prompt.

        Args:
            prompt: The task, phrased as a question or instruction.
            context: Structured data injected into the prompt (student id,
                sub-unit, difficulty, retrieved chunks...).

        Returns:
            A dict with:
                ``success`` (bool), ``agent`` (str), ``output`` (str),
                ``steps`` (list of {tool, input, observation}),
                ``elapsed_seconds`` (float), and ``error`` (str | None).
            Failures are returned, not raised, so one bad agent run cannot
            take down the request handling it.
        """
        started = time.perf_counter()

        try:
            executor = self._executor or self.setup_agent(self._format_context(context))
            self.logger.info("%s executing: %s", self.agent_name, prompt[:120])

            raw = executor.invoke({"input": prompt})
            elapsed = time.perf_counter() - started

            steps = self._summarise_steps(raw.get("intermediate_steps", []))
            self.logger.info(
                "%s finished in %.2fs across %d step(s)", self.agent_name, elapsed, len(steps)
            )
            return {
                "success": True,
                "agent": self.agent_name,
                "output": raw.get("output", ""),
                "steps": steps,
                "elapsed_seconds": round(elapsed, 3),
                "error": None,
            }

        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.logger.exception("%s failed after %.2fs: %s", self.agent_name, elapsed, exc)
            return {
                "success": False,
                "agent": self.agent_name,
                "output": "",
                "steps": [],
                "elapsed_seconds": round(elapsed, 3),
                "error": str(exc),
            }

    async def aexecute(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Async counterpart to :meth:`execute`, for use inside FastAPI routes."""
        started = time.perf_counter()

        try:
            executor = self._executor or self.setup_agent(self._format_context(context))
            self.logger.info("%s executing (async): %s", self.agent_name, prompt[:120])

            raw = await executor.ainvoke({"input": prompt})
            elapsed = time.perf_counter() - started

            steps = self._summarise_steps(raw.get("intermediate_steps", []))
            return {
                "success": True,
                "agent": self.agent_name,
                "output": raw.get("output", ""),
                "steps": steps,
                "elapsed_seconds": round(elapsed, 3),
                "error": None,
            }

        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.logger.exception("%s failed after %.2fs: %s", self.agent_name, elapsed, exc)
            return {
                "success": False,
                "agent": self.agent_name,
                "output": "",
                "steps": [],
                "elapsed_seconds": round(elapsed, 3),
                "error": str(exc),
            }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _summarise_steps(intermediate_steps: list[Any]) -> list[dict[str, str]]:
        """Flatten LangChain's (AgentAction, observation) pairs for logging."""
        summary: list[dict[str, str]] = []
        for step in intermediate_steps:
            try:
                action, observation = step
                summary.append(
                    {
                        "tool": getattr(action, "tool", "unknown"),
                        "input": str(getattr(action, "tool_input", ""))[:500],
                        "observation": str(observation)[:1000],
                    }
                )
            except (TypeError, ValueError):  # pragma: no cover - shape drift
                summary.append({"tool": "unknown", "input": "", "observation": str(step)[:500]})
        return summary

    @staticmethod
    def parse_json_output(output: str) -> Any | None:
        """Best-effort extraction of a JSON object/array from a Final Answer.

        Models wrap JSON in prose or ```json fences often enough that callers
        should not have to handle it individually.

        Returns:
            The parsed object, or None if nothing valid could be recovered.
        """
        if not output:
            return None

        text = output.strip()

        # Strip a fenced block if present.
        if "```" in text:
            fenced = text.split("```")
            for block in fenced:
                candidate = block.strip()
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith(("{", "[")):
                    text = candidate
                    break

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Fall back to the outermost brace/bracket span.
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{self.__class__.__name__} name={self.agent_name!r} model={self.model!r}>"


__all__ = ["BaseAgent", "AgentExecutionError", "REACT_TEMPLATE"]

"""Local tools exposed to the OpenAI Realtime conversation."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Protocol
import unicodedata

from robot.services import ROBOT_BEHAVIORS, RobotBehavior, RobotBehaviorService


LIST_BEHAVIORS_TOOL = "list_robot_behaviors"
PERFORM_BEHAVIOR_TOOL = "perform_robot_behavior"
SEARCH_COMPANY_KNOWLEDGE_TOOL = "search_digitalsign_knowledge"
DEFAULT_COMPANY_KNOWLEDGE_PATH = (
    Path(__file__).resolve().parent / "knowledge" / "digitalsign.json"
)


class ToolProvider(Protocol):
    """One group of Realtime function tools."""

    def definitions(self) -> list[dict[str, object]]: ...

    def supports(self, tool_name: str) -> bool: ...

    def execute(
        self, tool_name: str, arguments: object
    ) -> dict[str, object]: ...


class BehaviorService(Protocol):
    """Behavior operations needed by the conversation tool dispatcher."""

    def list(self) -> tuple[tuple[RobotBehavior, ...], bool]: ...

    def execute(self, name: str, *, hold: float = 2.0) -> RobotBehavior: ...


class RobotBehaviorTools:
    """Define, validate, and execute the conversation's robot tools."""

    def __init__(
        self,
        network_interface: str,
        *,
        channel_initialized: bool = False,
        service: BehaviorService | None = None,
    ) -> None:
        self._service = service or RobotBehaviorService(
            network_interface,
            initialize_channel=not channel_initialized,
        )

    @staticmethod
    def definitions() -> list[dict[str, object]]:
        """Return strict Realtime function definitions for safe behavior access."""
        behavior_names = [behavior.name for behavior in ROBOT_BEHAVIORS]
        return [
            {
                "type": "function",
                "name": LIST_BEHAVIORS_TOOL,
                "description": (
                    "Inspect which official physical arm behaviors you can "
                    "perform with your Unitree G1 body. This only reads your "
                    "capabilities and does not move your body."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": PERFORM_BEHAVIOR_TOOL,
                "description": (
                    "Perform one official physical arm behavior with your "
                    "Unitree G1 body. Call immediately when the user explicitly "
                    "asks you to perform it; no confirmation turn is needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "behavior": {
                            "type": "string",
                            "description": "Exact official behavior name.",
                            "enum": behavior_names,
                        },
                        "hold_seconds": {
                            "type": "number",
                            "description": (
                                "Seconds before releasing a held arm pose."
                            ),
                            "minimum": 0.1,
                            "maximum": 10.0,
                        },
                    },
                    "required": ["behavior"],
                    "additionalProperties": False,
                },
            },
        ]

    @staticmethod
    def supports(tool_name: str) -> bool:
        return tool_name in {LIST_BEHAVIORS_TOOL, PERFORM_BEHAVIOR_TOOL}

    def execute(
        self, tool_name: str, arguments: object
    ) -> dict[str, object]:
        """Execute one validated tool call and return JSON-serializable output."""
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")

        if tool_name == LIST_BEHAVIORS_TOOL:
            if arguments:
                raise ValueError("list_robot_behaviors does not accept arguments.")
            behaviors, confirmed_by_robot = self._service.list()
            return {
                "ok": True,
                "confirmed_by_robot": confirmed_by_robot,
                "behaviors": [
                    {
                        "name": behavior.name,
                        "action_id": behavior.action_id,
                        "releases_after_hold": behavior.release_after,
                    }
                    for behavior in behaviors
                ],
            }

        if tool_name == PERFORM_BEHAVIOR_TOOL:
            unexpected = set(arguments) - {"behavior", "hold_seconds"}
            if unexpected:
                names = ", ".join(sorted(unexpected))
                raise ValueError(f"Unexpected tool arguments: {names}.")
            behavior_name = arguments.get("behavior")
            if not isinstance(behavior_name, str) or not behavior_name.strip():
                raise ValueError("behavior must be a non-empty string.")
            hold = arguments.get("hold_seconds", 2.0)
            if isinstance(hold, bool) or not isinstance(hold, (int, float)):
                raise ValueError("hold_seconds must be a number.")
            hold = float(hold)
            if not 0.1 <= hold <= 10.0:
                raise ValueError("hold_seconds must be between 0.1 and 10.")
            behavior = self._service.execute(behavior_name, hold=hold)
            return {
                "ok": True,
                "behavior": behavior.name,
                "action_id": behavior.action_id,
                "released_after_hold": behavior.release_after,
            }

        raise ValueError(f"Unknown conversation tool {tool_name!r}.")


class CompanyKnowledgeTools:
    """Search a curated, local DigitalSign knowledge base."""

    def __init__(
        self,
        knowledge_path: Path | str = DEFAULT_COMPANY_KNOWLEDGE_PATH,
    ) -> None:
        self._knowledge_path = Path(knowledge_path)

    @staticmethod
    def definitions() -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "name": SEARCH_COMPANY_KNOWLEDGE_TOOL,
                "description": (
                    "Search the application's curated DigitalSign company "
                    "knowledge. Use this whenever the user asks who DigitalSign "
                    "is, what it does, its history, presence, services, or other "
                    "facts about the organization. Prefer these results over "
                    "the model's general knowledge."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "The user's question or the DigitalSign topic "
                                "to retrieve."
                            ),
                            "minLength": 1,
                            "maxLength": 300,
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    @staticmethod
    def supports(tool_name: str) -> bool:
        return tool_name == SEARCH_COMPANY_KNOWLEDGE_TOOL

    def execute(
        self, tool_name: str, arguments: object
    ) -> dict[str, object]:
        if tool_name != SEARCH_COMPANY_KNOWLEDGE_TOOL:
            raise ValueError(f"Unknown conversation tool {tool_name!r}.")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        if set(arguments) != {"query"}:
            raise ValueError(
                "search_digitalsign_knowledge requires only the query argument."
            )
        query = arguments["query"]
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if len(query) > 300:
            raise ValueError("query must contain at most 300 characters.")

        knowledge = self._load_knowledge()
        entries = knowledge["entries"]
        ranked = sorted(
            (
                (self._score_entry(query, entry), index, entry)
                for index, entry in enumerate(entries)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        matches = [
            entry
            for score, _index, entry in ranked
            if score > 0
        ][:3]
        if not matches:
            matches = entries[:1]
        return {
            "ok": True,
            "organization": knowledge["organization"],
            "knowledge_updated": knowledge["updated"],
            "matches": [
                {
                    "id": entry["id"],
                    "title": entry["title"],
                    "content": entry["content"],
                    "source_urls": entry["source_urls"],
                }
                for entry in matches
            ],
        }

    def _load_knowledge(self) -> dict[str, object]:
        try:
            data = json.loads(self._knowledge_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Company knowledge file not found: {self._knowledge_path}."
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Could not read company knowledge: {error}."
            ) from error

        if (
            not isinstance(data, dict)
            or not isinstance(data.get("organization"), str)
            or not isinstance(data.get("updated"), str)
            or not isinstance(data.get("entries"), list)
            or not data["entries"]
        ):
            raise RuntimeError("Company knowledge has an invalid structure.")
        for entry in data["entries"]:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not isinstance(entry.get("title"), str)
                or not isinstance(entry.get("content"), str)
                or not isinstance(entry.get("keywords"), list)
                or not all(
                    isinstance(keyword, str)
                    for keyword in entry["keywords"]
                )
                or not isinstance(entry.get("source_urls"), list)
                or not all(
                    isinstance(url, str)
                    for url in entry["source_urls"]
                )
            ):
                raise RuntimeError(
                    "Company knowledge contains an invalid entry."
                )
        return data

    @classmethod
    def _score_entry(
        cls, query: str, entry: dict[str, object]
    ) -> int:
        query_terms = cls._terms(query)
        title_terms = cls._terms(str(entry["title"]))
        keyword_terms = cls._terms(" ".join(entry["keywords"]))
        content_terms = cls._terms(str(entry["content"]))
        return sum(
            5 if term in title_terms else 3 if term in keyword_terms else 1
            for term in query_terms
            if term in title_terms or term in keyword_terms or term in content_terms
        )

    @staticmethod
    def _terms(value: str) -> set[str]:
        normalized = unicodedata.normalize("NFKD", value.lower())
        ascii_value = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return {
            term
            for term in re.findall(r"[a-z0-9]+", ascii_value)
            if len(term) > 2
        }

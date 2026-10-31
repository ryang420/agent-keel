"""A dream attempt's staged proposal (procedural learning design §5). The proposal tools change only this stage; each
change passes the evidence, duplicate, validation and consumer-precedence gates against the attempt's snapshot as it
is made, so a refusal goes straight back to the model. The store commits the whole proposal atomically if nothing it
read has moved."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from keel.dream_gates import duplicates, evidence_refusal, precedence_refusal
from keel.learned import MAX_SKILLS, check, document
from keel.tools.base import tool_json

STAGED = ("skill_view", "create_skill", "revise_skill", "merge_skills", "retire_skill", "dismiss_observation")
FIELDS = ("name", "description", "body", "category")
MAX_REASON = 300


@dataclass
class DreamProposal:
    ops: list[dict] = field(default_factory=list)  # store changes in order; each cites observation ids
    read: dict[str, int] = field(default_factory=dict)  # skill id -> the version this attempt read
    consumed: set[str] = field(default_factory=set)
    dismissed: dict[str, str] = field(default_factory=dict)  # observation id -> reason


def _refusal(message: str, code: str = "invalid") -> tuple[str, bool]:
    return tool_json({"success": False, "code": code, "error": message}), True


def _rationale(args: dict) -> str | None:
    value = args.get("rationale")
    return value[:MAX_REASON] if isinstance(value, str) else None


class StagedDream:
    def __init__(self, observations, skills, shipped: set[str]) -> None:
        """`observations` are this attempt's pending input, `skills` the profile's active and suspended learned skills
        and `shipped` the names of its enabled deployment skills, all read when the attempt began."""
        self.observations = {item.id: item for item in observations}
        self.skills = {skill.id: skill for skill in skills}
        self.shipped = shipped
        self.proposal = DreamProposal()
        # The live skills the proposal would leave, by key (skill id, or "new:<n>" for a staged create).
        self.live = {skill.id: (skill.name, skill.description) for skill in skills}

    def call(self, name: str, arguments: str) -> tuple[str, bool]:
        """One proposal tool call: (JSON result, error)."""
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return _refusal("Arguments must be a JSON object.")
        if not isinstance(args, dict):
            return _refusal("Arguments must be a JSON object.")
        handlers = {"skill_view": self._view, "create_skill": self._create, "revise_skill": self._revise,
                    "merge_skills": self._merge, "retire_skill": self._retire,
                    "dismiss_observation": self._dismiss}
        return handlers[name](args)

    def _view(self, args: dict) -> tuple[str, bool]:
        name = args.get("name")
        if name in self.shipped:
            return _refusal(f"{name} is a deployment skill; its text is not shown here. Do not duplicate it.",
                            "not_found")
        skill = next((s for s in self.skills.values() if s.name == name), None)
        if skill is None:
            return _refusal(f"No learned skill is named {name}.", "not_found")
        return tool_json({"success": True, "id": skill.id, "name": skill.name, "state": skill.state,
                          "version": skill.version, "content": document(skill)}), False

    def _cited(self, args: dict) -> tuple[list, str | None]:
        ids = args.get("observations", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return [], "observations must be a list of observation ids."
        unknown = [item for item in ids if item not in self.observations]
        if unknown:
            return [], f"Cite only the observations you were given; unknown: {', '.join(unknown[:3])}."
        if any(item in self.proposal.dismissed for item in ids):
            return [], "A dismissed observation cannot be cited."
        return [self.observations[item] for item in dict.fromkeys(ids)], None

    def _existing(self, skill_id) -> tuple[object | None, str | None]:
        skill = self.skills.get(skill_id) if isinstance(skill_id, str) else None
        if skill is None:
            return None, "No such learned skill."
        if skill.id in self.proposal.read:
            return None, f"Skill {skill.name} already changes in this proposal; each skill changes once per dream."
        return skill, None

    def _invalid(self, fields: dict, excluded: set[str]) -> str | None:
        if reason := check(fields["name"], fields["description"], fields["body"], fields["category"]):
            return reason
        if fields["name"] in self.shipped:
            return "A deployment skill enabled for this profile has that name."
        if any(name == fields["name"] for key, (name, _) in self.live.items() if key not in excluded):
            return "A learned skill already has that name."
        return None

    def _stage(self, op: dict, args: dict, cited: list) -> None:
        op["rationale"] = _rationale(args)
        op["cited"] = [item.id for item in cited]
        self.proposal.ops.append(op)
        self.proposal.consumed.update(op["cited"])

    def _create(self, args: dict) -> tuple[str, bool]:
        cited, reason = self._cited(args)
        if reason:
            return _refusal(reason)
        if reason := evidence_refusal("create", cited):
            return _refusal(reason, "evidence")
        fields = {key: args.get(key) for key in FIELDS}
        if reason := self._invalid(fields, set()):
            return _refusal(reason)
        for name, description in self.live.values():
            if duplicates(fields["name"], fields["description"], name, description):
                return _refusal(f"That reads like skill {name}; revise or merge it instead.", "duplicate")
        if len(self.live) >= MAX_SKILLS:
            return _refusal(f"This profile already has {MAX_SKILLS} learned skills; merge or retire one first.",
                            "limit")
        self.live[f"new:{len(self.proposal.ops)}"] = (fields["name"], fields["description"])
        self._stage({"action": "create", **fields}, args, cited)
        return tool_json({"success": True, "staged": "create", "name": fields["name"]}), False

    def _changes(self, skills: list, args: dict, action: str) -> tuple[list, str | None, str]:
        """Shared checks of a change to existing skills: (cited observations, refusal or None, refusal code)."""
        cited, reason = self._cited(args)
        if reason:
            return [], reason, "invalid"
        if reason := evidence_refusal(action, cited):
            return [], reason, "evidence"
        for skill in skills:
            if reason := precedence_refusal(skill, cited):
                return [], reason, "consumer_precedence"
        return cited, None, ""

    def _revise(self, args: dict) -> tuple[str, bool]:
        skill, reason = self._existing(args.get("skill_id"))
        if reason:
            return _refusal(reason)
        cited, reason, code = self._changes([skill], args, "revise")
        if reason:
            return _refusal(reason, code)
        given = {key: args[key] for key in FIELDS if key in args}
        if not given:
            return _refusal("Give at least one of name, description, body or category.")
        fields = {"name": skill.name, "description": skill.description, "body": skill.body,
                  "category": skill.category, **given}
        if reason := self._invalid(fields, {skill.id}):
            return _refusal(reason)
        self.proposal.read[skill.id] = skill.version
        self.live[skill.id] = (fields["name"], fields["description"])
        self._stage({"action": "revise", "skill_id": skill.id, "expected_version": skill.version, **given}, args,
                    cited)
        return tool_json({"success": True, "staged": "revise", "name": fields["name"]}), False

    def _merge(self, args: dict) -> tuple[str, bool]:
        target, reason = self._existing(args.get("target_id"))
        if reason:
            return _refusal(reason)
        ids = args.get("source_ids")
        if (not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids)
                or target.id in ids):
            return _refusal("source_ids must list the other skills to fold into the target.")
        sources = []
        for skill_id in dict.fromkeys(ids):
            skill, reason = self._existing(skill_id)
            if reason:
                return _refusal(reason)
            sources.append(skill)
        cited, reason, code = self._changes([target, *sources], args, "merge")
        if reason:
            return _refusal(reason, code)
        given = {key: args[key] for key in FIELDS if key in args}
        if "body" not in given:
            return _refusal("Give the merged body.")
        fields = {"name": target.name, "description": target.description, "body": target.body,
                  "category": target.category, **given}
        if reason := self._invalid(fields, {target.id, *(skill.id for skill in sources)}):
            return _refusal(reason)
        for skill in sources:
            self.proposal.read[skill.id] = skill.version
            del self.live[skill.id]
            self._stage({"action": "retire", "skill_id": skill.id, "expected_version": skill.version}, args, cited)
            self.proposal.ops[-1]["rationale"] = f"merged into {fields['name']}"
        self.proposal.read[target.id] = target.version
        self.live[target.id] = (fields["name"], fields["description"])
        self._stage({"action": "merge", "skill_id": target.id, "expected_version": target.version, **given}, args,
                    cited)
        return tool_json({"success": True, "staged": "merge", "name": fields["name"]}), False

    def _retire(self, args: dict) -> tuple[str, bool]:
        skill, reason = self._existing(args.get("skill_id"))
        if reason:
            return _refusal(reason)
        cited, reason, code = self._changes([skill], args, "retire")
        if reason:
            return _refusal(reason, code)
        self.proposal.read[skill.id] = skill.version
        del self.live[skill.id]
        self._stage({"action": "retire", "skill_id": skill.id, "expected_version": skill.version}, args, cited)
        return tool_json({"success": True, "staged": "retire", "name": skill.name}), False

    def _dismiss(self, args: dict) -> tuple[str, bool]:
        observation_id, reason = args.get("observation_id"), args.get("reason")
        if observation_id not in self.observations:
            return _refusal("Dismiss only an observation you were given.")
        if observation_id in self.proposal.consumed:
            return _refusal("That observation supports a staged change; it cannot also be dismissed.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
            return _refusal(f"reason must be non-empty text of at most {MAX_REASON} characters.")
        self.proposal.dismissed[observation_id] = reason
        return tool_json({"success": True, "staged": "dismiss"}), False

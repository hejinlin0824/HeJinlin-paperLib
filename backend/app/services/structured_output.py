"""Schema-constrained output helpers for OpenAI-compatible chat endpoints."""
from __future__ import annotations
import json
from typing import Any

class StructuredOutputValidationError(ValueError):
    pass

def response_format(schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}

def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required if required is not None else list(properties), "additionalProperties": False}

def chunk_analysis_schema() -> dict[str, Any]:
    evidence = object_schema({"block_id": {"type": "integer"}, "quote": {"type": "string"}})
    claim = object_schema({"claim": {"type": "string"}, "kind": {"type": "string"},
                           "evidence": {"type": "array", "items": evidence}})
    return object_schema({
        "section_role": {"type": "string"}, "summary": {"type": "string"},
        "claims": {"type": "array", "items": claim},
        "argument_steps": {"type": "array", "items": {"type": "string"}},
        "candidate_contributions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    })

def batch_chunk_schema() -> dict[str, Any]:
    item = object_schema({
        "chunk_id": {"type": "integer"}, "section_role": {"type": "string"}, "summary": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object", "properties": {"claim": {"type": "string"}, "kind": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "object", "properties": {"block_id": {"type": "integer"}, "quote": {"type": "string"}}, "required": ["block_id", "quote"], "additionalProperties": False}}, "required": ["claim", "kind", "evidence"], "additionalProperties": False}},
        "argument_steps": {"type": "array", "items": {"type": "string"}},
        "candidate_contributions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    })
    return object_schema({"chunks": {"type": "array", "items": item}})

def synthesis_schema() -> dict[str, Any]:
    evidence_list = {"type": "array", "items": {"type": "object", "properties": {
        "block_id": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["block_id", "quote"], "additionalProperties": False}}
    evidence_one = {"type": "object", "properties": {
        "block_id": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["block_id", "quote"], "additionalProperties": False}
    contribution = object_schema({
        "id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
        "kind": {"type": "string"}, "importance": {"type": "string"},
        "knowledge_role": {"type": "string"}, "parent_id": {"type": ["string", "null"]},
        "evidence": evidence_list,
    })
    theory_item = object_schema({"name": {"type": "string"}, "role": {"type": "string"}, "evidence": evidence_list})
    method_item = object_schema({"name": {"type": "string"}, "description": {"type": "string"}, "evidence": evidence_list})
    finding = object_schema({"claim": {"type": "string"}, "evidence": evidence_list})
    relation = object_schema({"source_id": {"type": "string"}, "target_id": {"type": "string"},
        "relation_type": {"type": "string"}, "evidence": evidence_one})
    hyperedge = object_schema({"id": {"type": "string"}, "relation_type": {"type": "string"},
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "target_id": {"type": "string"}, "statement": {"type": "string"}, "evidence": evidence_one})
    argument = object_schema({"claim": {"type": "string"},
        "premises": {"type": "array", "items": {"type": "string"}},
        "conclusion": {"type": "string"}, "evidence": evidence_list})
    return object_schema({
        "paper_type": {"type": "string", "enum": ["theory", "algorithm", "system", "empirical", "survey", "other"]},
        "problem": {"type": "string"}, "thesis": {"type": "string"},
        "contributions": {"type": "array", "items": contribution},
        "theoretical_framework": {"type": "array", "items": theory_item},
        "method_architecture": {"type": "array", "items": method_item},
        "key_findings": {"type": "array", "items": finding},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "relations": {"type": "array", "items": relation},
        "theory_logic": {"type": "array", "items": hyperedge},
        "argument_map": {"type": "array", "items": argument},
    })

def verification_schema() -> dict[str, Any]:
    item = object_schema({"id": {"type": "string"},
        "verdict": {"type": "string", "enum": ["entailed", "not_entailed", "uncertain"]},
        "reason": {"type": "string"}})
    return object_schema({"verifications": {"type": "array", "items": item}})

def parse_and_validate(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StructuredOutputValidationError(f"invalid_json: {exc}") from exc
    errors = validate_instance(value, schema)
    if errors:
        raise StructuredOutputValidationError("schema_invalid: " + "; ".join(errors[:8]))
    return value

def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    expected = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
    }
    if isinstance(expected, list):
        if not any(kind in checks and checks[kind](value) for kind in expected):
            return [f"{path} must be one of {expected}"]
    elif expected in checks and not checks[expected](value):
        return [f"{path} must be {expected}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if expected == "object":
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key} is not allowed")
        for key, child in props.items():
            if key in value:
                errors.extend(validate_instance(value[key], child, f"{path}.{key}"))
    elif expected == "array":
        for index, item in enumerate(value):
            errors.extend(validate_instance(item, schema.get("items") or {}, f"{path}[{index}]"))
    return errors

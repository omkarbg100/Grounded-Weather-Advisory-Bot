"""
JSON-Schema normalisation for Gemini function declarations.

Pydantic emits JSON Schema constructs that the Gemini function-calling API
rejects: `$ref`/`$defs` indirection, `title`, `default`, `const`, and so on.
`inline_schema` resolves every reference against the root `$defs` table and
drops the unsupported keywords, so any `BaseModel` can be turned into a valid
tool signature without hand-writing schema dicts.
"""
from typing import Any, Dict, List, Optional

# Keywords Gemini does not accept, or that only add noise to the signature.
_DROPPED_KEYWORDS = {
    "$defs",
    "$schema",
    "$id",
    "$anchor",
    "$ref",
    "title",
    "default",
    "examples",
    "additionalProperties",
    "discriminator",
    "deprecated",
    "readOnly",
    "writeOnly",
    "uniqueItems",
    "minItems",
    "maxItems",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
}

# `format` is only meaningful for a couple of values; sending unknown strings
# makes Gemini reject the whole declaration.
_RETAINED_FORMATS = {"date-time", "enum"}

_JSON_TYPE_FOR_PY = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


def inline_schema(
    schema: Dict[str, Any],
    defs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Return a Gemini-safe copy of `schema`.

    `$ref`s are resolved against `defs` (defaults to the root `$defs` table),
    single-variant nullable unions collapse to their non-null branch, and
    unsupported keywords are removed.
    """
    if not isinstance(schema, dict):
        raise TypeError("inline_schema expects a JSON Schema mapping")

    resolved_defs = defs if defs is not None else (schema.get("$defs") or {})
    return _resolve(schema, resolved_defs, frozenset())


def _resolve(node: Any, defs: Dict[str, Any], seen: frozenset) -> Any:
    if isinstance(node, list):
        return [_resolve(item, defs, seen) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _lookup_def(ref, defs)
        if target is None:
            # Dangling reference: describe it as a free-form object rather than
            # failing the whole declaration.
            return {"type": "object"}

        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            # Recursive model. Gemini cannot express recursion, so widen it.
            return {"type": "object", "description": f"Recursive definition of '{name}'."}

        resolved = _resolve(target, defs, seen | {name})
        siblings = {
            key: _resolve(value, defs, seen)
            for key, value in node.items()
            if key not in ("$ref", "$defs")
        }
        if isinstance(resolved, dict):
            merged = dict(resolved)
            merged.update(siblings)
            return merged
        return resolved

    if "const" in node and "enum" not in node:
        const_value = node["const"]
        collapsed: Dict[str, Any] = {"enum": [const_value]}
        inferred = _JSON_TYPE_FOR_PY.get(type(const_value))
        if inferred and inferred != "null":
            collapsed["type"] = inferred
        return collapsed

    out: Dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED_KEYWORDS:
            continue
        if key == "format":
            if value not in _RETAINED_FORMATS:
                continue
            out[key] = value
            continue
        if key in ("anyOf", "oneOf"):
            variants = _resolve(value, defs, seen)
            collapsed = _collapse_nullable_union(variants)
            if isinstance(collapsed, dict):
                # Optional[T]: drop the union wrapper entirely and mark the
                # remaining branch nullable, which is what Gemini expects.
                out.update(collapsed)
            else:
                out[key] = collapsed
            continue
        out[key] = _resolve(value, defs, seen)

    return out


def _collapse_nullable_union(variants: Any) -> Any:
    """`anyOf: [T, null]` becomes `T`. Genuine unions are left intact."""
    if not isinstance(variants, list):
        return variants

    non_null = [
        v for v in variants
        if not (isinstance(v, dict) and v.get("type") == "null")
    ]
    if len(non_null) == 1 and len(non_null) < len(variants):
        collapsed = dict(non_null[0])
        collapsed["nullable"] = True
        return collapsed
    if not non_null:
        return {"type": "string", "nullable": True}
    return non_null


def _lookup_def(ref: str, defs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not defs:
        return None
    name = ref.rsplit("/", 1)[-1]
    if name in defs:
        return defs[name]
    # Nested definitions are addressed as "#/$defs/Outer/$defs/Inner".
    for candidate in defs.values():
        nested = (candidate or {}).get("$defs")
        if isinstance(nested, dict) and name in nested:
            return nested[name]
    return None


def strip_titles(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Remove `title` keys in place-ish fashion. Kept for readable test fixtures."""
    cleaned: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if isinstance(value, dict):
            cleaned[key] = strip_titles(value)
        elif isinstance(value, list):
            cleaned[key] = [strip_titles(v) if isinstance(v, dict) else v for v in value]
        else:
            cleaned[key] = value
    return cleaned


def collect_schema_names(schemas: List[Dict[str, Any]]) -> List[str]:
    """Helper for tests: top-level property names across a schema list."""
    names: List[str] = []
    for schema in schemas:
        names.extend(list((schema.get("properties") or {}).keys()))
    return names
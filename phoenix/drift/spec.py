"""Adaptation specs (CONTRACTS.md section 6): declarative, validated, applied by code.

A spec is data, never code. The only operations are rename_field, scale_value,
add_field and remove_field. `validate_spec` rejects anything else, and every
field a spec touches must come from the error details or the request schema.
`apply_spec` is pure: it returns a new request body and never does I/O.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
)
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

MAX_OPS = 10
FACTOR_MIN = Decimal("0.0001")
FACTOR_MAX = Decimal(10000)
MAX_CONSTANT_LEN = 64
DECIMAL_STRING_RE = re.compile(r"^-?\d+(\.\d+)?$")
ROUNDING = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN, "down": ROUND_DOWN}
ValueType = Literal["decimal_string", "integer", "number"]


class SpecError(ValueError):
    """The spec is malformed, unsafe, or cannot be applied to this request."""


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenameField(_Op):
    op: Literal["rename_field"]
    from_: str = Field(alias="from")
    to: str


class ScaleValue(_Op):
    op: Literal["scale_value"]
    field: str
    factor: StrictInt | StrictFloat | StrictStr
    from_type: ValueType
    to_type: ValueType
    rounding: Literal["half_up", "half_even", "down"] = "half_up"


class ValueSource(_Op):
    source: Literal["constant", "task_field"]
    value: str | int | bool


class AddField(_Op):
    op: Literal["add_field"]
    field: str
    value: ValueSource


class RemoveField(_Op):
    op: Literal["remove_field"]
    field: str


Op = Annotated[
    RenameField | ScaleValue | AddField | RemoveField, Field(discriminator="op")
]


class AdaptationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_version: Literal[1]
    system: str
    operation: str
    ops: list[Op] = Field(min_length=1, max_length=MAX_OPS)


@dataclass
class SpecContext:
    """What a spec may legitimately touch."""

    request: dict[str, Any]
    details: list[dict[str, str]]
    task_fields: set[str] = field(default_factory=set)

    @property
    def detail_fields(self) -> set[str]:
        return {d["field"] for d in self.details if "field" in d}


def parse_factor(raw: float | str) -> Decimal:
    if isinstance(raw, bool):
        raise SpecError("factor must be a number")
    try:
        factor = Decimal(str(raw))
    except InvalidOperation as exc:
        raise SpecError(f"factor {raw!r} is not numeric") from exc
    if not factor.is_finite() or math.isnan(float(factor)):
        raise SpecError(f"factor {raw!r} is not finite")
    if not FACTOR_MIN <= factor <= FACTOR_MAX:
        raise SpecError(f"factor {raw!r} is outside {FACTOR_MIN}..{FACTOR_MAX}")
    return factor


def validate_spec(raw: dict[str, Any], ctx: SpecContext) -> AdaptationSpec:
    """Parse and check a proposed spec against the request and error details."""
    try:
        spec = AdaptationSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecError(
            f"spec is not a valid adaptation spec: {exc.errors()[0]['msg']}"
        ) from exc

    fields = set(ctx.request)
    allowed = fields | ctx.detail_fields
    for op in spec.ops:
        if isinstance(op, RenameField):
            if op.from_ not in fields:
                raise SpecError(f"rename_field: {op.from_!r} is not in the request")
            if op.to not in ctx.detail_fields:
                raise SpecError(
                    f"rename_field: {op.to!r} does not appear in the error details"
                )
            if op.to in fields:
                raise SpecError(f"rename_field: {op.to!r} already exists")
            fields.discard(op.from_)
            fields.add(op.to)
        elif isinstance(op, ScaleValue):
            parse_factor(op.factor)
            if op.field not in fields:
                raise SpecError(f"scale_value: {op.field!r} is not in the request")
        elif isinstance(op, AddField):
            if op.field not in ctx.detail_fields:
                raise SpecError(
                    f"add_field: {op.field!r} does not appear in the error details"
                )
            if op.field in fields:
                raise SpecError(f"add_field: {op.field!r} already exists")
            _check_value_source(op.value, ctx)
            fields.add(op.field)
        elif isinstance(op, RemoveField):
            if op.field not in ctx.detail_fields or op.field not in fields:
                raise SpecError(f"remove_field: {op.field!r} is not a flagged field")
            fields.discard(op.field)
        if not fields <= allowed | {
            op.to for op in spec.ops if isinstance(op, RenameField)
        }:
            raise SpecError(
                "spec touches a field outside the error details and request"
            )
    return spec


def _check_value_source(source: ValueSource, ctx: SpecContext) -> None:
    if source.source == "task_field":
        if not isinstance(source.value, str) or source.value not in ctx.task_fields:
            raise SpecError(f"add_field: task field {source.value!r} does not exist")
    elif isinstance(source.value, str) and len(source.value) > MAX_CONSTANT_LEN:
        raise SpecError("add_field: constant is too long")


def _to_decimal(value: Any, type_: ValueType) -> Decimal:
    if type_ == "decimal_string":
        if not isinstance(value, str) or not DECIMAL_STRING_RE.match(value):
            raise SpecError(f"value {value!r} is not a decimal string")
        return Decimal(value)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SpecError(f"value {value!r} is not a number")
    if type_ == "integer" and not isinstance(value, int):
        raise SpecError(f"value {value!r} is not an integer")
    return Decimal(str(value))


def _from_decimal(value: Decimal, type_: ValueType, rounding: str) -> Any:
    if type_ == "integer":
        return int(value.quantize(Decimal(1), rounding=ROUNDING[rounding]))
    if type_ == "decimal_string":
        return str(value.quantize(Decimal("0.01"), rounding=ROUNDING[rounding]))
    return float(value)


def apply_spec(
    spec: AdaptationSpec, request: dict[str, Any], task_values: dict[str, Any]
) -> dict[str, Any]:
    """Return a new request body with every op applied in order. Pure."""
    out = copy.deepcopy(request)
    for op in spec.ops:
        if isinstance(op, RenameField):
            if op.from_ not in out:
                raise SpecError(f"rename_field: {op.from_!r} missing at apply time")
            out[op.to] = out.pop(op.from_)
        elif isinstance(op, ScaleValue):
            if op.field not in out:
                raise SpecError(f"scale_value: {op.field!r} missing at apply time")
            scaled = _to_decimal(out[op.field], op.from_type) * parse_factor(op.factor)
            out[op.field] = _from_decimal(scaled, op.to_type, op.rounding)
        elif isinstance(op, AddField):
            if op.value.source == "constant":
                out[op.field] = op.value.value
            else:
                if op.value.value not in task_values:
                    raise SpecError(f"add_field: task field {op.value.value!r} missing")
                out[op.field] = task_values[str(op.value.value)]
        elif isinstance(op, RemoveField):
            out.pop(op.field, None)
    return out


def spec_to_dict(spec: AdaptationSpec) -> dict[str, Any]:
    return spec.model_dump(by_alias=True, mode="json")


def spec_id(spec: AdaptationSpec) -> str:
    """Stable content hash, so identical specs dedupe and compare equal."""
    canonical = json.dumps(spec_to_dict(spec), sort_keys=True, separators=(",", ":"))
    return "spec_" + hashlib.sha256(canonical.encode()).hexdigest()[:12]

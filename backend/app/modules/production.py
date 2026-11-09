"""Pure assignment preconditions; this module performs no authorization or writes."""

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Literal

from app.modules.orders import OrderContractValidationError


@dataclass(frozen=True, slots=True)
class AssignmentCommand:
    work_item_id: int
    expected_version: int
    idempotency_key: str
    catalog_sku_id: int
    reason: str

    def __post_init__(self):
        for value, maximum in (
            (self.work_item_id, 2**63 - 1),
            (self.expected_version, 2**63 - 1),
            (self.catalog_sku_id, 2**31 - 1),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise OrderContractValidationError(
                    "Positive command ID/version required"
                )
        for value in (self.idempotency_key, self.reason):
            if not isinstance(value, str) or not value or value != value.strip():
                raise OrderContractValidationError(
                    "Exact command key and reason required"
                )
            if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                raise OrderContractValidationError(
                    "Assignment text is not PostgreSQL compatible"
                )


class OrderCommandConflict(OrderContractValidationError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    work_item_id: int
    version: int
    catalog_sku_id: int
    required_quantity: int
    planned_quantity: int
    remaining_quantity: int
    source_item_version: int

    def __post_init__(self):
        for value, minimum, maximum in (
            (self.work_item_id, 1, 2**63 - 1),
            (self.version, 2, 2**63 - 1),
            (self.catalog_sku_id, 1, 2**31 - 1),
            (self.required_quantity, 1, 2**31 - 1),
            (self.planned_quantity, 0, 2**31 - 1),
            (self.remaining_quantity, 0, 2**31 - 1),
            (self.source_item_version, 1, 2**63 - 1),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise OrderContractValidationError("Invalid assignment result")
        if self.remaining_quantity != self.required_quantity - self.planned_quantity:
            raise OrderContractValidationError("Invalid assignment result quantities")


def serialize_assignment_result(result: AssignmentResult) -> dict:
    """Seven-field JSONB value; not an HTTP encoding or an authorization receipt."""
    if type(result) is not AssignmentResult:
        raise OrderContractValidationError("Exact assignment result required")
    return asdict(result)


def deserialize_assignment_result(
    payload: dict, *, schema_version: int
) -> AssignmentResult:
    if (
        type(schema_version) is not int
        or schema_version != 1
        or type(payload) is not dict
        or set(payload) != {field.name for field in fields(AssignmentResult)}
    ):
        raise OrderContractValidationError("Invalid assignment result payload")
    return AssignmentResult(**payload)


def serialize_assignment_command(command: AssignmentCommand) -> bytes:
    """Exact v1 receipt bytes from the accepted schema request, not a DB write."""
    if type(command) is not AssignmentCommand:
        raise OrderContractValidationError("Exact assignment command required")
    value = {
        "schema_version": 1,
        "command": {
            field: getattr(command, field)
            for field in (
                "work_item_id",
                "expected_version",
                "idempotency_key",
                "catalog_sku_id",
                "reason",
            )
        },
    }
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def assignment_command_checksum(command: AssignmentCommand) -> str:
    return hashlib.sha256(serialize_assignment_command(command)).hexdigest()


def deserialize_assignment_command(payload: bytes) -> AssignmentCommand:
    try:
        if type(payload) is not bytes:
            raise ValueError
        value = json.loads(payload)
        if type(value) is not dict or set(value) != {"schema_version", "command"}:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if type(value["command"]) is not dict or set(value["command"]) != {
            "work_item_id",
            "expected_version",
            "idempotency_key",
            "catalog_sku_id",
            "reason",
        }:
            raise ValueError
        command = AssignmentCommand(**value["command"])
        if serialize_assignment_command(command) != payload:
            raise ValueError
        return command
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise OrderContractValidationError(
            "Invalid assignment receipt payload"
        ) from None


def validate_assignment_preconditions(
    command: AssignmentCommand,
    current_version: int,
    previous_command: AssignmentCommand | None = None,
) -> Literal["new", "replay"]:
    """Caller must authorize and read scoped idempotency/CAS state in one transaction."""
    if (
        not isinstance(command, AssignmentCommand)
        or type(current_version) is not int
        or current_version < 1
    ):
        raise OrderContractValidationError("Valid command and current version required")
    if previous_command is not None:
        if not isinstance(previous_command, AssignmentCommand):
            raise OrderContractValidationError("Stored command required")
        if (
            command.idempotency_key != previous_command.idempotency_key
            or command.work_item_id != previous_command.work_item_id
        ):
            raise OrderContractValidationError(
                "Stored command outside idempotency target scope"
            )
        if command == previous_command:
            return "replay"
        raise OrderCommandConflict("IDEMPOTENCY_CONFLICT")
    if command.expected_version != current_version:
        raise OrderCommandConflict("VERSION_CONFLICT")
    return "new"

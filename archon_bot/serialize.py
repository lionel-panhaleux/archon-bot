import enum
import hikari.internal.enums
import msgspec
from typing import Any, Type, TypeVar, Union


T = TypeVar("T")


def enc_hook(obj: Any) -> Any:
    """Handle types we use (enums, hikari.Snowflakes, krcg.seating.Round, etc.)"""
    if isinstance(obj, (enum.Enum, hikari.internal.enums.Enum)):
        return obj.value
    if isinstance(obj, list):
        return list(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, str):
        return str(obj)
    else:
        raise TypeError(f"Objects of type {type(obj)} are not supported")


def dec_hook(type: Type, obj: Any) -> Any:
    """Handle types we use (enums, hikari.Snowflakes, krcg.seating.Round, etc.)"""
    if issubclass(type, (list, int, str)):
        return type(obj)
    else:
        raise TypeError(f"Objects of type {type} are not supported")


def loadd(data: Union[dict[str, Any], list], type: Type[T]) -> T:
    """Load basic data into a structured type."""
    return msgspec.from_builtins(data, type=type, dec_hook=dec_hook, str_keys=True)


def dumpd(data: Any) -> Union[dict[str, Any], list]:
    """Marshall anything into a JSON-compatible value (typically a dict)."""
    return msgspec.to_builtins(data, enc_hook=enc_hook, str_keys=True)

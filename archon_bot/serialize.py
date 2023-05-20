import dataclasses
from collections import abc
from typing import Any, Type, TypeVar, Mapping, Iterable


def is_dataclass_instance(obj):
    return dataclasses.is_dataclass(obj) and not isinstance(obj, type)


T = TypeVar("T")


def loadd(data: Any, cls: Type[T]) -> T:
    """Load basic data (typically a dict) into a structured type.

    Useful to parse a standard structure (like a dict or list from a JSON load)
    into a well-type structure, using dataclasses and type hints for in-depth typing.
    No validation, use pydantic if you want something smarter.
    """
    # handle generic aliases, eg. dict[str, int] or list[float]
    if hasattr(cls, "__args__"):
        origin = getattr(cls, "__origin__", None)
        args = cls.__args__
        # handle old-school Dict, List and Tuple typehints
        origin = getattr(origin, "__origin__", None) or origin
        if issubclass(origin, Mapping):
            # allow non-string keys (enums, ints, etc.)
            return cls(
                {
                    loadd(key, args[0]): loadd(value, args[1])
                    for key, value in data.items()
                }
            )
        if issubclass(origin, Iterable):
            return cls([loadd(value, args[0]) for value in data])
    # fully use dataclasses type hints
    if dataclasses.is_dataclass(cls):
        kwargs = {}
        for field in dataclasses.fields(cls):
            if field.name not in data:
                continue
            kwargs[field.name] = loadd(data[field.name], field.type)
        return cls(**kwargs)
    return cls(data)


def dumpd(data: Any) -> Any:
    """Marshall anything into a JSON-compatible value (typically a dict)"""
    if isinstance(data, Mapping):
        # JSON-compatibility requires keys to be str
        return {str(key): dumpd(value) for key, value in data.items()}
    if isinstance(data, Iterable) and not isinstance(data, (str, abc.ByteString)):
        return [dumpd(value) for value in data]
    if dataclasses.is_dataclass(data):
        return {
            field.name: dumpd(getattr(data, field.name))
            for field in dataclasses.fields(data)
        }
    # all other cases (dates, numbers, uuids) handled by orjson
    return data

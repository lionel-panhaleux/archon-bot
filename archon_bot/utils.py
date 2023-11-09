"""Useful generic tools
"""
import logging
from dataclasses import is_dataclass
from typing import get_args, get_origin, Union, TypeVar

# TODO: remove conditional on upgrade
# python 3.9 backward compatibility
try:
    from types import UnionType
except ImportError:
    from typing import Union as UnionType


logger = logging.getLogger()

Dataclass = TypeVar("Dataclass")


def dictas(cls: Dataclass, dic: dict) -> Dataclass:
    """Provides an type-enforcing reverse operation to datasets `asdict` function.

    Note: Unions will match native JSON types (str, int, float, None) by default,
    and otherwise try all possible types in natural order (starting from the left).
    Note: Optional[X] Union[X, NoneType]
    """

    def instantitate_type(typ, val):
        origin = get_origin(typ)
        if origin is None:
            if is_dataclass(typ):
                return dictas(typ, val)
            elif typ is type(None):
                return None
            elif val is None:
                return typ()
            else:
                return typ(val)
        elif origin is list:
            cls = get_args(typ)[0]
            return [instantitate_type(cls, v) for v in val]
        elif origin is dict:
            k_cls, v_cls = get_args(typ)
            return {
                instantitate_type(k_cls, k): instantitate_type(v_cls, v)
                for k, v in val.items()
            }
        elif origin in [Union, UnionType]:
            options = get_args(typ)
            if type(val) in [o for o in options if o not in {dict, list}]:
                return val
            options = [o for o in options if o not in {str, type(None)}]
            while options:
                try:
                    return instantitate_type(options.pop(0), val)
                except ValueError:
                    pass
        else:
            logger.warning("Unhandled type %s", typ)
            return val

    return cls(
        **{
            name: instantitate_type(field_type, dic[name])
            for name, field_type in cls.__annotations__.items()
            if name in dic
        }
    )

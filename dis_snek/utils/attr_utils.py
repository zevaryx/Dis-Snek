import logging
from functools import partial
from typing import Any, Dict

import attr
from dis_snek.const import logger_name, MISSING

log = logging.getLogger(logger_name)


class_defaults = dict(
    eq=False,
    order=False,
    hash=False,
    slots=True,
    kw_only=True,
    on_setattr=[attr.setters.convert, attr.setters.validate],
)
field_defaults = dict(repr=False)


define = partial(attr.define, **class_defaults)  # type: ignore


def field(*, docs=None, no_export=False, **kwargs):  # type: ignore
    metadata = kwargs.get("metadata", dict())
    if docs is not None:
        metadata["docs"] = docs
    if no_export:
        metadata["no_export"] = True

    if metadata:
        kwargs["metadata"] = metadata

    final_kwargs = field_defaults | kwargs

    return attr.field(**final_kwargs)


def copy_converter(value):
    if isinstance(value, (list, set)):
        return value.copy()
    return value


def docs(doc_string: str) -> Dict[str, str]:
    """Makes it easier to quickly type attr documentation."""
    return {"docs": doc_string}


# def converter(attribute):
#     def decorator(func):
#         attribute.converter = func
#         return staticmethod(func)
#
#     return decorator


def str_validator(self, attribute: attr.Attribute, value: Any):
    if not isinstance(value, str):
        if value is MISSING:
            return
        setattr(self, attribute.name, str(value))
        log.warning(
            f"Value of {attribute.name} has been automatically converted to a string. Please use strings in future.\n"
            "Note: Discord will always return value as a string"
        )

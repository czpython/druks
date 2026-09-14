import importlib
import inspect
from pathlib import Path

import druks
import pytest
from dbos._serialization import DefaultSerializer, deserialize_exception, serialize_exception


def _exception_classes() -> list[type[Exception]]:
    root = Path(druks.__file__).parent
    classes = []
    for path in sorted(root.rglob("exceptions.py")):
        module_name = ".".join(("druks", *path.relative_to(root).with_suffix("").parts))
        module = importlib.import_module(module_name)
        classes += [
            value
            for value in vars(module).values()
            if inspect.isclass(value)
            and issubclass(value, Exception)
            and value.__module__ == module_name
        ]
    return classes


def _placeholder(parameter: inspect.Parameter) -> object:
    annotation = str(parameter.annotation)
    if "int" in annotation:
        return 7
    if "tuple" in annotation:
        return ("a",)
    if "dict" in annotation:
        return {"k": 1}
    return parameter.name


def _instance(cls: type[Exception]) -> Exception:
    args: list[object] = []
    kwargs: dict[str, object] = {}
    for parameter in list(inspect.signature(cls.__init__).parameters.values())[1:]:
        if parameter.kind is parameter.KEYWORD_ONLY:
            kwargs[parameter.name] = _placeholder(parameter)
        elif parameter.kind is not parameter.VAR_KEYWORD:
            args.append(_placeholder(parameter))
    return cls(*args, **kwargs)


@pytest.mark.parametrize("cls", _exception_classes(), ids=lambda cls: cls.__name__)
def test_exception_survives_dbos_step_serialization(cls: type[Exception]) -> None:
    error = _instance(cls)
    serializer = DefaultSerializer()
    payload, serialization = serialize_exception(error, None, serializer)
    replayed = deserialize_exception(payload, serialization, serializer)
    assert type(replayed) is cls
    assert str(replayed) == str(error)
    assert vars(replayed) == vars(error)

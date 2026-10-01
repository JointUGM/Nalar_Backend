import contextlib
import importlib
from types import ModuleType

import pytest
from pydantic import BaseModel

from tests.contract.forbidden import forbidden_keys, schema_property_names


def _modules() -> list[ModuleType]:
    modules = [importlib.import_module("nalar.presentation.api.schemas.student")]
    with contextlib.suppress(ModuleNotFoundError):
        modules.append(importlib.import_module("nalar.presentation.api.schemas.parent"))
    return modules


MODELS = [
    model
    for module in _modules()
    for model in vars(module).values()
    if isinstance(model, type)
    and issubclass(model, BaseModel)
    and model.__module__ == module.__name__
]


def test_the_student_models_were_found() -> None:
    assert {
        "JoinOut",
        "StateOut",
        "ReflectionOut",
        "MissionCardOut",
        "ParentProgressOut",
        "ParentReflectionOut",
    } <= {m.__name__ for m in MODELS}


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_student_and_parent_models_never_carry_hidden_fields(model: type[BaseModel]) -> None:
    assert not forbidden_keys(schema_property_names(model.model_json_schema()))


def test_the_checker_catches_nested_and_fragment_keys() -> None:
    assert forbidden_keys(["move", "final_level", "ai_level", "score_id", "prompt", "scored"]) == {
        "move",
        "final_level",
        "ai_level",
        "score_id",
    }

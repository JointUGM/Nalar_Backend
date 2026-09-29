import pytest
from pydantic import BaseModel

from nalar.application.ports import ai_contract
from tests.contract.forbidden import schema_property_names

PII_FIELDS = {
    "nisn",
    "email",
    "full_name",
    "display_name",
    "student_name",
    "student_id",
    "user_id",
    "parent_id",
    "phone",
}

REQUEST_MODELS = [
    model
    for name, model in vars(ai_contract).items()
    if isinstance(model, type) and issubclass(model, BaseModel) and name.endswith("In")
]


def test_request_models_were_found() -> None:
    names = {model.__name__ for model in REQUEST_MODELS}
    assert {"NextTurnIn", "EvaluateIn", "WarmIn", "EmbedIn"} <= names


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda model: model.__name__)
def test_ai_requests_cannot_carry_personal_data(model: type[BaseModel]) -> None:
    assert not PII_FIELDS & set(schema_property_names(model.model_json_schema()))

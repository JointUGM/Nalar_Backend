import hashlib
import json
from dataclasses import asdict

from nalar.application.ports.administration import NewAcademicYear, NewCurriculum, NewSchool


def request_digest(details: NewAcademicYear | NewCurriculum | NewSchool | str) -> str:
    payload = details if isinstance(details, str) else asdict(details)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

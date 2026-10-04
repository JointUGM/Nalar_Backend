from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from nalar.presentation.api.schemas.common import Body

type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
type Role = Literal["school_admin", "teacher", "student", "parent"]


class LinkedPersonOut(BaseModel):
    user_id: UUID
    full_name: str


class PersonOut(BaseModel):
    user_id: UUID
    full_name: str
    role: Role
    roles: list[Role]
    nisn: str | None
    email: str | None
    class_name: str | None
    account_state: Literal["active", "inactive", "pending_activation"]
    linked_parents: list[LinkedPersonOut]
    linked_children: list[LinkedPersonOut]


class PeoplePageOut(BaseModel):
    items: list[PersonOut]
    next_cursor: UUID | None
    total: int


class PersonEditIn(Body):
    full_name: Name | None = None
    class_id: UUID | None = None

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if self.full_name is None and self.class_id is None:
            raise ValueError("Supply full_name or class_id")
        return self


class ClassIn(Body):
    name: Name
    grade_level: int = Field(ge=1, le=12)
    academic_year_id: UUID
    homeroom_teacher_id: UUID | None = None


class StudentPlacementIn(Body):
    user_ids: list[UUID] = Field(min_length=1, max_length=500)


class ParentLinkIn(Body):
    relationship: Literal["ayah", "ibu", "wali"] | None = None


class PersonCreateIn(Body):
    full_name: Name
    role: Literal["teacher", "student", "parent"]
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    nisn: str | None = Field(default=None, pattern=r"^\d{10}$")
    class_id: UUID | None = None
    child_ids: list[UUID] = Field(default_factory=list, max_length=100)
    relationship: Literal["ayah", "ibu", "wali"] | None = None
    invite: bool = False

    @model_validator(mode="after")
    def role_fields(self) -> Self:
        if self.email is not None:
            self.email = self.email.strip().lower()
        if self.role == "student":
            if self.nisn is None or self.class_id is None or self.child_ids:
                raise ValueError("A student requires nisn and class_id")
        elif self.email is None or self.nisn is not None or self.class_id is not None:
            raise ValueError("A teacher or parent requires email")
        if self.role == "parent" and not self.child_ids:
            raise ValueError("A parent requires at least one child in this school")
        if self.role != "parent" and (self.child_ids or self.relationship is not None):
            raise ValueError("Child links belong to parents")
        if self.invite and self.email is None:
            raise ValueError("An invitation requires a real email")
        return self


class PersonCreatedOut(BaseModel):
    user_id: UUID


class ClassPatchIn(Body):
    name: Name | None = None
    grade_level: int | None = Field(None, ge=1, le=12)
    academic_year_id: UUID | None = None
    homeroom_teacher_id: UUID | None = None

    @model_validator(mode="after")
    def valid_changes(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set - {"homeroom_teacher_id"}
        ):
            raise ValueError("Supply non-null class fields")
        return self


class ClassOut(BaseModel):
    class_id: UUID
    name: str
    grade_level: int
    academic_year_id: UUID
    homeroom_teacher_id: UUID | None
    student_count: int
    archived_at: datetime | None


class SubjectKnowledgeBaseOut(BaseModel):
    knowledge_base_id: UUID
    topic_title: str
    owner_teacher_id: UUID
    owner_name: str | None
    status: str


class SubjectOut(BaseModel):
    school_subject_id: UUID
    name: str
    cp_version_id: UUID | None
    cp_subject_id: UUID | None
    kb_owner_name: str | None
    knowledge_bases: list[SubjectKnowledgeBaseOut]


class CurriculumMappingIn(Body):
    cp_version_id: UUID
    cp_subject_id: UUID | None = None


class AssignmentIn(Body):
    class_id: UUID
    school_subject_id: UUID
    teacher_id: UUID | None


class SchoolAssignmentOut(AssignmentIn):
    academic_year_id: UUID
    class_name: str
    subject_name: str
    teacher_name: str


class CurriculumSubjectChoiceOut(BaseModel):
    id: UUID
    name: str
    phase: str


class SchoolCurriculumVersionOut(BaseModel):
    id: UUID
    name: str
    decree_code: str
    effective_on: date
    published_at: datetime | None
    is_current: bool
    subjects: list[CurriculumSubjectChoiceOut]


class KbOwnerIn(Body):
    teacher_id: UUID


class AcademicYearIn(Body):
    name: Name
    starts_on: date
    ends_on: date
    copy_classes_from: UUID | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.ends_on <= self.starts_on:
            raise ValueError("ends_on must follow starts_on")
        return self


class AcademicYearCreatedOut(BaseModel):
    academic_year_id: UUID


class AdminEmailIn(Body):
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        local, sep, domain = value.partition("@")
        if (
            not sep
            or not local
            or "." not in domain
            or "@" in domain
            or any(c.isspace() for c in value)
        ):
            raise ValueError("Invalid email")
        return value.lower()


class SchoolIn(Body):
    name: Name
    npsn: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{8}$")]
    city: Name
    admin_email: str

    @field_validator("admin_email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return AdminEmailIn(email=value).email


class SchoolStatusIn(Body):
    status: Literal["active", "suspended"]


class SchoolOut(BaseModel):
    id: UUID
    name: str
    npsn: str | None
    city: str | None
    status: Literal["active", "suspended"]
    admin_name: str | None
    user_count: int


class SchoolsPageOut(BaseModel):
    items: list[SchoolOut]
    next_cursor: UUID | None
    total: int
    counts: dict[str, int]


class SchoolCreatedOut(BaseModel):
    school_id: UUID
    pending_activation: bool


class SchoolAdminOut(BaseModel):
    user_id: UUID
    pending_activation: bool


class LearningOutcomeIn(Body):
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)
    ]
    element: Name | None = None
    ordinal: int = Field(0, ge=0)


class CurriculumSubjectIn(Body):
    name: Name
    phase: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-F]$")]
    learning_outcomes: list[LearningOutcomeIn] = Field(min_length=1, max_length=1000)


class CurriculumIn(Body):
    name: Name
    decree_code: Name
    effective_on: date
    subjects: list[CurriculumSubjectIn] = Field(min_length=1, max_length=100)
    is_current: bool = True


class CurriculumVersionOut(BaseModel):
    id: UUID
    name: str
    decree_code: str
    effective_on: date
    published_at: datetime | None
    school_count: int
    is_current: bool
    status: Literal["draft", "published", "superseded"]


class LearningOutcomeOut(BaseModel):
    id: UUID
    description: str
    element: str | None
    ordinal: int


class CurriculumSubjectOut(BaseModel):
    id: UUID
    name: str
    phase: str
    learning_outcomes: list[LearningOutcomeOut]


class CurriculumDetailOut(CurriculumVersionOut):
    subjects: list[CurriculumSubjectOut]


class CurriculumCreatedOut(BaseModel):
    curriculum_version_id: UUID

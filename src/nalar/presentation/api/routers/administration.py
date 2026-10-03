from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Header, Query, Response

from nalar.application.features.administration.commands.assign_teacher import AssignTeacherHandler
from nalar.application.features.administration.commands.create_academic_year import (
    CreateAcademicYearHandler,
)
from nalar.application.features.administration.commands.deactivate_person import (
    DeactivatePersonHandler,
)
from nalar.application.features.administration.commands.edit_person import EditPersonHandler
from nalar.application.features.administration.commands.install_school_admin import (
    InstallSchoolAdminHandler,
)
from nalar.application.features.administration.commands.publish_curriculum import (
    PublishCurriculumHandler,
)
from nalar.application.features.administration.commands.save_class import SaveClassHandler
from nalar.application.features.administration.commands.set_curriculum import SetCurriculumHandler
from nalar.application.features.administration.commands.set_school_status import (
    SetSchoolStatusHandler,
)
from nalar.application.features.administration.commands.transfer_kb import TransferKbHandler
from nalar.application.features.administration.queries.get_curriculum_version import (
    GetCurriculumVersionQuery,
)
from nalar.application.features.administration.queries.list_assignments import ListAssignmentsQuery
from nalar.application.features.administration.queries.list_classes import ListClassesQuery
from nalar.application.features.administration.queries.list_curriculum_versions import (
    ListCurriculumVersionsQuery,
)
from nalar.application.features.administration.queries.list_people import ListPeopleQuery
from nalar.application.features.administration.queries.list_schools import ListSchoolsQuery
from nalar.application.features.administration.queries.list_subjects import ListSubjectsQuery
from nalar.application.ports.administration import (
    CurriculumSubject,
    LearningOutcome,
    NewAcademicYear,
    NewCurriculum,
    NewSchool,
)
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.administration import (
    AcademicYearCreatedOut,
    AcademicYearIn,
    AdminEmailIn,
    AssignmentIn,
    AssignmentOut,
    ClassIn,
    ClassOut,
    ClassPatchIn,
    CurriculumCreatedOut,
    CurriculumDetailOut,
    CurriculumIn,
    CurriculumMappingIn,
    CurriculumVersionOut,
    KbOwnerIn,
    PeoplePageOut,
    PersonEditIn,
    Role,
    SchoolAdminOut,
    SchoolCreatedOut,
    SchoolIn,
    SchoolsPageOut,
    SchoolStatusIn,
    SubjectOut,
)

router = APIRouter(tags=["administration"], route_class=DishkaRoute)
type RequestKey = Annotated[UUID, Header(alias="Idempotency-Key")]
type PageLimit = Annotated[int, Query(ge=1, le=100)]
type Search = Annotated[str, Query(max_length=200)]


@router.get("/schools/{school_id}/people", response_model=PeoplePageOut)
async def people(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListPeopleQuery],
    role: Role | None = None,
    q: Search = "",
    cursor: UUID | None = None,
    limit: PageLimit = 50,
) -> PeoplePageOut:
    return PeoplePageOut.model_validate(
        asdict(await query.execute(user.id, school_id, role, q, cursor, limit))
    )


@router.patch("/schools/{school_id}/people/{user_id}", status_code=204)
async def edit_person(
    school_id: UUID,
    user_id: UUID,
    body: PersonEditIn,
    user: CurrentUser,
    handler: FromDishka[EditPersonHandler],
) -> Response:
    await handler.execute(user.id, school_id, user_id, body.full_name, body.class_id)
    return Response(status_code=204)


@router.post("/schools/{school_id}/people/{user_id}/deactivate", status_code=204)
async def deactivate_person(
    school_id: UUID, user_id: UUID, user: CurrentUser, handler: FromDishka[DeactivatePersonHandler]
) -> Response:
    await handler.execute(user.id, school_id, user_id)
    return Response(status_code=204)


@router.get("/schools/{school_id}/classes", response_model=list[ClassOut])
async def classes(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListClassesQuery],
    academic_year_id: UUID | None = None,
) -> list[ClassOut]:
    return [
        ClassOut.model_validate(row)
        for row in await query.execute(user.id, school_id, academic_year_id)
    ]


@router.post("/schools/{school_id}/classes", status_code=201, response_model=ClassOut)
async def create_class(
    school_id: UUID, body: ClassIn, user: CurrentUser, handler: FromDishka[SaveClassHandler]
) -> ClassOut:
    return ClassOut.model_validate(await handler.execute(user.id, school_id, body.model_dump()))


@router.patch("/schools/{school_id}/classes/{class_id}", response_model=ClassOut)
async def edit_class(
    school_id: UUID,
    class_id: UUID,
    body: ClassPatchIn,
    user: CurrentUser,
    handler: FromDishka[SaveClassHandler],
) -> ClassOut:
    return ClassOut.model_validate(
        await handler.execute(user.id, school_id, body.model_dump(exclude_unset=True), class_id)
    )


@router.get("/schools/{school_id}/subjects", response_model=list[SubjectOut])
async def subjects(
    school_id: UUID, user: CurrentUser, query: FromDishka[ListSubjectsQuery]
) -> list[SubjectOut]:
    return [SubjectOut.model_validate(row) for row in await query.execute(user.id, school_id)]


@router.put("/schools/{school_id}/subjects/{subject_id}/curriculum", status_code=204)
async def set_curriculum(
    school_id: UUID,
    subject_id: UUID,
    body: CurriculumMappingIn,
    user: CurrentUser,
    handler: FromDishka[SetCurriculumHandler],
) -> Response:
    await handler.execute(user.id, school_id, subject_id, body.cp_version_id, body.cp_subject_id)
    return Response(status_code=204)


@router.get("/schools/{school_id}/assignments", response_model=list[AssignmentOut])
async def assignments(
    school_id: UUID,
    user: CurrentUser,
    query: FromDishka[ListAssignmentsQuery],
    academic_year_id: UUID | None = None,
) -> list[AssignmentOut]:
    return [
        AssignmentOut.model_validate(row)
        for row in await query.execute(user.id, school_id, academic_year_id)
    ]


@router.put("/schools/{school_id}/assignments", status_code=204)
async def assign_teacher(
    school_id: UUID,
    body: AssignmentIn,
    user: CurrentUser,
    handler: FromDishka[AssignTeacherHandler],
) -> Response:
    await handler.execute(
        user.id, school_id, body.class_id, body.school_subject_id, body.teacher_id
    )
    return Response(status_code=204)


@router.post("/knowledge-bases/{kb_id}/owner", status_code=204)
async def transfer_kb(
    kb_id: UUID, body: KbOwnerIn, user: CurrentUser, handler: FromDishka[TransferKbHandler]
) -> Response:
    await handler.execute(user.id, kb_id, body.teacher_id)
    return Response(status_code=204)


@router.post(
    "/schools/{school_id}/academic-years", status_code=201, response_model=AcademicYearCreatedOut
)
async def create_year(
    school_id: UUID,
    body: AcademicYearIn,
    key: RequestKey,
    user: CurrentUser,
    handler: FromDishka[CreateAcademicYearHandler],
) -> AcademicYearCreatedOut:
    result = await handler.execute(user.id, school_id, NewAcademicYear(**body.model_dump()), key)
    return AcademicYearCreatedOut(academic_year_id=result)


@router.get("/platform/schools", response_model=SchoolsPageOut)
async def schools(
    user: CurrentUser,
    query: FromDishka[ListSchoolsQuery],
    q: Search = "",
    cursor: UUID | None = None,
    limit: PageLimit = 50,
) -> SchoolsPageOut:
    return SchoolsPageOut.model_validate(asdict(await query.execute(user.id, q, cursor, limit)))


@router.post("/platform/schools", status_code=201, response_model=SchoolCreatedOut)
async def create_school(
    body: SchoolIn,
    key: RequestKey,
    user: CurrentUser,
    handler: FromDishka[InstallSchoolAdminHandler],
) -> SchoolCreatedOut:
    result = await handler.execute(user.id, key, NewSchool(**body.model_dump()))
    return SchoolCreatedOut(
        school_id=result.result_id, pending_activation=result.pending_activation
    )


@router.patch("/platform/schools/{school_id}/status", status_code=204)
async def school_status(
    school_id: UUID,
    body: SchoolStatusIn,
    user: CurrentUser,
    handler: FromDishka[SetSchoolStatusHandler],
) -> Response:
    await handler.execute(user.id, school_id, body.status == "active")
    return Response(status_code=204)


@router.post("/platform/schools/{school_id}/admin", response_model=SchoolAdminOut)
async def school_admin(
    school_id: UUID,
    body: AdminEmailIn,
    key: RequestKey,
    user: CurrentUser,
    handler: FromDishka[InstallSchoolAdminHandler],
) -> SchoolAdminOut:
    result = await handler.execute(user.id, key, body.email, school_id)
    return SchoolAdminOut(user_id=result.result_id, pending_activation=result.pending_activation)


@router.get("/platform/curriculum-versions", response_model=list[CurriculumVersionOut])
async def curriculum_versions(
    user: CurrentUser, query: FromDishka[ListCurriculumVersionsQuery]
) -> list[CurriculumVersionOut]:
    return [CurriculumVersionOut.model_validate(row) for row in await query.execute(user.id)]


@router.get("/platform/curriculum-versions/{version_id}", response_model=CurriculumDetailOut)
async def curriculum_version(
    version_id: UUID, user: CurrentUser, query: FromDishka[GetCurriculumVersionQuery]
) -> CurriculumDetailOut:
    return CurriculumDetailOut.model_validate(await query.execute(user.id, version_id))


@router.post("/platform/curriculum-versions", status_code=201, response_model=CurriculumCreatedOut)
async def publish_curriculum(
    body: CurriculumIn,
    key: RequestKey,
    user: CurrentUser,
    handler: FromDishka[PublishCurriculumHandler],
) -> CurriculumCreatedOut:
    details = NewCurriculum(
        body.name,
        body.decree_code,
        body.effective_on,
        [
            CurriculumSubject(
                s.name, s.phase, [LearningOutcome(**o.model_dump()) for o in s.learning_outcomes]
            )
            for s in body.subjects
        ],
        body.is_current,
    )
    result = await handler.execute(user.id, details, key)
    return CurriculumCreatedOut(curriculum_version_id=result)

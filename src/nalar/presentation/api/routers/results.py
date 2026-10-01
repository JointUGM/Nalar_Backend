from dataclasses import asdict
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from nalar.application.features.results.commands.override_score import (
    OverrideScore,
    OverrideScoreHandler,
)
from nalar.application.features.results.commands.review_flag import ReviewFlag, ReviewFlagHandler
from nalar.application.features.results.queries.class_map import ClassMapQuery
from nalar.application.features.results.queries.monitor import MonitorQuery
from nalar.application.features.results.queries.session_report import SessionReportQuery
from nalar.application.features.sessions.commands.safety_action import (
    SafetyAction,
    SafetyActionHandler,
)
from nalar.application.ports.clock import Clock
from nalar.presentation.api.deps import CurrentUser
from nalar.presentation.api.schemas.results import (
    ClassMapOut,
    ConceptCountOut,
    EvidenceOut,
    FlagReviewIn,
    FlagReviewOut,
    InsightOut,
    MisconceptionCountOut,
    MonitorOut,
    MonitorRunOut,
    MonitorStudentOut,
    OverrideIn,
    OverrideOut,
    OverrideResultOut,
    ReportConceptResultOut,
    ReportEvaluationOut,
    ReportFlagOut,
    ReportOut,
    ReportScoreOut,
    ReportSessionOut,
    ReportStudentOut,
    ReportTurnOut,
    SafetyActionIn,
    SafetyActionOut,
)

router = APIRouter(tags=["results"], route_class=DishkaRoute)


@router.get("/publications/{publication_id}/monitor", response_model=MonitorOut)
async def monitor(
    publication_id: UUID,
    user: CurrentUser,
    query: FromDishka[MonitorQuery],
    clock: FromDishka[Clock],
) -> MonitorOut:
    found = await query.execute(user.id, publication_id)
    return MonitorOut(
        run=MonitorRunOut(**asdict(found.run)),
        waiting_count=found.waiting_count,
        students=[
            MonitorStudentOut(**asdict(s), max_turns=found.max_turns) for s in found.students
        ],
        server_now=clock.now(),
    )


@router.get("/publications/{publication_id}/class-map", response_model=ClassMapOut)
async def class_map(
    publication_id: UUID, user: CurrentUser, query: FromDishka[ClassMapQuery]
) -> ClassMapOut:
    data, counts, insight = await query.execute(user.id, publication_id)
    names = dict(data.concepts)
    statements = {m: statement for m, _, statement in data.misconceptions}
    return ClassMapOut(
        denominator=counts.denominator,
        incomplete_count=counts.incomplete_count,
        insight=InsightOut(narrative=insight.narrative, generated_at=insight.generated_at)
        if insight
        else None,
        concepts=[
            ConceptCountOut(
                concept_id=c.concept_id,
                name=names[c.concept_id],
                mastered_count=c.mastered_count,
                developing_count=c.developing_count,
                not_observed_count=c.not_observed_count,
                misconceptions=[
                    MisconceptionCountOut(
                        misconception_id=m.misconception_id,
                        statement=statements[m.misconception_id],
                        count=m.count,
                        resolved_count=m.resolved_count,
                        student_ids=list(m.student_ids),
                    )
                    for m in c.misconceptions
                ],
            )
            for c in counts.concepts
        ],
    )


@router.get("/sessions/{session_id}/report", response_model=ReportOut)
async def report(
    session_id: UUID, user: CurrentUser, query: FromDishka[SessionReportQuery]
) -> ReportOut:
    r = await query.execute(user.id, session_id)
    return ReportOut(
        student=ReportStudentOut(id=r.student_id, name=r.student_name),
        session=ReportSessionOut(
            status=r.status,
            end_reason=r.end_reason,
            started_at=r.started_at,
            ended_at=r.ended_at,
            attempt_number=r.attempt_number,
        ),
        evaluation=ReportEvaluationOut(status=r.evaluation_status, summary=r.evaluation_summary)
        if r.evaluation_status
        else None,
        turns=[ReportTurnOut(**asdict(t)) for t in r.turns],
        scores=[
            ReportScoreOut(
                score_id=s.score_id,
                dimension=s.dimension,
                ai_level=s.ai_level,
                final_level=s.final_level,
                rationale=s.rationale,
                evidence=[EvidenceOut(turn_id=t, quote=q) for t, q in s.evidence],
                overrides=[OverrideOut(**asdict(o)) for o in s.overrides],
            )
            for s in r.scores
        ],
        concept_results=[ReportConceptResultOut(**asdict(c)) for c in r.concept_results],
        flags=[ReportFlagOut(**asdict(f)) for f in r.flags],
    )


@router.post("/sessions/{session_id}/safety-actions", response_model=SafetyActionOut)
async def safety_action(
    session_id: UUID,
    body: SafetyActionIn,
    user: CurrentUser,
    handler: FromDishka[SafetyActionHandler],
) -> SafetyActionOut:
    acted = await handler.execute(SafetyAction(user.id, session_id, body.action, body.note))
    return SafetyActionOut(session_id=session_id, status=acted.status, acted_at=acted.acted_at)


@router.post("/scores/{score_id}/overrides", response_model=OverrideResultOut)
async def override_score(
    score_id: UUID, body: OverrideIn, user: CurrentUser, handler: FromDishka[OverrideScoreHandler]
) -> OverrideResultOut:
    done = await handler.execute(OverrideScore(user.id, score_id, body.final_level, body.reason))
    return OverrideResultOut(**asdict(done))


@router.post("/flags/{flag_id}/review", response_model=FlagReviewOut)
async def review_flag(
    flag_id: UUID, body: FlagReviewIn, user: CurrentUser, handler: FromDishka[ReviewFlagHandler]
) -> FlagReviewOut:
    done = await handler.execute(ReviewFlag(user.id, flag_id, body.decision, body.note))
    return FlagReviewOut(**asdict(done))

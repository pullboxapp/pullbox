"""Bounded individual file decisions for the guided import review."""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.composition.services import build_metadata_service
from pullbox.services.import_review_file_assignment import assign_review_file, load_review_file
from pullbox.services.import_review_scope import (
    review_scope,
    sign_review_scope,
    verify_review_scope,
)

router = APIRouter()


@router.get("/import/{job_id}/files/{file_id}/source", include_in_schema=False)
async def preview_source_action(
    job_id: int, file_id: int, request: Request, user: InteractiveOperatorUser, session: DbSession
) -> Response:
    from pullbox.services.import_review_source_actions import replacement_candidate
    from pullbox.ui.import_routes import _ctx, _templates

    job, parent, file = await load_review_file(session, job_id, file_id)
    candidate = await replacement_candidate(session, file, parent)
    action = "pair" if candidate is not None else "recheck"
    token = await sign_review_scope(
        session,
        {
            "actor": user.id,
            "action": action,
            "scope": review_scope(job, parent, file),
            "candidate_scope": review_scope(job, parent, candidate) if candidate else None,
        },
    )
    return _templates().TemplateResponse(
        request,
        "partials/import_review_file_action.html",
        _ctx(
            request,
            user,
            job=job,
            file=file,
            candidate=candidate,
            source_action=action,
            token=token,
            action="source",
        ),
    )


@router.post("/import/{job_id}/files/{file_id}/source", include_in_schema=False)
async def apply_source_action(
    job_id: int,
    file_id: int,
    user: InteractiveOperatorUser,
    session: DbSession,
    source_action: Annotated[str, Form()],
    token: Annotated[str, Form()],
) -> Response:
    from pullbox.services.import_review_source_actions import (
        queue_source_action,
        replacement_candidate,
    )
    from pullbox.tasks.import_task import trigger_import_review_source_action

    job, parent, file = await load_review_file(session, job_id, file_id)
    candidate = (
        await replacement_candidate(session, file, parent) if source_action == "pair" else None
    )
    await verify_review_scope(
        session,
        token,
        {
            "actor": user.id,
            "action": source_action,
            "scope": review_scope(job, parent, file),
            "candidate_scope": review_scope(job, parent, candidate) if candidate else None,
        },
    )
    await queue_source_action(session, job_id, file_id, action=source_action, actor_id=user.id)
    await session.commit()
    trigger_import_review_source_action(job_id, file_id)
    return JSONResponse(
        {"message": "Source verification queued. You can keep reviewing other series."}
    )


@router.get("/import/{job_id}/files/{file_id}/assign", include_in_schema=False)
async def preview_file_assignment(
    job_id: int,
    file_id: int,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    cv_id: int = Query(gt=0),
) -> Response:
    from pullbox.ui.import_routes import _ctx, _templates

    job, parent, file = await load_review_file(session, job_id, file_id)
    service = await build_metadata_service(session)
    series = await service.get_series_metadata(cv_id)
    issues = await service.get_issue_summaries_for_series(cv_id)
    token = await sign_review_scope(
        session,
        {
            "actor": user.id,
            "action": "assign",
            "cv_id": cv_id,
            "scope": review_scope(job, parent, file),
        },
    )
    return _templates().TemplateResponse(
        request,
        "partials/import_review_file_action.html",
        _ctx(
            request,
            user,
            job=job,
            file=file,
            series=series,
            issues=issues,
            token=token,
            action="assign",
        ),
    )


@router.post("/import/{job_id}/files/{file_id}/assign", include_in_schema=False)
async def apply_file_assignment(
    job_id: int,
    file_id: int,
    user: InteractiveOperatorUser,
    session: DbSession,
    cv_id: Annotated[int, Form(gt=0)],
    issue_cv_id: Annotated[int, Form(gt=0)],
    token: Annotated[str, Form()],
) -> Response:
    job, parent, file = await load_review_file(session, job_id, file_id)
    await verify_review_scope(
        session,
        token,
        {
            "actor": user.id,
            "action": "assign",
            "cv_id": cv_id,
            "scope": review_scope(job, parent, file),
        },
    )
    await assign_review_file(
        session,
        job_id,
        file_id,
        cv_id=cv_id,
        issue_cv_id=issue_cv_id,
        metadata_service=await build_metadata_service(session),
    )
    await session.commit()
    return JSONResponse(
        {"message": "File assigned. Source files and other issue decisions are unchanged."}
    )

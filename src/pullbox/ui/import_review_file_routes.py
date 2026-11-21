"""Bounded individual file decisions for the guided import review."""

from typing import Annotated, Literal

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from pullbox.api.deps import DbSession, InteractiveOperatorUser, Settings
from pullbox.composition.services import build_metadata_service
from pullbox.services.import_review_file_assignment import (
    assign_review_file,
    load_review_file,
    require_legacy_assignment,
)
from pullbox.services.import_review_scope import (
    review_scope,
    sign_review_scope,
    verify_review_scope,
)

router = APIRouter()


@router.get("/import/{job_id}/series/{series_id}/identity-preview", include_in_schema=False)
@router.get("/import/{job_id}/series/{series_id}/metron-preview", include_in_schema=False)
async def metron_preview_context(
    job_id: int,
    series_id: int,
    user: InteractiveOperatorUser,
    session: DbSession,
    settings: Settings,
) -> Response:
    from fastapi import HTTPException

    from pullbox.core.exceptions import ValidationError
    from pullbox.core.metadata_identity import IdentityNamespace, MetadataSource
    from pullbox.schemas.metadata_sources import SourceCapability
    from pullbox.services.import_metron_matching import native_series_preview_identity
    from pullbox.services.library_root_management import list_library_roots
    from pullbox.services.metadata_discovery import MetadataSourceRegistry
    from pullbox.services.metadata_sources import load_source_runtime

    try:
        job, item, namespace, external_id = await native_series_preview_identity(
            session, job_id, series_id
        )
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    source = MetadataSource.METRON_API
    if namespace is IdentityNamespace.GCD:
        registry = MetadataSourceRegistry(
            await load_source_runtime(
                session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
            ),
            gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
        )
        candidates = [
            candidate
            for candidate in registry.ordered_sources(
                sources=[MetadataSource.GCD_LOCAL, MetadataSource.GCD_API_V2]
            )
            if all(
                registry.source_availability(candidate, capability) is None
                for capability in (SourceCapability.SERIES_DETAILS, SourceCapability.ISSUE_LIST)
            )
            and (
                candidate is MetadataSource.GCD_LOCAL
                or registry.runtime[candidate].policy.credential_configured
            )
        ]
        if not candidates:
            raise HTTPException(
                409,
                "Enable and configure a GCD source in Metadata settings, then retry this preview. "
                "Source files are unchanged.",
            )
        source = candidates[0]
    roots = [
        root
        for root in await list_library_roots(session)
        if all(
            root[key]
            for key in ("enabled", "allow_managed_writes", "available", "readable", "writable")
        )
    ]
    root = next((root for root in roots if root["id"] == job.target_library_root_id), None)
    if job.target_library_root_id is None:
        root = next((root for root in roots if root["is_default_managed_destination"]), None)
    return JSONResponse(
        {
            "source": source.value,
            "external_id": external_id,
            "title": item.raw_series_name,
            "library_root_id": root["id"] if root else None,
            "root_label": f"{root['path']} - {root['name']}" if root else "",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/import/{job_id}/series/{series_id}/identity-recheck", include_in_schema=False)
@router.post("/import/{job_id}/series/{series_id}/metron-recheck", include_in_schema=False)
async def recheck_metron_identity(
    job_id: int,
    series_id: int,
    user: InteractiveOperatorUser,
    session: DbSession,
) -> Response:
    from fastapi import HTTPException

    from pullbox.core.exceptions import ValidationError
    from pullbox.models.import_job import ImportJobLog
    from pullbox.services.import_metron_matching import (
        import_identity_namespace,
        prepare_metron_series_recheck,
    )
    from pullbox.tasks.import_task import trigger_import_series_rematch

    try:
        item = await prepare_metron_series_recheck(session, job_id, series_id)
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    namespace = import_identity_namespace((item.diagnostics or {}).get("metron_import") or {})
    identity_key = namespace.value if namespace else "provider"
    session.add(
        ImportJobLog(
            import_job_id=job_id,
            level="INFO",
            event=f"import_{identity_key}_identity_recheck_requested",
            message="Rechecking saved provider identities against the linked library metadata.",
            data={"imported_series_id": series_id, "actor_user_id": user.id},
        )
    )
    await session.commit()
    trigger_import_series_rematch(job_id, series_id)
    return JSONResponse(
        {"message": "Rechecking linked metadata. Source files are unchanged."}, status_code=202
    )


@router.get("/import/{job_id}/series/{series_id}/files", include_in_schema=False)
async def view_series_files(
    job_id: int,
    series_id: int,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    files_page: int = Query(1, ge=1),
) -> Response:
    from pullbox.ui.import_review_files import load_series_file_inventory
    from pullbox.ui.import_routes import _ctx, _templates

    inventory = await load_series_file_inventory(session, job_id, series_id, files_page)
    return _templates().TemplateResponse(
        request,
        "partials/import_review_files_inventory.html",
        _ctx(request, user, inventory=inventory, job_id=job_id),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/import/{job_id}/series/{series_id}/review-library", include_in_schema=False)
async def preview_library_series(
    job_id: int,
    series_id: int,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    library_series_id: int | None = Query(None, gt=0),
    follow_up: bool = False,
) -> Response:
    from pullbox.services.import_library_selection import (
        library_selection_context,
        library_series_identity,
        library_series_page,
    )
    from pullbox.ui.import_routes import _ctx, _templates

    job, parent, scope = await library_selection_context(
        session, job_id, series_id, user.id, follow_up=follow_up
    )
    series, page, pages = await library_series_page(session, q, page, series_id=library_series_id)
    choices = []
    for item in series:
        identity = await library_series_identity(session, item)
        signed = await sign_review_scope(
            session, {**scope, "target": identity, "title": item.title, "year": item.year_start}
        )
        choices.append({"series": item, "identity": identity, "token": signed})
    return _templates().TemplateResponse(
        request,
        "partials/import_library_series_choices.html",
        _ctx(
            request,
            user,
            job=job,
            series=parent,
            choices=choices,
            query=q,
            page=page,
            pages=pages,
            follow_up=follow_up,
        ),
    )


@router.post("/import/{job_id}/series/{series_id}/review-library", include_in_schema=False)
async def apply_library_series(
    job_id: int,
    series_id: int,
    user: InteractiveOperatorUser,
    session: DbSession,
    library_series_id: Annotated[int, Form(gt=0)],
    token: Annotated[str, Form()],
    follow_up: bool = False,
) -> Response:
    from pullbox.services.import_library_selection import choose_library_series

    await choose_library_series(
        session, job_id, series_id, library_series_id, user.id, token, follow_up=follow_up
    )
    await session.commit()
    return JSONResponse(
        {
            "message": (
                "Library series selected. Choose each file's issue before importing; "
                "no files were selected automatically."
            )
        }
    )


@router.get("/import/{job_id}/series/{series_id}/review-library-search", include_in_schema=False)
async def search_library_series(
    job_id: int,
    series_id: int,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    q: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    source: Annotated[Literal["all", "metron_api", "gcd_local", "gcd_api_v2"], Query()] = "all",
    follow_up: bool = False,
) -> Response:
    from pullbox.core.metadata_identity import MetadataSource
    from pullbox.models.series import Series
    from pullbox.services.import_library_selection import (
        library_selection_context,
        library_series_identity,
    )
    from pullbox.services.library_root_management import list_library_roots
    from pullbox.ui.import_routes import _ctx, _templates
    from pullbox.ui.series_routes import _request_search_cache, load_add_series_search_context

    actor_id = user.id
    await library_selection_context(session, job_id, series_id, actor_id, follow_up=follow_up)
    search = await load_add_series_search_context(
        session,
        q,
        "relevance",
        page,
        source=MetadataSource(source) if source != "all" else None,
        allowed_sources=(
            MetadataSource.METRON_API,
            MetadataSource.GCD_LOCAL,
            MetadataSource.GCD_API_V2,
        ),
        cache=_request_search_cache(request),
    )
    # Provider I/O ends the prior transaction. Revalidate the review before offering choices.
    job, parent, scope = await library_selection_context(
        session, job_id, series_id, actor_id, follow_up=follow_up
    )
    await session.refresh(user)
    rows = search["search_results"]
    if isinstance(rows, list):
        for row in rows:
            if not row["already_added"]:
                continue
            item = await session.get(Series, row["library_series_id"])
            if item is not None:
                identity = await library_series_identity(session, item)
                row["choice_token"] = await sign_review_scope(
                    session,
                    {**scope, "target": identity, "title": item.title, "year": item.year_start},
                )
    roots = [
        root
        for root in await list_library_roots(session)
        if all(
            root[key]
            for key in ("enabled", "allow_managed_writes", "available", "readable", "writable")
        )
    ]
    root = next((root for root in roots if root["id"] == job.target_library_root_id), None)
    if job.target_library_root_id is None:
        root = next((root for root in roots if root["is_default_managed_destination"]), None)
    return _templates().TemplateResponse(
        request,
        "partials/import_library_series_search.html",
        _ctx(
            request,
            user,
            job=job,
            series=parent,
            search=search,
            follow_up=follow_up,
            search_config={
                "jobId": job_id,
                "seriesId": series_id,
                "action": "library",
                "followUp": follow_up,
                "libraryRootId": root["id"] if root else None,
                "rootLabel": f"{root['path']} - {root['name']}" if root else "",
            },
        ),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/import/{job_id}/series/{series_id}/review-{action}", include_in_schema=False)
async def preview_series_choice(
    job_id: int,
    series_id: int,
    action: str,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
) -> Response:
    from fastapi import HTTPException

    from pullbox.core.exceptions import ValidationError
    from pullbox.services.import_review_series_choice import series_choice_scope
    from pullbox.ui.import_routes import _ctx, _templates

    try:
        job, series, scope = await series_choice_scope(session, job_id, series_id, action, user.id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    token = await sign_review_scope(session, scope)
    return _templates().TemplateResponse(
        request,
        "partials/import_review_series_choice.html",
        _ctx(
            request,
            user,
            job=job,
            series=series,
            action=action,
            token=token,
        ),
    )


@router.post("/import/{job_id}/series/{series_id}/review-{action}", include_in_schema=False)
async def decide_series_choice(
    job_id: int,
    series_id: int,
    action: str,
    user: InteractiveOperatorUser,
    session: DbSession,
    token: Annotated[str, Form()],
) -> Response:
    from fastapi import HTTPException

    from pullbox.core.exceptions import ValidationError
    from pullbox.services.import_review_series_choice import apply_series_choice

    try:
        await apply_series_choice(session, job_id, series_id, action, user.id, token)
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    return JSONResponse(
        {
            "message": "Series skipped. Source files are unchanged."
            if action == "skip"
            else "Series returned to review. Check its selection before importing."
        }
    )


@router.post("/import/{job_id}/series/{series_id}/one-page/{action}", include_in_schema=False)
async def decide_one_page_archives(
    job_id: int,
    series_id: int,
    action: str,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    token: Annotated[str, Form()],
    status: str = Query("decide"),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
) -> Response:
    from fastapi import HTTPException

    from pullbox.core.exceptions import ValidationError
    from pullbox.services.import_review_one_page import decide_one_page_series
    from pullbox.tasks.import_task import trigger_import_series_rematch
    from pullbox.ui.import_routes import _render_import_review_partial

    if action not in ("allow", "skip"):
        raise HTTPException(status_code=404)
    try:
        series = await decide_one_page_series(
            session,
            job_id,
            series_id,
            actor_id=user.id,
            action="allow" if action == "allow" else "skip",
            token=token,
        )
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    if action == "allow" and (series.diagnostics or {}).get("rematch_pending"):
        trigger_import_series_rematch(job_id, series_id)
    return await _render_import_review_partial(
        job_id, request, user, session, status=status, page=page, sort=sort
    )


@router.post(
    "/import/{job_id}/series/{series_id}/dangerous/acknowledge",
    include_in_schema=False,
)
async def acknowledge_dangerous_files(
    job_id: int,
    series_id: int,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    status: str = Query("blocked"),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
) -> Response:
    from fastapi import HTTPException

    from pullbox.composition.services import build_import_control_service
    from pullbox.core.exceptions import ValidationError
    from pullbox.ui.import_routes import _render_import_review_partial

    service = build_import_control_service()
    try:
        await service.acknowledge_dangerous_files(
            session,
            job_id,
            series_id,
            actor_id=user.id,
        )
    except ValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.message) from exc
    await session.commit()
    return await _render_import_review_partial(
        job_id, request, user, session, status=status, page=page, sort=sort
    )


@router.get("/import/{job_id}/files/{file_id}/source", include_in_schema=False)
async def preview_source_action(
    job_id: int, file_id: int, request: Request, user: InteractiveOperatorUser, session: DbSession
) -> Response:
    from pullbox.services.import_review_source_actions import replacement_candidates
    from pullbox.ui.import_routes import _ctx, _templates

    job, parent, file = await load_review_file(session, job_id, file_id)
    candidates = await replacement_candidates(session, file, parent)
    action = "pair" if candidates else "recheck"
    candidate_scopes = [
        {"id": candidate.id, "scope": review_scope(job, parent, candidate)}
        for candidate in candidates
    ]
    token = await sign_review_scope(
        session,
        {
            "actor": user.id,
            "action": action,
            "scope": review_scope(job, parent, file),
            "candidate_scopes": candidate_scopes,
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
            candidates=candidates,
            candidate=candidates[0] if len(candidates) == 1 else None,
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
    candidate_file_id: Annotated[int | None, Form()] = None,
) -> Response:
    from pullbox.services.import_review_source_actions import (
        queue_source_action,
        replacement_candidates,
    )
    from pullbox.tasks.import_task import trigger_import_review_source_action

    job, parent, file = await load_review_file(session, job_id, file_id)
    candidates = (
        await replacement_candidates(session, file, parent) if source_action == "pair" else []
    )
    candidate_scopes = [
        {"id": candidate.id, "scope": review_scope(job, parent, candidate)}
        for candidate in candidates
    ]
    await verify_review_scope(
        session,
        token,
        {
            "actor": user.id,
            "action": source_action,
            "scope": review_scope(job, parent, file),
            "candidate_scopes": candidate_scopes,
        },
    )
    await queue_source_action(
        session,
        job_id,
        file_id,
        action=source_action,
        actor_id=user.id,
        candidate_file_id=candidate_file_id,
    )
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
    inline: bool = Query(False),
    issue_page: int = Query(1, ge=1),
    q: str = Query("", max_length=200),
) -> Response:
    from pullbox.ui.import_routes import _ctx, _templates

    job, parent, file = await load_review_file(session, job_id, file_id)
    require_legacy_assignment(parent, file)
    service = await build_metadata_service(session)
    series = await service.get_series_metadata(cv_id)
    issues = await service.get_issue_summaries_for_series(cv_id)
    query = q.strip().casefold()
    filtered = [
        issue
        for issue in issues
        if not query or query in f"{issue.issue_number} {issue.title or ''}".casefold()
    ]
    page_count = max(1, (len(filtered) + 24) // 25)
    issue_page = min(issue_page, page_count)
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
        "partials/import_review_issue_choices.html"
        if inline
        else "partials/import_review_file_action.html",
        _ctx(
            request,
            user,
            job=job,
            file=file,
            series=series,
            issues=filtered[(issue_page - 1) * 25 : issue_page * 25] if inline else issues,
            issue_page=issue_page,
            issue_page_count=page_count,
            query=q,
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


@router.get("/import/{job_id}/files/{file_id}/assign-native", include_in_schema=False)
async def preview_native_assignment(
    job_id: int,
    file_id: int,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    issue_page: int = Query(1, ge=1),
    q: str = Query("", max_length=200),
    follow_up: bool = Query(False),
) -> Response:
    from pullbox.services.import_library_selection import has_compiled_series_choice
    from pullbox.services.import_native_issue_choices import (
        native_issue_choice,
        native_issue_page,
        native_review_context,
    )
    from pullbox.ui.import_routes import _ctx, _templates

    job, parent, file, series, namespace = await native_review_context(
        session, job_id, file_id, follow_up=follow_up
    )
    issues, issue_page, pages = await native_issue_page(
        session,
        series.id,
        namespace,
        issue_page,
        q,
        include_other_native=bool(parent.diagnostics.get("manual_library_series"))
        or has_compiled_series_choice(file),
    )
    choices = {}
    for issue in issues:
        choice = await native_issue_choice(session, job, parent, file, series, issue, user.id)
        choice.token = await sign_review_scope(session, choice.scope)
        choices[issue.id] = choice
    return _templates().TemplateResponse(
        request,
        "partials/import_review_issue_choices.html",
        _ctx(
            request,
            user,
            job=job,
            file=file,
            series=series,
            issues=issues,
            issue_page=issue_page,
            issue_page_count=pages,
            query=q,
            native_choices=choices,
            native_assignment=True,
            follow_up_choice=follow_up,
            manual_library_choice=bool(parent.diagnostics.get("manual_library_series")),
            sidecar_library_choice=has_compiled_series_choice(file),
        ),
    )


@router.post("/import/{job_id}/files/{file_id}/assign-native", include_in_schema=False)
async def apply_native_assignment(
    job_id: int,
    file_id: int,
    user: InteractiveOperatorUser,
    session: DbSession,
    issue_id: Annotated[int, Form(gt=0)],
    token: Annotated[str, Form()],
) -> Response:
    from pullbox.services.import_native_issue_choices import assign_native_issue

    await assign_native_issue(session, job_id, file_id, issue_id, token, user.id)
    await session.commit()
    return JSONResponse(
        {"message": "File assigned. Source files and other issue decisions are unchanged."}
    )

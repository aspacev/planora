"""The requirements document of a project: what it is, the file, its removal.

The document gets to a project through the AI intake (app/api/ai_routes.py);
these routes are what is left to do with it afterwards. Its own file, by the
same rule as the export: a route here answers with a file rather than JSON.
"""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session as DbSession

from app.access import Action
from app.api.deps import ProjectContext, project_context
from app.db import get_db
from app.documents import project_document
from app.models import ProjectDocument, User

router = APIRouter(prefix="/api/projects", tags=["documents"])
logger = logging.getLogger("app.documents")


def _document(db: DbSession, context: ProjectContext) -> ProjectDocument:
    """The project's document for someone entitled to read it.

    The permission is asked before the lookup: a client must not learn from the
    difference between 403 and 404 whether the project has a document.
    """
    context.require(Action.DOCUMENT_READ)
    document = project_document(db, context.project.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    return document


def _disposition(filename: str) -> str:
    """The same two-name header as the export's: an ASCII placeholder for old
    clients and RFC 5987 for the real name — the file was named by a person,
    usually not in ASCII. Quotes and backslashes are taken out of the
    placeholder: the name is user input, and it must not be able to close the
    quoted string it is put into."""
    fallback = filename.encode("ascii", "replace").decode("ascii")
    fallback = "".join("_" if ch in '?"\\' else ch for ch in fallback)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/{project_id}/document")
def read_document(
    context: ProjectContext = Depends(project_context), db: DbSession = Depends(get_db)
):
    document = _document(db, context)
    uploader = db.get(User, document.uploaded_by) if document.uploaded_by else None
    return {
        "id": str(document.id),
        "filename": document.filename,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "created_at": document.created_at.isoformat(),
        "uploaded_by_name": uploader.name if uploader else None,
        # The interface shows the button by this flag rather than by repeating
        # the permission matrix on its side.
        "can_delete": context.can(Action.DOCUMENT_DELETE),
    }


@router.get("/{project_id}/document/file")
def download_document(
    context: ProjectContext = Depends(project_context), db: DbSession = Depends(get_db)
):
    document = _document(db, context)
    # The file column is deferred; here is the one place that needs it.
    db.refresh(document, attribute_names=["content"])
    return Response(
        content=document.content,
        # Always as a download and never under the stored type: the file was
        # uploaded by a user, and a browser must not be invited to render it on
        # the application's own origin.
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _disposition(document.filename),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/{project_id}/document", status_code=204)
def delete_document(
    context: ProjectContext = Depends(project_context), db: DbSession = Depends(get_db)
):
    """Removes the file and its text. The tasks and the clause references in
    their descriptions stay: they are the plan now, not the document."""
    context.require(Action.DOCUMENT_DELETE)
    document = project_document(db, context.project.id)
    if document is None:
        raise HTTPException(status_code=404, detail="document_not_found")
    # No journal entry exists for this and none can: the document is not part of
    # the plan. A line in the application log is the trace — as with deleting a
    # project.
    logger.info("документ проекта %s удалил %s", context.project.id, context.user.id)
    db.delete(document)
    return None

"""
curator.py — Session manager and orchestration for the Module Curation pipeline.

State machine:
  INIT → GATHERING → ASKING → (ASKING_FOLLOWUP)? → READY → GENERATING → DONE | ERROR

v2.1 additions
==============
* On session start (any mode), `./repos/` is scanned and any modules that look
  relevant to the service_name are attached to the session.
* When tf_files are present, dependent modules are resolved transitively
  (local → repos/ → ChromaDB → Registry) and attached to the session.
* After the user finishes the first round of Q&A the engine generates
  follow-up questions; if any come back the session transitions to
  ASKING_FOLLOWUP instead of READY.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from backend.module_curator.models import (
    CurationMode,
    CurationSession,
    DependentModuleRef,
    GenerationResult,
    LocalModuleRef,
    QAPair,
    SessionStatus,
    SessionView,
    StartCurationRequest,
)
from backend.module_curator.question_engine import (
    generate_followups,
    generate_questions,
)
from backend.module_curator.code_generator import generate_terraform_code
from backend.module_curator.module_fetcher import (
    extract_module_sources,
    fetch_from_github,
    fetch_from_local,
    fetch_from_zip,
)
from backend.module_curator.local_repo_scanner import (
    find_matching_modules,
    scan_local_modules,
)
from backend.module_curator.dependency_resolver import (
    DependencyTree,
    resolve_dependencies,
)
from backend.registry_fetcher.registry_api import fetch_service_docs


# ── In-memory session store ───────────────────────────────────────────────────

_sessions: dict[str, CurationSession] = {}


# ── Public helpers ────────────────────────────────────────────────────────────

def create_session(req: StartCurationRequest) -> CurationSession:
    sid = str(uuid.uuid4())
    session = CurationSession(
        session_id=sid,
        mode=req.mode,
        provider=req.provider,
        service_name=req.service_name,
        repo_name=req.repo_name,
        new_tag=req.new_tag,
        document_text=req.description,
        status=SessionStatus.GATHERING,
    )
    _sessions[sid] = session
    return session


def get_session(session_id: str) -> Optional[CurationSession]:
    return _sessions.get(session_id)


def to_view(session: CurationSession) -> SessionView:
    return SessionView(
        session_id=session.session_id,
        mode=session.mode,
        provider=session.provider,
        service_name=session.service_name,
        status=session.status,
        questions=session.questions,
        current_question_idx=session.current_question_idx,
        qa_pairs=session.qa_pairs,
        tf_files_loaded=list(session.tf_files.keys()),
        registry_docs_available=bool(
            session.registry_docs and not session.registry_docs.startswith("[")
        ),
        local_modules=session.local_modules,
        dependent_modules=session.dependent_modules,
        followup_round=session.followup_round,
        result=session.result,
        error=session.error,
        current_question=session.current_question,
        all_questions_answered=session.all_questions_answered,
    )


# ── Context ingestion ─────────────────────────────────────────────────────────

async def _fetch_docs_if_needed(session: CurationSession) -> None:
    if session.service_name and not session.registry_docs:
        try:
            session.registry_docs = await fetch_service_docs(
                session.provider.value, session.service_name
            )
        except Exception as exc:
            print(f"[curator] Registry fetch failed: {exc}")
            session.registry_docs = ""


def _scan_local_repos(session: CurationSession) -> None:
    """Attach matching modules from ./repos/ to the session."""
    if not session.service_name:
        return
    try:
        matches = find_matching_modules(
            session.service_name, session.provider.value, limit=5
        )
    except Exception as exc:
        print(f"[curator] Local repo scan failed: {exc}")
        return

    session.local_modules = [
        LocalModuleRef(
            name=m.name,
            display_name=m.display_name,
            rel_path=m.rel_path,
            resource_types=sorted(m.resource_types)[:10],
            required_inputs=list(m.required_inputs.keys())[:10],
            match_reason=(
                f"matched on resource types / name / "
                f"product={m.gcp_product}"
            ),
        )
        for m in matches
    ]


async def _resolve_dependent_modules(session: CurationSession) -> None:
    """Walk module {} references in tf_files and attach the resolved tree."""
    if not session.tf_files:
        return
    try:
        # Anchor for relative-path resolution: use the first repo dir we know about.
        anchor: Optional[Path] = None
        if session.local_modules:
            from backend.module_curator.local_repo_scanner import get_module_by_name
            mod = get_module_by_name(session.local_modules[0].name)
            if mod:
                anchor = mod.abs_path
        tree: DependencyTree = await resolve_dependencies(
            session.tf_files, root_dir=anchor, max_depth=2, max_total=15
        )
    except Exception as exc:
        print(f"[curator] Dependency resolution failed: {exc}")
        return

    session.dependent_modules = [
        DependentModuleRef(
            raw_source=d.raw_source,
            kind=d.kind,
            depth=d.depth,
            required_inputs=[n for n, v in d.inputs.items() if v.get("required")][:8],
            outputs=list(d.outputs.keys())[:8],
        )
        for d in tree.dependencies
    ]


async def _start_asking(session: CurationSession) -> list[str]:
    """Common entry — gather context, then generate first-round questions."""
    await _fetch_docs_if_needed(session)
    _scan_local_repos(session)

    if session.tf_files:
        await _resolve_dependent_modules(session)

    questions = await generate_questions(session)
    session.questions = questions
    session.current_question_idx = 0
    session.status = SessionStatus.ASKING
    return questions


# ── Mode-specific start helpers ───────────────────────────────────────────────

async def start_new_product(session_id: str) -> list[str]:
    session = _get_or_raise(session_id)
    return await _start_asking(session)


async def start_from_document(session_id: str, text: str) -> list[str]:
    session = _get_or_raise(session_id)
    session.document_text = (session.document_text + "\n\n" + text).strip()
    return await _start_asking(session)


async def start_from_github(
    session_id: str, url: str, git_tag: Optional[str] = None
) -> list[str]:
    session = _get_or_raise(session_id)
    tf_files = fetch_from_github(url, git_tag, session_tag=session_id[:8])
    session.tf_files = tf_files
    return await _start_asking(session)


async def start_from_local(session_id: str, path: str) -> list[str]:
    session = _get_or_raise(session_id)
    session.tf_files = fetch_from_local(path)
    return await _start_asking(session)


async def start_from_zip(session_id: str, zip_bytes: bytes) -> list[str]:
    session = _get_or_raise(session_id)
    session.tf_files = fetch_from_zip(zip_bytes, session_tag=session_id[:8])
    return await _start_asking(session)


async def start_from_tf_files(session_id: str, tf_files: dict[str, str]) -> list[str]:
    session = _get_or_raise(session_id)
    session.tf_files = {k: v for k, v in tf_files.items() if k.endswith(".tf")}
    return await _start_asking(session)


async def start_self_curation(session_id: str) -> list[str]:
    """Load existing repo code and start Q&A for self-curation."""
    session = _get_or_raise(session_id)

    if session.repo_name:
        from backend.config import get_config
        from backend.agent.tools.git_tools import (
            get_file_at_tag,
            list_tags_for_repo,
            list_tf_files_at_tag,
        )

        cfg = get_config()
        repo_cfg = cfg.get_repo(session.repo_name)
        if repo_cfg:
            tags = list_tags_for_repo(session.repo_name)
            if tags:
                latest = tags[0]
                for fpath in list_tf_files_at_tag(session.repo_name, latest)[:20]:
                    content = get_file_at_tag(session.repo_name, latest, fpath)
                    if content:
                        session.tf_files[fpath] = content

    return await _start_asking(session)


# ── Q&A ───────────────────────────────────────────────────────────────────────

async def answer_question(session_id: str, answer: str) -> Optional[str]:
    """
    Record answer to the current question.

    Returns the next question string, or None if the session has moved on.
    When the last question is answered we run `generate_followups()` — if it
    returns questions, the session transitions to ASKING_FOLLOWUP and a new
    question is returned; otherwise the session transitions to READY.
    """
    session = _get_or_raise(session_id)

    if session.current_question is None:
        return None

    session.qa_pairs.append(QAPair(question=session.current_question, answer=answer))
    session.current_question_idx += 1

    # If still more pre-asked questions, return the next one as-is.
    if not session.all_questions_answered:
        return session.current_question

    # All questions answered — see if we need follow-ups.
    if session.followup_round < session.max_followup_rounds:
        try:
            followups = await generate_followups(session)
        except Exception as exc:
            print(f"[curator] Follow-up generation failed: {exc}")
            followups = []

        if followups:
            session.questions.extend(followups)
            session.followup_round += 1
            session.status = SessionStatus.ASKING_FOLLOWUP
            return session.current_question

    session.status = SessionStatus.READY
    return None


# ── Code generation ───────────────────────────────────────────────────────────

async def generate(session_id: str) -> GenerationResult:
    session = _get_or_raise(session_id)
    session.status = SessionStatus.GENERATING

    try:
        result = await generate_terraform_code(session)
        # Stamp the modules used onto the result so the UI can show them.
        result.local_modules_used = list(session.local_modules)
        result.dependent_modules  = list(session.dependent_modules)
        session.result = result
        session.status = SessionStatus.DONE
        return result
    except Exception as exc:
        session.status = SessionStatus.ERROR
        session.error = str(exc)
        raise


# ── Private ───────────────────────────────────────────────────────────────────

def _get_or_raise(session_id: str) -> CurationSession:
    s = _sessions.get(session_id)
    if s is None:
        raise KeyError(f"Session '{session_id}' not found")
    return s

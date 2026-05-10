"""
curator.py — Session manager and orchestration for the Module Curation pipeline.

State machine:
  INIT → GATHERING → ASKING → READY → GENERATING → DONE | ERROR
"""
from __future__ import annotations

import uuid
from typing import Optional

from backend.module_curator.models import (
    CurationMode,
    CurationSession,
    GenerationResult,
    QAPair,
    SessionStatus,
    SessionView,
    StartCurationRequest,
)
from backend.module_curator.question_engine import generate_questions
from backend.module_curator.code_generator import generate_terraform_code
from backend.module_curator.module_fetcher import (
    extract_module_sources,
    fetch_from_github,
    fetch_from_local,
    fetch_from_zip,
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
        result=session.result,
        error=session.error,
        current_question=session.current_question,
        all_questions_answered=session.all_questions_answered,
    )


# ── Context ingestion ─────────────────────────────────────────────────────────

async def _fetch_docs_if_needed(session: CurationSession) -> None:
    if session.service_name and not session.registry_docs:
        session.registry_docs = await fetch_service_docs(
            session.provider.value, session.service_name
        )


async def _resolve_cross_modules(session: CurationSession) -> None:
    """Try to look up module sources in indexed ChromaDB collections."""
    sources = extract_module_sources(session.tf_files)
    for src in sources[:3]:
        if src in session.referenced_modules:
            continue
        content = _search_chromadb(src)
        if content:
            session.referenced_modules[src] = content


def _search_chromadb(source_hint: str) -> Optional[str]:
    try:
        from backend.agent.tools.search_tools import semantic_search, get_indexed_tags
        from backend.config import get_config

        cfg = get_config()
        for repo in cfg.enabled_repos:
            tags = get_indexed_tags(repo.name)
            if not tags:
                continue
            results = semantic_search(
                f"module source {source_hint}", repo.name, tags[0], n_results=3
            )
            if results:
                return f"# From {repo.name}\n" + "\n".join(r.snippet for r in results)
    except Exception:
        pass
    return None


async def _start_asking(session: CurationSession) -> list[str]:
    """Generate questions and transition to ASKING state."""
    await _fetch_docs_if_needed(session)
    if session.tf_files:
        await _resolve_cross_modules(session)

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
    """Record answer to the current question. Returns the next question or None."""
    session = _get_or_raise(session_id)

    if session.current_question is None:
        return None

    session.qa_pairs.append(QAPair(question=session.current_question, answer=answer))
    session.current_question_idx += 1

    if session.all_questions_answered:
        session.status = SessionStatus.READY
        return None

    return session.current_question


# ── Code generation ───────────────────────────────────────────────────────────

async def generate(session_id: str) -> GenerationResult:
    session = _get_or_raise(session_id)
    session.status = SessionStatus.GENERATING

    try:
        result = await generate_terraform_code(session)
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

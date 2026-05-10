"""
processor.py — Extract plain text from PDF, Word (.docx), and plain text files.
"""
from __future__ import annotations

import io
from pathlib import Path


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Dispatch to the correct extractor based on file extension."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _from_pdf(file_bytes)
    if ext in (".docx", ".doc"):
        return _from_docx(file_bytes)
    # .txt, .md, .tf, .hcl, or anything else
    return file_bytes.decode("utf-8", errors="replace")


def _from_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n\n".join(t for t in pages if t.strip())
    except ImportError:
        return "[pdfplumber not installed — run: pip install pdfplumber]"
    except Exception as exc:
        return f"[PDF extraction error: {exc}]"


def _from_docx(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except ImportError:
        return "[python-docx not installed — run: pip install python-docx]"
    except Exception as exc:
        return f"[DOCX extraction error: {exc}]"

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Literal

import fitz  # PyMuPDF
import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000/files")

app = FastAPI(title="Architectural Document Reviewer Backend")
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")


class Finding(BaseModel):
    sheet_number: str | None = None
    page_index: int = Field(..., ge=0)
    status: Literal["MISSED", "PARTIAL", "VERIFY"]
    label: str
    note: str
    bbox: list[float] | None = Field(
        None,
        description="Rectangle [x0, y0, x1, y1] in PDF points. If omitted, a default callout is placed."
    )


class ReviewRequest(BaseModel):
    review_pdf_url: str | None = None
    revised_pdf_url: str
    project_name: str = "Architectural_Document_Review"
    findings: list[Finding]


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return value or "Architectural_Document_Review"


def download_pdf(url: str, target: Path) -> None:
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not download PDF: {exc}") from exc
    if not response.content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Downloaded file does not appear to be a PDF.")
    target.write_bytes(response.content)


def default_bbox(page: fitz.Page, index: int) -> fitz.Rect:
    # Stagger default boxes near the right side when exact coordinates are unknown.
    rect = page.rect
    width = rect.width
    y = 72 + (index % 10) * 58
    return fitz.Rect(width - 260, y, width - 36, y + 42)


def color_for_status(status: str) -> tuple[float, float, float]:
    if status == "PARTIAL":
        return (1, 0.8, 0)  # yellow/orange
    if status == "VERIFY":
        return (0.1, 0.45, 1)  # blue
    return (1, 0.45, 0)  # orange


@app.post("/create-review-pdf")
def create_review_pdf(payload: ReviewRequest):
    if not payload.findings:
        raise HTTPException(status_code=400, detail="No findings were provided.")

    job_id = uuid.uuid4().hex[:10]
    input_path = OUTPUT_DIR / f"{job_id}_revised.pdf"
    output_name = f"{safe_name(payload.project_name)}_Reviewed_Markups.pdf"
    output_path = OUTPUT_DIR / f"{job_id}_{output_name}"

    download_pdf(payload.revised_pdf_url, input_path)

    try:
        doc = fitz.open(input_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not open revised PDF: {exc}") from exc

    for page_finding_index, finding in enumerate(payload.findings):
        if finding.page_index >= len(doc):
            continue

        page = doc[finding.page_index]
        color = color_for_status(finding.status)
        rect = fitz.Rect(finding.bbox) if finding.bbox else default_bbox(page, page_finding_index)

        # Draw review rectangle.
        page.draw_rect(rect, color=color, width=3, overlay=True)

        # Add a compact label above or below the box.
        label = f"{finding.status} - {finding.label}"[:90]
        label_rect = fitz.Rect(rect.x0, max(12, rect.y0 - 20), min(page.rect.width - 24, rect.x1 + 120), rect.y0 - 2)
        if label_rect.height < 10:
            label_rect = fitz.Rect(rect.x0, rect.y1 + 2, min(page.rect.width - 24, rect.x1 + 120), rect.y1 + 22)

        page.draw_rect(label_rect, color=color, fill=(1, 1, 1), width=1, overlay=True)
        page.insert_textbox(label_rect, label, fontsize=8, color=color, align=0, overlay=True)

        # Add PDF comment annotation with the detailed note.
        annot = page.add_text_annot(rect.tl, finding.note)
        annot.set_info(title=finding.status, content=finding.note)
        annot.update()

    doc.save(output_path, deflate=True, garbage=4)
    doc.close()

    return {
        "annotated_pdf_url": f"{PUBLIC_BASE_URL}/{output_path.name}",
        "summary": f"Created annotated PDF with {len(payload.findings)} marked finding(s)."
    }


@app.get("/health")
def health():
    return {"status": "ok"}

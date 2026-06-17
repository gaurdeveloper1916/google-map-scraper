import logging
import math
import os
import queue
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from scraper import scrape_places


# FIREBASE INIT

_CRED_PATH = os.path.join(os.path.dirname(__file__), "firebase_credentials.json")

cred = credentials.Certificate(_CRED_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()


# APP

app = FastAPI(title="Google Maps Scraper API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# JSON SANITIZER  (NaN / inf → None)

def sanitize(obj: Any) -> Any:
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


# REQUEST / RESPONSE MODELS

class CategoryRequest(BaseModel):
    name: str                        # e.g. "FMCG", "Restaurants"
    description: Optional[str] = ""


class ScrapeRequest(BaseModel):
    search: str                      # Google Maps search query
    total: int = 100
    category_id: str                 # Firestore category doc ID


# IN-MEMORY JOB STORE

jobs: Dict[str, dict] = {}


# QUEUE LOG HANDLER

class QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue
        self.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )

    def emit(self, record: logging.LogRecord):
        self.log_queue.put(self.format(record))


# BACKGROUND SCRAPER THREAD

def run_scraper(job_id: str, search_for: str, total: int, category_id: str):
    job = jobs[job_id]
    log_queue: queue.Queue = job["log_queue"]

    logger = logging.getLogger(f"scraper.{job_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = QueueHandler(log_queue)
    logger.addHandler(handler)

    try:
        # Verify category exists
        cat_ref = db.collection("categories").document(category_id)
        if not cat_ref.get().exists:
            raise ValueError(f"Category '{category_id}' not found in Firestore")

        places = scrape_places(search_for, total, logger=logger)

        if not places:
            logger.info("No places found.")
            job["result"] = []
            job["status"] = "done"
            return

        # Deduplicate
        df = pd.DataFrame([asdict(p) for p in places])
        df.drop_duplicates(subset=["name", "phone_number"], inplace=True)
        records = sanitize(df.to_dict(orient="records"))

        # Save to Firestore:
        #   1. categories/{category_id}/places/{auto_id}  — scoped under category
        #   2. places/{auto_id}                            — flat global table
        places_ref = cat_ref.collection("places")
        global_places_ref = db.collection("places")
        batch = db.batch()
        saved = 0

        for record in records:
            record["scraped_at"] = datetime.now(timezone.utc).isoformat()
            record["search_query"] = search_for
            record["category_id"] = category_id
            record["job_id"] = job_id

            # Subcollection under category
            cat_doc_ref = places_ref.document()
            batch.set(cat_doc_ref, record)

            # Global places collection (same doc ID for cross-reference)
            global_doc_ref = global_places_ref.document(cat_doc_ref.id)
            batch.set(global_doc_ref, record)

            saved += 1

            if saved % 250 == 0:
                batch.commit()
                batch = db.batch()

        batch.commit()

        cat_ref.update({
            "last_scraped": datetime.now(timezone.utc).isoformat(),
            "total_places": firestore.Increment(saved),
        })

        logger.info(f"Saved {saved} places to Firestore under category '{category_id}'")
        job["result"] = records
        job["status"] = "done"

    except Exception as e:
        logger.error(f"Scraper crashed: {e}")
        job["status"] = "error"
        job["error"] = str(e)

    finally:
        log_queue.put(None)
        logger.removeHandler(handler)


# CATEGORY ROUTES

@app.post("/categories", summary="Create a new category")
def create_category(req: CategoryRequest):
    """
    Creates a category in Firestore.
    Returns the generated category_id to use when starting a scrape.
    """
    category_id = str(uuid.uuid4())
    db.collection("categories").document(category_id).set({
        "id": category_id,
        "name": req.name,
        "description": req.description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_places": 0,
        "last_scraped": None,
    })
    return {"category_id": category_id, "name": req.name}


@app.get("/categories", summary="List all categories")
def list_categories():
    """Returns all categories from Firestore."""
    docs = db.collection("categories").stream()
    return {"categories": [doc.to_dict() for doc in docs]}


@app.get("/categories/{category_id}", summary="Get a single category")
def get_category(category_id: str):
    doc = db.collection("categories").document(category_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Category not found")
    return doc.to_dict()


@app.get("/categories/{category_id}/places", summary="Get all places in a category")
def get_places(category_id: str):
    """Returns all scraped places stored under a category."""
    cat_ref = db.collection("categories").document(category_id)
    if not cat_ref.get().exists:
        raise HTTPException(status_code=404, detail="Category not found")

    docs = cat_ref.collection("places").stream()
    places = [doc.to_dict() for doc in docs]
    return JSONResponse({"category_id": category_id, "count": len(places), "places": places})


@app.delete("/categories/{category_id}", summary="Delete a category and all its places")
def delete_category(category_id: str):
    cat_ref = db.collection("categories").document(category_id)
    if not cat_ref.get().exists:
        raise HTTPException(status_code=404, detail="Category not found")

    # Delete all places subcollection docs
    for doc in cat_ref.collection("places").stream():
        doc.reference.delete()

    cat_ref.delete()
    return {"deleted": category_id}


# SCRAPE ROUTES

@app.post("/scrape", summary="Start a scrape job")
def start_scrape(req: ScrapeRequest):
    """
    Start a scrape job. Results are saved to Firestore under the given category.

    Returns a `job_id` to:
    - Stream live logs:  GET /scrape/{job_id}/stream
    - Fetch results:     GET /scrape/{job_id}/result
    """
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "log_queue": queue.Queue(),
        "result": [],
        "error": None,
        "category_id": req.category_id,
    }

    threading.Thread(
        target=run_scraper,
        args=(job_id, req.search, req.total, req.category_id),
        daemon=True,
    ).start()

    return {"job_id": job_id, "status": "running", "category_id": req.category_id}


@app.get("/scrape/{job_id}/stream", summary="Stream live log messages (SSE)")
def stream_logs(job_id: str):
    """
    Server-Sent Events stream of real-time log messages.
    Closes automatically when the job finishes.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    log_queue: queue.Queue = jobs[job_id]["log_queue"]

    def event_generator():
        while True:
            try:
                msg = log_queue.get(timeout=30)
            except queue.Empty:
                yield ": ping\n\n"
                continue

            if msg is None:
                yield f"event: done\ndata: {jobs[job_id]['status']}\n\n"
                break

            yield f"data: {msg}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/scrape/{job_id}/result", summary="Get scrape results")
def get_result(job_id: str):
    """
    Returns scraped places once the job is done.
    Results are fetched from Firestore (persistent) using the job_id field,
    so they survive server restarts. Falls back to in-memory if still running.
    """
    # Check Firestore first — results may exist even if job is not in memory
    firestore_docs = (
        db.collection("places")
        .where("job_id", "==", job_id)
        .stream()
    )
    firestore_results = [doc.to_dict() for doc in firestore_docs]

    if firestore_results:
        category_id = firestore_results[0].get("category_id", "")
        return JSONResponse({
            "status": "done",
            "job_id": job_id,
            "category_id": category_id,
            "count": len(firestore_results),
            "result": firestore_results,
        })

    # Fall back to in-memory job store (job still running or not yet flushed)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]

    if job["status"] == "running":
        return JSONResponse({"status": "running", "job_id": job_id, "result": []})

    if job["status"] == "error":
        raise HTTPException(status_code=500, detail=job["error"])

    return JSONResponse({
        "status": "done",
        "job_id": job_id,
        "category_id": job.get("category_id", ""),
        "count": len(job["result"]),
        "result": job["result"],
    })


@app.get("/scrape/{job_id}/status", summary="Check job status")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": jobs[job_id]["status"]}


@app.get("/health")
def health():
    return {"ok": True}


# GLOBAL PLACES TABLE

@app.get("/places", summary="List all scraped places (global table)")
def list_all_places(category_id: Optional[str] = None):
    """
    Returns all scraped places from the global `places` collection.
    Optionally filter by category_id via query param: /places?category_id=xxx
    """
    ref = db.collection("places")
    if category_id:
        ref = ref.where("category_id", "==", category_id)
    docs = ref.stream()
    places = [doc.to_dict() for doc in docs]
    return JSONResponse({"count": len(places), "places": places})





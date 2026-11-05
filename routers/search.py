"""
Spac2 Search API Router
========================================
Provides RESTful search endpoints across the Spac2 Web OS Ecosystem.
Supports multi-language search (Vietnamese, English, Korean), diacritics-insensitive
matching, keyword scoring, autocomplete suggestions, and category filtering.
"""

import os
import json
import re
import unicodedata
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Query, HTTPException

router = APIRouter(prefix="/api/search", tags=["search"])

# Path to datasets in /search/data/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEARCH_DATA_DIR = os.path.join(BASE_DIR, "search", "data")

# In-memory cache for search datasets with mtime check
_DATA_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_MTIME: Dict[str, float] = {}

# Default Trending Keywords
DEFAULT_TRENDING = [
    "spac2",
    "spac2 doc",
    "spac2 ai",
    "spac2 note",
    "spac2 chat",
    "spac2 music",
    "spac2 games",
    "pdf hub",
    "mindmap",
    "education"
]

# Vietnamese / International Diacritics Normalization
def normalize_text(text: str) -> str:
    """Normalize text by converting to lowercase and stripping Vietnamese / Unicode accents."""
    if not text:
        return ""
    text = text.lower()
    text = text.replace("đ", "d").replace("Đ", "d")
    # Decompose unicode characters into base char + combining accent
    nfkd = unicodedata.normalize("NFKD", text)
    # Strip accent marks
    stripped = "".join([c for c in nfkd if not unicodedata.combining(c)])
    # Remove excessive whitespace
    return re.sub(r"\s+", " ", stripped).strip()


def load_dataset(lang: str = "vi") -> List[Dict[str, Any]]:
    """Loads and caches dataset from /search/data/<lang>.json with automatic reload on file changes."""
    lang = lang.lower().strip()
    if lang not in ["vi", "en", "kr"]:
        lang = "vi"

    filepath = os.path.join(SEARCH_DATA_DIR, f"{lang}.json")
    if not os.path.isfile(filepath):
        # Fallback to English if file not found
        fallback_path = os.path.join(SEARCH_DATA_DIR, "en.json")
        if not os.path.isfile(fallback_path):
            return []
        filepath = fallback_path

    try:
        current_mtime = os.path.getmtime(filepath)
        if lang in _DATA_CACHE and _CACHE_MTIME.get(lang) == current_mtime:
            return _DATA_CACHE[lang].get("apps", [])

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        apps = data.get("apps", [])
        # Pre-calculate normalized fields for ultra-fast matching
        for app in apps:
            app["_norm_title"] = normalize_text(app.get("t", ""))
            app["_norm_desc"] = normalize_text(app.get("d", ""))
            app["_norm_meta"] = normalize_text(app.get("metadata", ""))
            norm_tags = [normalize_text(tag) for tag in app.get("tags", [])]
            app["_norm_tags"] = norm_tags
            app["_norm_tags_str"] = " ".join(norm_tags)

        _DATA_CACHE[lang] = {"apps": apps}
        _CACHE_MTIME[lang] = current_mtime
        return apps
    except Exception as e:
        print(f"[Search API] Error loading dataset {filepath}: {e}")
        return []


def score_app(app: Dict[str, Any], raw_q: str, norm_q: str, q_words: List[str]) -> int:
    """Calculates relevance score for an application given a search query."""
    title_raw = (app.get("t") or "").lower()
    title_norm = app.get("_norm_title", "")
    desc_norm = app.get("_norm_desc", "")
    meta_norm = app.get("_norm_meta", "")
    tags_norm = app.get("_norm_tags", [])
    tags_str = app.get("_norm_tags_str", "")

    score = 0

    # 1. Exact match with title (highest priority)
    if norm_q == title_norm or raw_q.lower() == title_raw:
        score += 1000

    # 2. Title starts with query
    elif title_norm.startswith(norm_q):
        score += 500

    # 3. Query is inside title
    elif norm_q in title_norm:
        score += 300

    # 4. Exact match in tags
    if norm_q in tags_norm:
        score += 350
    elif any(norm_q in t for t in tags_norm):
        score += 200

    # 5. Metadata or description contains full query
    if norm_q in desc_norm:
        score += 150
    if norm_q in meta_norm:
        score += 100

    # 6. Multi-word individual match scoring
    if len(q_words) > 1:
        matched_words = 0
        for w in q_words:
            if not w:
                continue
            if w in title_norm:
                score += 80
                matched_words += 1
            elif w in tags_str:
                score += 60
                matched_words += 1
            elif w in desc_norm or w in meta_norm:
                score += 30
                matched_words += 1
        # Bonus if all words matched
        if matched_words == len(q_words):
            score += 200

    return score


@router.get("")
@router.get("/")
async def search_apps(
    q: Optional[str] = Query(None, description="Search keyword"),
    lang: str = Query("vi", description="Language code: vi, en, kr"),
    category: Optional[str] = Query("all", description="Category filter (e.g. all, workspace, ai, tools, game, music, education)"),
    limit: int = Query(20, ge=1, le=100, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    """
    Search Spac2 applications with multi-field scoring and diacritics-insensitive matching.
    """
    raw_query = (q or "").strip()
    dataset = load_dataset(lang)

    # Empty query: return default popular apps
    if not raw_query:
        # If category filter is applied
        if category and category.lower() != "all":
            norm_cat = normalize_text(category)
            filtered = [
                app for app in dataset
                if norm_cat in app.get("_norm_meta", "") or norm_cat in app.get("_norm_tags_str", "")
            ]
            total = len(filtered)
            paged = filtered[offset : offset + limit]
        else:
            total = len(dataset)
            paged = dataset[offset : offset + limit]

        results = [
            {
                "title": app.get("t"),
                "description": app.get("d"),
                "url": app.get("l"),
                "icon": app.get("i"),
                "metadata": app.get("metadata"),
                "tags": app.get("tags", [])
            }
            for app in paged
        ]
        return {
            "status": "success",
            "query": "",
            "lang": lang,
            "category": category,
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": results
        }

    norm_query = normalize_text(raw_query)
    q_words = norm_query.split()

    norm_category = normalize_text(category) if category and category.lower() != "all" else None

    scored_items = []
    for app in dataset:
        # Category filtering if specified
        if norm_category:
            in_cat = (
                norm_category in app.get("_norm_meta", "") or
                norm_category in app.get("_norm_tags_str", "")
            )
            if not in_cat:
                continue

        score = score_app(app, raw_query, norm_query, q_words)
        if score > 0:
            scored_items.append((score, app))

    # Sort descending by score
    scored_items.sort(key=lambda x: x[0], reverse=True)
    total_matched = len(scored_items)

    paged_items = scored_items[offset : offset + limit]
    results = [
        {
            "title": app.get("t"),
            "description": app.get("d"),
            "url": app.get("l"),
            "icon": app.get("i"),
            "metadata": app.get("metadata"),
            "tags": app.get("tags", []),
            "relevance_score": score
        }
        for score, app in paged_items
    ]

    return {
        "status": "success",
        "query": raw_query,
        "lang": lang,
        "category": category or "all",
        "total": total_matched,
        "limit": limit,
        "offset": offset,
        "results": results
    }


@router.get("/suggest")
async def search_suggestions(
    q: Optional[str] = Query("", description="Input query prefix"),
    lang: str = Query("vi", description="Language code: vi, en, kr"),
    limit: int = Query(8, ge=1, le=20, description="Max suggestions")
):
    """
    Returns real-time search suggestions / autocomplete for search inputs.
    """
    raw_query = (q or "").strip()
    if not raw_query:
        return {
            "status": "success",
            "query": "",
            "suggestions": DEFAULT_TRENDING[:limit]
        }

    norm_query = normalize_text(raw_query)
    dataset = load_dataset(lang)

    suggestions_set = set()
    suggestions_list = []

    # 1. Match from App Titles
    for app in dataset:
        title = app.get("t", "")
        norm_title = app.get("_norm_title", "")
        if norm_query in norm_title and title.lower() not in suggestions_set:
            suggestions_set.add(title.lower())
            suggestions_list.append(title)
            if len(suggestions_list) >= limit:
                break

    # 2. Match from App Tags if still have slots
    if len(suggestions_list) < limit:
        for app in dataset:
            for tag in app.get("tags", []):
                norm_tag = normalize_text(tag)
                tag_display = tag.replace("-", " ")
                if norm_query in norm_tag and tag_display.lower() not in suggestions_set:
                    suggestions_set.add(tag_display.lower())
                    suggestions_list.append(tag_display)
                    if len(suggestions_list) >= limit:
                        break
            if len(suggestions_list) >= limit:
                break

    return {
        "status": "success",
        "query": raw_query,
        "suggestions": suggestions_list
    }


@router.get("/apps")
async def list_all_apps(
    lang: str = Query("vi", description="Language code: vi, en, kr")
):
    """
    Returns all registered applications and tools in the ecosystem.
    """
    dataset = load_dataset(lang)
    apps = [
        {
            "title": app.get("t"),
            "description": app.get("d"),
            "url": app.get("l"),
            "icon": app.get("i"),
            "metadata": app.get("metadata"),
            "tags": app.get("tags", [])
        }
        for app in dataset
    ]
    return {
        "status": "success",
        "lang": lang,
        "count": len(apps),
        "apps": apps
    }


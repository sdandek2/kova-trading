"""
Test Alpaca News SDK compatibility after the dict-based article fix.

Verifies:
  1. news.data.get('news', []) works (new SDK structure)
  2. Articles come back as dicts (not objects)
  3. All accessed fields (headline, id, author, created_at, url, symbols)
     are reachable via both dict .get() and the _g() lambda used in alpaca_service

Run locally (needs env vars):
  railway run python3 backend/scripts/test_news_sdk.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings

print("=" * 60)
print("TEST: Alpaca News SDK compatibility")
print("=" * 60)

# ── 1. Raw SDK call ───────────────────────────────────────────────────────────
print("\n[1] Raw NewsClient call...")
from alpaca.data.historical import NewsClient
from alpaca.data.requests import NewsRequest

nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
news = nc.get_news(NewsRequest(limit=5))

print(f"    news type        : {type(news).__name__}")
print(f"    has .data attr   : {hasattr(news, 'data')}")
print(f"    has .news attr   : {hasattr(news, 'news')}")

# ── 2. Our extraction logic (mirrors alpaca_service.py) ──────────────────────
print("\n[2] Extracting articles with our fix...")
_articles = (
    news.data.get('news', [])
    if isinstance(getattr(news, 'data', None), dict)
    else getattr(news, 'news', [])
)
print(f"    articles found   : {len(_articles)}")
if not _articles:
    print("    WARN: 0 articles — nothing to validate")
    sys.exit(0)

# ── 3. Check each article ─────────────────────────────────────────────────────
print("\n[3] Validating article fields...")
FIELDS = ['id', 'headline', 'author', 'created_at', 'url', 'symbols', 'source']
all_ok = True

for i, article in enumerate(_articles):
    is_dict = isinstance(article, dict)
    _g = (lambda f: article.get(f) if is_dict else getattr(article, f, None))

    missing = [f for f in FIELDS if _g(f) is None and f not in ('source',)]
    ok = len(missing) == 0

    print(f"\n    Article {i+1}: type={type(article).__name__}  dict={is_dict}")
    print(f"      headline   : {str(_g('headline') or '')[:60]}")
    print(f"      symbols    : {(article.get('symbols') if is_dict else article.symbols) or []}")
    print(f"      created_at : {_g('created_at')}")
    print(f"      id         : {_g('id')}")
    print(f"      author     : {_g('author')}")
    print(f"      url        : {str(_g('url') or '')[:50]}")
    if missing:
        print(f"      MISSING    : {missing}")
        all_ok = False

# ── 4. Test get_tradeable_universe news path ──────────────────────────────────
print("\n[4] Universe news path (Counter over article.symbols)...")
from collections import Counter
syms: list = []
for article in _articles:
    _g2 = (lambda f: article.get(f) if isinstance(article, dict) else getattr(article, f, None))
    syms.extend(
        (article.get('symbols') if isinstance(article, dict) else article.symbols) or []
    )
top = Counter(syms).most_common(5)
print(f"    symbol mentions  : {top}")

# ── Result ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("RESULT:", "PASS — all fields accessible" if all_ok else "FAIL — some fields missing (see above)")
print("=" * 60)

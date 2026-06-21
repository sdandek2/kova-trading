"""
Quick local test for get_sentiment_context() — shows signed sentiment per symbol.
Run from backend/ directory:
    python3 scripts/test_sentiment.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from services.alpaca_service import get_sentiment_context, _score_headline, _BULLISH_TERMS, _BEARISH_TERMS

# ── 1. Test the keyword classifier directly ──────────────────────────────────
print("=" * 60)
print("  TEST 1: _score_headline() keyword classifier")
print("=" * 60)

test_headlines = [
    ("NVDA beats earnings estimates by 20%, raises guidance",         ""),
    ("INTC misses revenue, cuts guidance, announces layoffs",         ""),
    ("AAPL stock in focus ahead of iPhone launch",                    ""),
    ("META upgraded to Buy, analyst raises price target",             ""),
    ("TSLA CEO departure raises investor concerns",                   ""),
    ("AMZN wins $10B government contract, acquisition announced",     ""),
    ("GOOG faces SEC investigation and class action lawsuit",         ""),
    ("SPY closes flat — markets digest Fed comments",                 ""),
]

for headline, summary in test_headlines:
    score = _score_headline(headline, summary)
    label = "BULLISH (+1)" if score == 1 else "BEARISH (-1)" if score == -1 else "NEUTRAL ( 0)"
    print(f"  {label}  {headline[:65]}")

# ── 2. Score articles from get_news() (RSS feeds — same source test_e2e uses) ─
print()
print("=" * 60)
print("  TEST 2: get_news() → _score_headline() pipeline")
print("=" * 60)

from services.alpaca_service import get_news
from collections import defaultdict

articles = get_news(limit=50)
print(f"  Fetched {len(articles)} articles from all RSS feeds")
print()

scores: dict = defaultdict(int)
scored_articles = []
for art in articles:
    hl  = (art.get("headline") or "")
    sm  = (art.get("summary")  or "")
    syms = art.get("symbols") or []
    direction = _score_headline(hl, sm)
    label = "BULL" if direction == 1 else "BEAR" if direction == -1 else "NEUT"
    scored_articles.append((label, hl[:80]))
    for sym in syms:
        scores[sym] += direction

# Show sample scored headlines
print("  Sample scored headlines:")
print(f"  {'Label':<6}  Headline")
print(f"  {'─────':<6}  {'─' * 60}")
shown = {"BULL": 0, "BEAR": 0, "NEUT": 0}
for label, hl in scored_articles:
    if shown[label] < 3:
        print(f"  {label:<6}  {hl}")
        shown[label] += 1
    if all(v >= 3 for v in shown.values()):
        break

# Show per-symbol signed result
print()
symbols = ["NVDA", "AAPL", "MSFT", "INTC", "TSLA", "META", "AMZN", "GOOG", "SPY", "SOXL"]
result = {s: scores[s] for s in symbols if s in scores}
if not result:
    print("  ⚠  No symbol-tagged articles locally (Alpaca key lacks news sub) — running demo mode")
else:
    print(f"  {'Symbol':<8} {'Net':>6}  Sentiment")
    print(f"  {'──────':<8} {'───':>6}  {'─' * 30}")
    for sym in symbols:
        score = scores.get(sym, None)
        if score is None:
            print(f"  {sym:<8}   n/a   (not in today's news)")
        elif score > 0:
            print(f"  {sym:<8}  {score:>+4}   BULLISH")
        elif score < 0:
            print(f"  {sym:<8}  {score:>+4}   BEARISH")
        else:
            print(f"  {sym:<8}  {score:>+4}   NEUTRAL (mentioned but mixed)")

# ── 2b. Demo mode: simulate what Railway sees with symbol-tagged articles ────
print()
print("=" * 60)
print("  TEST 2b: DEMO — simulated symbol-tagged articles (like Railway)")
print("=" * 60)

demo_articles = [
    {"headline": "NVDA beats earnings by 20%, raises full-year guidance",      "symbols": ["NVDA"]},
    {"headline": "NVDA wins $5B AI chip contract with hyperscalers",           "symbols": ["NVDA"]},
    {"headline": "NVDA stock momentum — analysts watching closely",            "symbols": ["NVDA"]},
    {"headline": "MSFT Azure revenue accelerating, stock upgrade to Buy",      "symbols": ["MSFT"]},
    {"headline": "MSFT faces antitrust probe into cloud pricing",              "symbols": ["MSFT"]},
    {"headline": "INTC misses revenue estimates, cuts guidance again",         "symbols": ["INTC"]},
    {"headline": "INTC announces layoffs, CEO departure expected",             "symbols": ["INTC"]},
    {"headline": "INTC loses major foundry contract to TSMC",                 "symbols": ["INTC"]},
    {"headline": "AAPL supplier warns of weak iPhone demand in Asia",          "symbols": ["AAPL"]},
    {"headline": "AAPL launches new product line, strong pre-orders",          "symbols": ["AAPL"]},
    {"headline": "SPY holds steady after Fed comments on rates",               "symbols": ["SPY"]},
    {"headline": "META upgrades guidance, ad revenue exceeds expectations",    "symbols": ["META"]},
]

demo_scores: dict = defaultdict(int)
print(f"  {'Article':<65} {'Direction'}")
print(f"  {'───────':<65} {'─────────'}")
for art in demo_articles:
    direction = _score_headline(art["headline"], "")
    label = "+1 BULL" if direction == 1 else "-1 BEAR" if direction == -1 else " 0 NEUT"
    print(f"  {art['headline']:<65}  {label}")
    for sym in art["symbols"]:
        demo_scores[sym] += direction

print()
print(f"  {'Symbol':<8} {'Net':>6}  Result")
print(f"  {'──────':<8} {'───':>6}  {'──────'}")
for sym, score in sorted(demo_scores.items(), key=lambda x: -abs(x[1])):
    tag = "BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL"
    pts = max(-15, min(20, score * 5))
    current = max(0, min(20, len([a for a in demo_articles if sym in a["symbols"]]) * 5))
    print(f"  {sym:<8}  {score:>+4}   {tag}  →  signal={pts:>+4}pts  (current pipeline: {current:>+4}pts always positive)")

# ── 3. Show what the pipeline would inject into signals.py ──────────────────
print()
print("=" * 60)
print("  TEST 3: How scores translate to signal points (×5, cap ±15/+20)")
print("=" * 60)
for sym in symbols:
    score = scores.get(sym, 0)
    pts = max(-15, min(20, score * 5))
    current_pts = max(0, min(20, abs(score) * 5))  # current pipeline (always positive)
    print(f"  {sym:<8}  net={score:>+3}  →  NEW={pts:>+4} pts   (current pipeline would give {current_pts:>+4})")

print()
print("  Done.")

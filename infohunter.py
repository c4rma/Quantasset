#!/usr/bin/env python3
"""
InfoHunter — Quantasset Terminal News Aggregator
Real-time financial headlines, ranked by market impact via rules + AI scoring.
Usage:  python infohunter.py
Deps:   pip install feedparser textual requests
AI key: set ANTHROPIC_API_KEY env var for AI re-scoring (optional but recommended)
"""

import asyncio
import feedparser
import hashlib
import html
import json
import os
import re
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, DataTable, Label, Static, Input
from textual.containers import Vertical, Horizontal
from textual import work
from textual.reactive import reactive
from textual.screen import Screen
from rich.text import Text

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

REFRESH_INTERVAL_SECONDS = 15
WINDOW_HOURS             = 12
MAX_HEADLINES            = 2000
AI_BATCH_SIZE            = 8        # headlines per Ollama call (small models overflow above ~10)
AI_RESCORE_INTERVAL      = 20       # seconds between AI passes
AI_SCORE_ALL             = True     # score every headline when AI enabled
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# ── AI backend selection ─────────────────────────────────────────────────────
# Options:
#   "ollama"     — local Ollama server (free, runs on your machine/phone via Termux)
#   "anthropic"  — Claude Haiku via API (paid, most accurate)
#   "none"       — disable AI scoring entirely
#
# Ollama setup (Termux on Android):
#   pkg install ollama
#   ollama serve &
#   ollama pull qwen2.5:3b        # ~2 GB — good balance of speed + quality for 11 GB RAM
#   Then set AI_BACKEND = "ollama" below.
#
# Recommended models by RAM budget:
#   qwen2.5:3b      ~2 GB  — fast, good reasoning, best for 11 GB
#   qwen2.5:7b      ~5 GB  — better quality, fits in 11 GB
#   llama3.2:3b     ~2 GB  — solid alt to qwen at same size
#   mistral:7b      ~5 GB  — strong reasoning, fits in 11 GB
#   phi3.5          ~2 GB  — Microsoft, very fast on ARM
#
AI_BACKEND         = os.environ.get("INFOHUNTER_AI", "none")   # "ollama" | "anthropic" | "none"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
OLLAMA_HOST        = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL       = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
AI_ENABLED         = AI_BACKEND in ("ollama", "anthropic") and (
    AI_BACKEND != "anthropic" or bool(ANTHROPIC_API_KEY)
)

# ─────────────────────────────────────────────────────────────────────────────
# NEWS SOURCES
# ─────────────────────────────────────────────────────────────────────────────

SOURCES = [
    {"name": "Reuters Business",     "url": "https://feeds.reuters.com/reuters/businessNews",       "category": "MACRO"},
    {"name": "Reuters Finance",      "url": "https://feeds.reuters.com/reuters/financialNews",      "category": "MACRO"},
    {"name": "Reuters Top News",     "url": "https://feeds.reuters.com/reuters/topNews",            "category": "MACRO"},
    {"name": "AP Business",          "url": "https://rsshub.app/apnews/topics/business-news",       "category": "MACRO"},
    {"name": "MarketWatch Top",      "url": "https://feeds.marketwatch.com/marketwatch/topstories/","category": "MARKETS"},
    {"name": "MarketWatch Economy",  "url": "https://feeds.marketwatch.com/marketwatch/economy-politics/", "category": "MACRO"},
    {"name": "WSJ Markets",          "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",        "category": "MARKETS"},
    {"name": "WSJ World",            "url": "https://feeds.a.dj.com/rss/RSSWSJD.xml",               "category": "MACRO"},
    {"name": "CNBC Finance",         "url": "https://search.cnbc.com/rs/search/combinedcombined?partnerId=wrss01&hasCBSi=1&httpHSTS=false&rootExchangeFilter=CNBC&type=cnbcnewsstory&cvurl=http%3A%2F%2Fwww.cnbc.com%2Ffinance%2F", "category": "MARKETS"},
    {"name": "CNBC Economy",         "url": "https://search.cnbc.com/rs/search/combinedcombined?partnerId=wrss01&hasCBSi=1&httpHSTS=false&rootExchangeFilter=CNBC&type=cnbcnewsstory&cvurl=http%3A%2F%2Fwww.cnbc.com%2Feconomy%2F", "category": "MACRO"},
    {"name": "Yahoo Finance",        "url": "https://finance.yahoo.com/rss/topfinstories",          "category": "MARKETS"},
    {"name": "FT Home",              "url": "https://www.ft.com/rss/home/uk",                       "category": "MACRO"},
    {"name": "Bloomberg Markets",    "url": "https://feeds.bloomberg.com/markets/news.rss",         "category": "MARKETS"},
    {"name": "Investing.com Economy","url": "https://www.investing.com/rss/news_25.rss",            "category": "MACRO"},
    {"name": "Investing.com Crypto", "url": "https://www.investing.com/rss/news_301.rss",           "category": "CRYPTO"},
    {"name": "ForexLive",            "url": "https://www.forexlive.com/feed/news",                  "category": "FOREX"},
    {"name": "FXStreet",             "url": "https://www.fxstreet.com/rss/news",                    "category": "FOREX"},
    {"name": "CoinDesk",             "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",      "category": "CRYPTO"},
    {"name": "Cointelegraph",        "url": "https://cointelegraph.com/rss",                        "category": "CRYPTO"},
    {"name": "The Block",            "url": "https://www.theblock.co/rss.xml",                      "category": "CRYPTO"},
    {"name": "Decrypt",              "url": "https://decrypt.co/feed",                              "category": "CRYPTO"},
    {"name": "Bitcoin Magazine",     "url": "https://bitcoinmagazine.com/feed",                     "category": "CRYPTO"},
    {"name": "Fed Reserve",          "url": "https://www.federalreserve.gov/feeds/press_all.xml",   "category": "CB"},
    {"name": "ECB",                  "url": "https://www.ecb.europa.eu/rss/press.html",             "category": "CB"},
    {"name": "IMF News",             "url": "https://www.imf.org/en/News/rss?language=eng",         "category": "CB"},
    {"name": "BIS",                  "url": "https://www.bis.org/rss/press.xml",                    "category": "CB"},
]

# ─────────────────────────────────────────────────────────────────────────────
# RULE-BASED IMPACT SCORING ENGINE
# Each tuple: (pattern, weight, label)   HIGH >= 7  |  MEDIUM 3-6  |  LOW < 3
# ─────────────────────────────────────────────────────────────────────────────

IMPACT_RULES = [

    # ══ CENTRAL BANK / MONETARY POLICY ══════════════════════════════════════
    (r"\bfed(?:eral reserve)?\b.{0,60}\b(rates?|hike|cut|pivot|pause|decision|raises?|lowers?)\b",  8, "Fed rate decision"),
    (r"\b(rates?|hike|cut|pivot|pause|decision|raises?|lowers?)\b.{0,60}\bfed(?:eral reserve)?\b",  8, "Fed rate decision"),
    (r"\b(fomc|federal open market committee)\b",                                                     8, "FOMC"),
    (r"\b(jerome powell|chair powell)\b.{0,50}\b(said|says|warns?|signals?|hints?|speak|speech)\b", 7, "Powell speech"),
    (r"\b(ecb|european central bank)\b.{0,60}\b(rates?|decision|hike|cut|raises?)\b",               7, "ECB rate"),
    (r"\b(boe|bank of england)\b.{0,60}\b(rates?|decision|hike|cut)\b",                             7, "BoE rate"),
    (r"\b(boj|bank of japan)\b.{0,60}\b(rates?|decision|yield|policy|pivot)\b",                     7, "BoJ policy"),
    (r"\b(rba|reserve bank of australia)\b.{0,40}\b(rates?|decision|hike|cut)\b",                   6, "RBA rate"),
    (r"\b(pboc|people.s bank of china)\b.{0,40}\b(rates?|cut|stimulus|policy)\b",                   7, "PBOC"),
    (r"\binterest rates? (decision|hike|cut|hold|pause|raise|lower|increase|decrease)\b",            7, "Rate decision"),
    (r"\b(raises?|hikes?|cuts?|lowers?)\b.{0,20}\binterest rates?\b",                               7, "Rate decision"),
    (r"\bquantitative (tightening|easing)\b|\b(qt|qe)\b.{0,20}\b(begin|end|extend|pause)\b",        6, "QT/QE"),
    (r"\byield curve (control|inversion|inverts?|steepen)\b",                                        6, "Yield curve"),
    (r"\b(hawkish|dovish)\b.{0,30}\b(fed|ecb|boe|boj|central bank)\b",                              5, "CB tone shift"),
    (r"\bfed speak\b",                                                                               5, "Fed speak"),

    # ══ MACRO DATA RELEASES ══════════════════════════════════════════════════
    (r"\b(cpi|consumer price index)\b",                                                              7, "CPI"),
    (r"\b(pce|personal consumption expenditure)\b",                                                  7, "PCE"),
    (r"\b(ppi|producer price index)\b",                                                              5, "PPI"),
    (r"\bnon.?farm payrolls?\b",                                                                     8, "NFP"),
    (r"\bnfp\b",                                                                                     8, "NFP"),
    (r"\bjobs? report\b",                                                                            7, "Jobs report"),
    (r"\bunemployment (rate|claims|data|rises?|falls?)\b",                                           6, "Unemployment"),
    (r"\binitial (jobless )?claims\b",                                                               5, "Jobless claims"),
    (r"\bgdp (data|growth|contraction|contracts?|print|report|miss|beat|shrinks?|expands?)\b",       7, "GDP"),
    (r"\b(gdp|gross domestic product)\b.{0,50}\b(contraction|contracts?|shrinks?|miss|beat|falls?|drops?|surges?)\b", 7, "GDP"),
    (r"\b(retail sales|consumer spending)\b",                                                        5, "Consumer data"),
    (r"\b(ism|pmi)\b.{0,30}\b(manufacturing|services|composite)\b",                                 5, "ISM/PMI"),
    (r"\bjolts\b",                                                                                   5, "JOLTS"),
    (r"\b(trade deficit|trade balance)\b",                                                           5, "Trade data"),
    (r"\b(core inflation|headline inflation)\b",                                                     6, "Inflation print"),
    (r"\binflation (surges?|spikes?|jumps?|rises?|falls?|cools?|drops?|unexpectedly)\b",             6, "Inflation move"),
    (r"\b(durable goods|housing starts|building permits)\b",                                         4, "Housing/Durables"),
    (r"\b(consumer confidence|sentiment index|michigan sentiment)\b",                                4, "Sentiment data"),

    # ══ MARKET EXTREMES ══════════════════════════════════════════════════════
    (r"\b(crash|collapse|meltdown|black swan|flash crash)\b",                                        9, "Market crash"),
    (r"\b(circuit breaker|trading halt|market halt)\b",                                              9, "Circuit breaker"),
    (r"\b(bank run|bank failure|bank collapse|bank seized)\b",                                       9, "Bank failure"),
    (r"\b(systemic risk|contagion|financial crisis|liquidity crisis)\b",                             8, "Systemic risk"),
    (r"\b(recession|depression|stagflation|technical recession)\b",                                  7, "Recession"),
    (r"\b(bear market|bull run|capitulation)\b",                                                     5, "Market trend"),
    (r"\b(record high|all.?time high|ath)\b",                                                        5, "Record high"),
    (r"\brecord low\b",                                                                              5, "Record low"),
    (r"\bmarket (rout|selloff|sell.off|rally|surge|plunge|plunges?|tumbles?|rips?)\b",               5, "Market move"),
    (r"\b(margin call|forced liquidation|deleveraging)\b",                                           7, "Margin/Deleveraging"),
    (r"\b(volatility spike|vix (surge|spike|soar|jump))\b",                                         6, "VIX spike"),
    (r"\byield (spike|surge|soar|jump|invert)\b",                                                    6, "Yield move"),

    # ══ GEOPOLITICAL / ENERGY / MACRO SHOCK ══════════════════════════════════
    (r"\b(war|warfare|invasion|invades?|invaded)\b",                                                 9, "War/Invasion"),
    (r"\b(military strike|airstrike|air strike|bombing|bombed|shelled|attacked)\b",                 8, "Military strike"),
    (r"\b(nuclear|nuke|missile|ballistic)\b.{0,40}\b(launch|threat|test|strike|attack)\b",          9, "Nuclear/Missile"),
    (r"\b(terrorist|terrorism)\b.{0,40}\b(market|financial|infrastructure|pipeline|port|attack)\b", 8, "Terror attack"),
    (r"\b(strait of hormuz|hormuz|bab el.?mandeb|suez canal|panama canal)\b",                       8, "Chokepoint disruption"),
    (r"\b(oil tanker|tanker attack|tanker seized|tanker struck|tanker hit)\b",                       8, "Tanker attack"),
    (r"\b(ship|vessel|cargo|tanker)\b.{0,40}\b(attack|seized|struck|hit|sunk|boarded|fired upon)\b", 7, "Vessel attack"),
    (r"\b(houthi|houthis)\b.{0,80}\b(attack|strike|missile|drone|vessel|tanker|ship|fired|launched|hits?|targets?)\b", 8, "Houthi attack"),
    (r"\b(red sea|gulf of aden|arabian sea)\b.{0,50}\b(attack|strike|missile|drone|disruption|closure|reroute|risk)\b", 8, "Red Sea disruption"),
    (r"\b(pipeline)\b.{0,30}\b(attack|explosion|explodes?|rupture|shut|closure|sabotage)\b",        8, "Pipeline disruption"),
    (r"\b(iran|hezbollah|hamas)\b.{0,60}\b(attack|strike|missile|drone|vessel|tanker|ship|fired|launched)\b", 8, "Iran/Proxy attack"),
    (r"\b(russia|ukraine)\b.{0,40}\b(attack|strike|escalat|offensive|ceasefire|peace|nuclear)\b",   7, "Russia/Ukraine"),
    (r"\b(taiwan|china)\b.{0,40}\b(military|invasion|blockade|strait|conflict|tension|drills?)\b",  8, "Taiwan/China tension"),
    (r"\b(north korea|dprk)\b.{0,40}\b(missile|nuclear|launch|test|provoc)\b",                      8, "DPRK threat"),
    (r"\b(coup|regime change|government collapse|political crisis)\b",                               7, "Political shock"),
    (r"\b(sanctions|embargo|export ban|import ban)\b",                                               7, "Sanctions"),
    (r"\b(tariff|trade war|trade deal|trade agreement)\b",                                           6, "Trade policy"),
    (r"\b(opec|opec\+)\b.{0,40}\b(cut|reduce|increase|production|output|meeting|decision|surprise)\b", 8, "OPEC decision"),
    (r"\b(oil supply|crude supply|oil disruption|oil embargo)\b",                                    7, "Oil supply shock"),
    (r"\b(energy crisis|power crisis|gas shortage|fuel shortage)\b",                                 8, "Energy crisis"),
    (r"\bnatural gas\b.{0,30}\b(spike|surge|shortage|crisis|record)\b",                             7, "Nat gas crisis"),
    (r"\b(supply chain disruption|supply shock|port closure|shipping halt|shipping disruption)\b",   6, "Supply shock"),
    (r"\b(default|sovereign debt crisis)\b",                                                         8, "Sovereign default"),
    (r"\bdebt ceiling\b",                                                                            7, "Debt ceiling"),
    (r"\bgovernment shutdown\b",                                                                     6, "Gov shutdown"),
    (r"\b(election|electoral)\b.{0,40}\b(result|win|upset|landslide|stolen|disputed|runoff)\b",     6, "Election result"),
    (r"\b(earthquake|tsunami|hurricane|tornado|catastrophic)\b.{0,40}\b(major|devastating|kills?|dead|\d+ dead)\b", 6, "Natural disaster"),
    (r"\b(pandemic|outbreak|epidemic)\b.{0,30}\b(declared|spreading|confirmed|global)\b",           7, "Pandemic/Outbreak"),

    # ══ CRYPTO-SPECIFIC ══════════════════════════════════════════════════════
    (r"\b(sec|cftc|doj|treasury)\b.{0,40}\b(bitcoin|ethereum|crypto|btc|eth|defi|nft)\b",           7, "Crypto regulation"),
    (r"\b(bitcoin|btc)\b.{0,40}\b(etf|approval|rejected?|spot etf)\b",                              8, "BTC ETF"),
    (r"\b(ethereum|eth)\b.{0,40}\b(etf|upgrade|dencun|cancun|merge|fork)\b",                        7, "ETH event"),
    (r"\b(exchange hack|crypto hack|protocol hack|bridge hack|defi hack|exploit)\b",                8, "Crypto hack"),
    (r"\b(hack|exploit|stolen|drained|vulnerability)\b.{0,50}(?:\$[\d,.]+\s*(?:m|million|b|billion)|[\d,.]+\s*(?:million|billion))", 8, "Crypto hack"),
    (r"\b(stablecoin|tether|usdc|usdt|dai|frax)\b.{0,40}\b(depegs?|collapses?|pauses?|halts?)\b",  9, "Stablecoin crisis"),
    (r"\bdepegs?\b.{0,20}\b(tether|usdt|usdc|stablecoin|dai)\b",                                    9, "Stablecoin crisis"),
    (r"\b(ftx|binance|coinbase|kraken|bybit|okx)\b.{0,40}\b(bankrupt|ban|sued|charged?|fined?|seized?|halted?)\b", 8, "Exchange legal"),
    (r"\b(bitcoin halving|btc halving|halving)\b",                                                   7, "BTC halving"),
    (r"\b(liquidation|long squeeze|short squeeze|liq cascade)\b",                                    6, "Liquidation"),
    (r"\bopen interest\b.{0,30}\b(record|surge|spike)\b",                                           5, "OI spike"),
    (r"\b(crypto ban|blanket ban|crypto crackdown)\b",                                               8, "Crypto ban"),

    # ══ CORPORATE / EARNINGS ═════════════════════════════════════════════════
    (r"\b(bankruptcy|chapter 11|chapter 7|insolvent|insolvency)\b",                                  8, "Bankruptcy"),
    (r"\b(earnings miss|earnings beat|eps miss|eps beat|revenue miss|revenue beat)\b",               5, "Earnings"),
    (r"\b(merger|acquisition|buyout|takeover)\b.{0,40}\b(\$[\d,.]+\s*billion|\d+\s*billion)\b",     6, "Major M&A"),
    (r"\b(layoffs?|mass layoff|workforce reduction|job cuts?)\b.{0,30}\b(\d{1,3},?\d{3}|\d+%|thousand)\b", 5, "Layoffs"),
    (r"\b(ipo|initial public offering)\b",                                                           4, "IPO"),
    (r"\b(credit rating|downgrade|junk status)\b.{0,30}\b(moodys?|s&p|fitch)\b",                   7, "Credit downgrade"),
    (r"\b(fraud|accounting fraud|ponzi|embezzlement)\b.{0,30}\b(billion|million)\b",                7, "Corporate fraud"),

    # ══ SCALE BOOSTS ═════════════════════════════════════════════════════════
    (r"\$[\d,]+\s*trillion|\b\d+\s*trillion\b",                                                      4, "Trillion-scale"),
    (r"\$[\d,]+\s*billion|\b\d+\s*billion\b",                                                        2, "Billion-scale"),

    # ══ NOISE SUPPRESSORS ════════════════════════════════════════════════════
    (r"\b(sports?|nfl|nba|nhl|mlb|nascar|cricket|soccer goal|rugby)\b",                            -6, "Noise: sports"),
    (r"\b(celebrity|entertainment|movie|award|oscar|grammy|emmy|kardashian|taylor swift)\b",        -6, "Noise: entertainment"),
    (r"\b(horoscope|recipe|travel tip|lifestyle|beauty|fashion week)\b",                            -6, "Noise: lifestyle"),
    (r"\b(weekly (wrap|recap)|market (wrap|recap)|week in review)\b",                               -2, "Noise: recap"),
    (r"\banalyst (upgrades?|reiterates?) .{0,20}\b(buy|hold|outperform)\b(?!.{0,30}(?:downgrade|cut|sell))", -2, "Noise: routine rating"),
]

_COMPILED = [
    (re.compile(p, re.IGNORECASE | re.DOTALL), w, lbl)
    for p, w, lbl in IMPACT_RULES
]

CATEGORY_BOOST = {"CB": 4, "MACRO": 1, "FOREX": 1, "MARKETS": 0, "CRYPTO": 0}
HIGH_THRESHOLD, MEDIUM_THRESHOLD = 7, 3


def rule_score(title: str, summary: str, category: str) -> tuple[int, str, list[str]]:
    text   = f"{title} {summary}"
    score  = CATEGORY_BOOST.get(category, 0)
    matched: list[str] = []
    for pattern, weight, label in _COMPILED:
        if pattern.search(text):
            score += weight
            if weight > 0:
                matched.append(label)
    score = max(0, score)
    level = "HIGH" if score >= HIGH_THRESHOLD else ("MEDIUM" if score >= MEDIUM_THRESHOLD else "LOW")
    return score, level, matched


# ─────────────────────────────────────────────────────────────────────────────
# AI RESCORING
# ─────────────────────────────────────────────────────────────────────────────

AI_SYSTEM = (
    "You are a financial news impact classifier. Output ONLY valid JSON — no prose, no markdown.\n"
    "HIGH:   CB rate decisions, NFP/CPI/GDP prints, market crashes, wars, strait/chokepoint attacks, "
    "oil tanker attacks, pipeline sabotage, nuclear threats, OPEC surprises, bank failures, "
    "stablecoin depegs, major crypto hacks, sovereign defaults, pandemic declarations.\n"
    "MEDIUM: Fed-speak, PMI data, earnings beats/misses, M&A, geopolitical tension without immediate "
    "market impact, regulatory proposals, layoffs without systemic implications.\n"
    "LOW:    Routine company news, analyst opinions, price target changes, recaps, lifestyle.\n\n"
    "Respond with a JSON array ONLY — no prose, no markdown, no explanation before or after.\n"
    '[{"id":"abc123","impact":"HIGH","reason":"FOMC rate hike"},{"id":"def456","impact":"LOW","reason":"routine note"}]'
)


def _parse_ai_response(raw: str) -> dict[str, tuple[str, str]]:
    """
    Robustly extract a JSON array from AI output.
    Handles: markdown fences, prose preamble, nested objects, partial output.
    """
    # Strip markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    # Find the first "[" and the matching closing "]" by bracket counting
    # (avoids the greedy-regex trap of matching [..."id"...] across items)
    start = raw.find("[")
    if start == -1:
        raise ValueError(f"No JSON array found in AI response: {raw[:200]!r}")

    depth = 0
    end   = -1
    for i, ch in enumerate(raw[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        raise ValueError(f"Unclosed JSON array in AI response: {raw[start:start+200]!r}")

    array_str = raw[start:end]
    data = json.loads(array_str)

    result = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        hid    = str(item.get("id", "")).strip()
        impact = str(item.get("impact", "")).upper().strip()
        reason = str(item.get("reason", "")).strip()
        if hid and impact in ("HIGH", "MEDIUM", "LOW"):
            result[hid] = (impact, reason)
    return result


def _ai_via_anthropic(prompt: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "system": AI_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def _ai_via_ollama(prompt: str) -> str:
    resp = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 1024},
            "messages": [
                {"role": "system", "content": AI_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
        },
        timeout=60,   # local models can be slower
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def ai_rescore_batch(headlines: list, store: "HeadlineStore | None" = None) -> dict[str, tuple[str, str]]:
    if not AI_ENABLED or not headlines:
        return {}
    payload = []
    for h in headlines:
        entry = {"id": h.id, "title": h.title}
        if h.summary:
            entry["summary"] = h.summary[:120]  # small models have limited context
        payload.append(entry)
    prompt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        if AI_BACKEND == "anthropic":
            raw = _ai_via_anthropic(prompt)
        elif AI_BACKEND == "ollama":
            raw = _ai_via_ollama(prompt)
        else:
            return {}
        result = _parse_ai_response(raw)
        if store:
            store.last_ai_error = ""
        return result
    except Exception as exc:
        err = str(exc)[:120]
        if store:
            store.last_ai_error = err
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Headline:
    id:         str
    timestamp:  datetime
    source:     str
    category:   str
    impact:     str
    score:      int
    title:      str
    url:        str
    summary:    str       = ""
    tags:       list[str] = field(default_factory=list)
    ai_scored:  bool      = False
    ai_reason:  str       = ""

    @property
    def ts_str(self) -> str:
        return self.timestamp.astimezone().strftime("%H:%M:%S")

    @property
    def date_str(self) -> str:
        return self.timestamp.astimezone().strftime("%m/%d")


# ─────────────────────────────────────────────────────────────────────────────
# FEED FETCHER
# ─────────────────────────────────────────────────────────────────────────────

def _make_id(url: str, title: str) -> str:
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()[:16]

def _parse_time(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split())

def fetch_source(source: dict) -> list[Headline]:
    results = []
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    try:
        resp = requests.get(source["url"], timeout=12, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:
        try:
            feed = feedparser.parse(source["url"])
        except Exception:
            return results

    for entry in feed.entries:
        title = _clean(entry.get("title", ""))
        if not title or len(title) < 10:
            continue
        url = entry.get("link", "")
        if not url:
            continue
        summary  = _clean(entry.get("summary", entry.get("description", "")))
        pub_time = _parse_time(entry)
        if pub_time < cutoff:
            continue
        score, impact, tags = rule_score(title, summary, source["category"])
        results.append(Headline(
            id=_make_id(url, title),
            timestamp=pub_time, source=source["name"],
            category=source["category"], impact=impact,
            score=score, title=title, url=url,
            summary=summary, tags=tags,
        ))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# HEADLINE STORE  (thread-safe)
# ─────────────────────────────────────────────────────────────────────────────

class HeadlineStore:
    def __init__(self):
        self._lock      = threading.Lock()
        self._data: dict[str, Headline] = {}
        self.last_ai_error: str = ""   # surface errors in the UI

    def add_many(self, items: list[Headline]) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
        added  = 0
        with self._lock:
            for h in items:
                if h.id not in self._data:
                    self._data[h.id] = h
                    added += 1
            self._data = {k: v for k, v in self._data.items() if v.timestamp >= cutoff}
        return added

    def apply_ai_scores(self, updates: dict[str, tuple[str, str]]) -> None:
        with self._lock:
            for hid, (impact, reason) in updates.items():
                if hid in self._data:
                    self._data[hid].impact    = impact
                    self._data[hid].ai_scored = True
                    self._data[hid].ai_reason = reason

    def get_unscored(self) -> list[Headline]:
        """Return ALL un-AI-scored headlines, newest first."""
        with self._lock:
            candidates = [h for h in self._data.values() if not h.ai_scored]
        candidates.sort(key=lambda h: h.timestamp, reverse=True)
        return candidates

    def get_sorted(
        self,
        filter_impact:   Optional[str] = None,
        filter_category: Optional[str] = None,
        search: str = "",
    ) -> list[Headline]:
        with self._lock:
            items = list(self._data.values())
        if filter_impact:
            items = [h for h in items if h.impact == filter_impact]
        if filter_category:
            items = [h for h in items if h.category == filter_category]
        if search:
            sq = search.lower()
            items = [h for h in items
                     if sq in h.title.lower() or sq in h.source.lower() or sq in h.category.lower()]
        items.sort(key=lambda h: (h.timestamp, h.score), reverse=True)
        return items[:MAX_HEADLINES]

    def counts(self) -> dict:
        with self._lock:
            total  = len(self._data)
            high   = sum(1 for h in self._data.values() if h.impact == "HIGH")
            medium = sum(1 for h in self._data.values() if h.impact == "MEDIUM")
            ai_cnt = sum(1 for h in self._data.values() if h.ai_scored)
        return {"total": total, "high": high, "medium": medium,
                "low": total - high - medium, "ai": ai_cnt}


# ─────────────────────────────────────────────────────────────────────────────
# VISUAL CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

IMPACT_STYLE = {
    "HIGH":   ("bold red",  "bold red"),
    "MEDIUM": ("yellow",    "yellow"),
    "LOW":    ("dim",       "dim"),
}
CATEGORY_STYLE = {
    "CB":      "bold magenta",
    "MACRO":   "cyan",
    "FOREX":   "blue",
    "MARKETS": "green",
    "CRYPTO":  "bright_yellow",
}


def _esc(text: str) -> str:
    """Escape Rich markup special characters in arbitrary strings."""
    return text.replace("\\", "\\\\").replace("[", r"\[")


# ─────────────────────────────────────────────────────────────────────────────
# SCREENS
# ─────────────────────────────────────────────────────────────────────────────

class DetailScreen(Screen):
    """Article detail pop-up — all user content fully escaped before rendering."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q",      "dismiss", "Close"),
    ]

    def __init__(self, headline: Headline) -> None:
        super().__init__()
        self.h = headline

    def compose(self) -> ComposeResult:
        h   = self.h
        imp = IMPACT_STYLE.get(h.impact, ("white", "white"))[0]
        cat = CATEGORY_STYLE.get(h.category, "white")

        tags_str = _esc(", ".join(h.tags)) if h.tags else "—"
        summary  = _esc(h.summary) if h.summary else "(no summary available)"
        url_e    = _esc(h.url)
        title_e  = _esc(h.title)
        source_e = _esc(h.source)

        ai_line = f"\n[dim]AI note:[/dim]  {_esc(h.ai_reason)}" if h.ai_scored else ""

        content = (
            f"[bold]{title_e}[/bold]\n\n"
            f"[{imp}]⬥ IMPACT: {h.impact}  (score: {h.score})[/{imp}]   "
            f"[{cat}]▸ {h.category}[/{cat}]\n\n"
            f"[dim]Source:[/dim]  {source_e}\n"
            f"[dim]Time:[/dim]    {h.date_str}  {h.ts_str}\n"
            f"[dim]Tags:[/dim]    {tags_str}"
            f"{ai_line}\n\n"
            f"[dim]Summary:[/dim]\n{summary}\n\n"
            f"[dim]URL:[/dim]\n{url_e}\n\n"
            f"[dim italic]ESC or Q to return[/dim italic]"
        )
        yield Vertical(Static(content, id="detail-body"), id="detail-panel")

    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    #detail-panel {
        width: 90%; max-width: 115;
        height: auto; max-height: 90%;
        background: $surface;
        border: round $accent;
        padding: 2 4;
        overflow-y: auto;
    }
    #detail-body { width: 100%; }
    """


class SearchScreen(Screen):
    """Search overlay — uses dismiss(value) via a callback, no worker needed."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                "[bold]Search headlines[/bold]\n"
                "Type to filter · Enter to apply · ESC to cancel",
                id="srch-title",
            ),
            Input(placeholder="e.g.  fed rate  /  bitcoin  /  iran  /  tanker", id="srch-input"),
            id="srch-panel",
        )

    def on_mount(self) -> None:
        self.query_one("#srch-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")

    DEFAULT_CSS = """
    SearchScreen { align: center middle; }
    #srch-panel {
        width: 64; height: auto;
        background: $surface; border: round $accent;
        padding: 2 4;
    }
    #srch-title { margin-bottom: 1; }
    """


class HelpScreen(Screen):
    BINDINGS = [Binding("escape,q,h,question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        if AI_BACKEND == "ollama" and AI_ENABLED:
            ai_status = f"[green]✓ ACTIVE[/green]  Ollama / {OLLAMA_MODEL}"
        elif AI_BACKEND == "anthropic" and AI_ENABLED:
            ai_status = "[green]✓ ACTIVE[/green]  claude-haiku (Anthropic API)"
        else:
            ai_status = (
                "[dim]DISABLED — set env var INFOHUNTER_AI=ollama  "
                "(or INFOHUNTER_AI=anthropic + ANTHROPIC_API_KEY)[/dim]"
            )
        text = f"""[bold cyan]InfoHunter — Quantasset Market Intelligence[/bold cyan]

[bold]KEYBINDINGS[/bold]
  [yellow]↑ / ↓ / PgUp / PgDn[/yellow]   Scroll
  [yellow]Enter[/yellow]                  Open detail view
  [yellow]R[/yellow]                      Force refresh now
  [yellow]F[/yellow]                      Cycle impact filter  ALL → HIGH → MEDIUM → LOW
  [yellow]C[/yellow]                      Cycle category       ALL → CB → MACRO → FOREX → MARKETS → CRYPTO
  [yellow]S[/yellow]                      Open search
  [yellow]ESC[/yellow]                    Clear all filters
  [yellow]Q[/yellow]                      Quit

[bold]IMPACT LEVELS[/bold]
  [bold red]HIGH[/bold red]      FOMC/CB decisions · NFP/CPI/GDP prints · war/invasion · strait attacks
             tanker/pipeline strikes · OPEC surprises · bank failures · stablecoin depegs
  [yellow]MEDIUM[/yellow]    Fed-speak · PMI · earnings · M&A · geopolitical tension · regulatory news
  [dim]LOW[/dim]       Routine company news · analyst ratings · recaps · lifestyle

[bold]AI SCORING[/bold]
  {ai_status}
  Ambiguous headlines (score 2–10) are batched every {AI_RESCORE_INTERVAL}s and re-evaluated
  by Claude for nuanced events that pure keyword rules may miss.
  AI-scored headlines are marked with [bold]✦[/bold] in the IMP column.

[bold]STATS[/bold]  {len(SOURCES)} sources  │  12-hour window  │  {REFRESH_INTERVAL_SECONDS}s refresh

[dim]ESC / Q / H to close[/dim]"""
        yield Vertical(Static(text, id="help-body"), id="help-panel")

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-panel {
        width: 86; height: auto; max-height: 92%;
        background: $surface; border: round $accent;
        padding: 2 4; overflow-y: auto;
    }
    """


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────────────────────────

class InfoHunter(App):
    TITLE     = "InfoHunter"
    SUB_TITLE = "Quantasset Market Intelligence"

    CSS = """
    Screen { background: $background; }
    #toolbar {
        height: 1; background: $panel; padding: 0 2; color: $text-muted;
    }
    #toolbar Label { margin-right: 2; }
    #status-bar {
        height: 1; background: $panel; padding: 0 2; color: $text-muted;
    }
    DataTable { height: 1fr; }
    DataTable > .datatable--header { background: $surface; color: $text; text-style: bold; }
    DataTable > .datatable--cursor { background: $accent 40%; }
    """

    BINDINGS = [
        Binding("r",             "refresh",               "Refresh"),
        Binding("f",             "cycle_impact",          "Impact filter"),
        Binding("c",             "cycle_category",        "Cat filter"),
        Binding("s",             "search",                "Search"),
        Binding("enter",         "open_detail",           "Detail"),
        Binding("escape",        "clear_filters",         "Clear"),
        Binding("q",             "quit",                  "Quit"),
        Binding("h",             "help",                  "Help"),
        Binding("question_mark", "help",                  "Help", show=False),
    ]

    IMPACT_CYCLE   = [None, "HIGH", "MEDIUM", "LOW"]
    CATEGORY_CYCLE = [None, "CB", "MACRO", "FOREX", "MARKETS", "CRYPTO"]

    filter_impact:   reactive[Optional[str]] = reactive(None)
    filter_category: reactive[Optional[str]] = reactive(None)
    search_query:    reactive[str]           = reactive("")

    def __init__(self) -> None:
        super().__init__()
        self.store           = HeadlineStore()
        self._rows:  list[Headline] = []
        self._refresh_ts     = "—"
        self._countdown      = REFRESH_INTERVAL_SECONDS

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            Label("[bold cyan]IMPACT:[/bold cyan] ALL", id="lbl-impact"),
            Label("[bold cyan]CAT:[/bold cyan] ALL",    id="lbl-cat"),
            Label("[bold cyan]SEARCH:[/bold cyan] —",   id="lbl-search"),
            Label("",                                   id="lbl-fetch"),
            id="toolbar",
        )
        tbl = DataTable(id="tbl", cursor_type="row", zebra_stripes=True)
        tbl.add_columns("TIME", "IMP", "CAT", "SOURCE", "HEADLINE")
        yield tbl
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_table()
        self.fetch_all()
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.fetch_all)
        self.set_interval(AI_RESCORE_INTERVAL,      self.ai_rescore)
        self.set_interval(1,                        self._tick)

    def on_worker_state_changed(self, event) -> None:
        # Kick off AI scoring as soon as the first fetch worker finishes
        from textual.worker import WorkerState
        if event.state == WorkerState.SUCCESS and event.worker.name == "fetch_all":
            if AI_ENABLED:
                self.ai_rescore()

    # ── Workers ───────────────────────────────────────────────────────────────

    @work(thread=True, exclusive=False)
    def fetch_all(self) -> None:
        self.call_from_thread(self._set_fetch, "⟳ fetching…")
        for src in SOURCES:
            try:
                self.store.add_many(fetch_source(src))
            except Exception:
                pass
        ts = datetime.now().strftime("%H:%M:%S")
        self._refresh_ts = ts
        self._countdown  = REFRESH_INTERVAL_SECONDS
        self.call_from_thread(self._set_fetch, f"✓ {ts}")
        self.call_from_thread(self._rebuild_table)

    @work(thread=True, exclusive=True)
    def ai_rescore(self) -> None:
        if not AI_ENABLED:
            return
        unscored = self.store.get_unscored()
        if not unscored:
            return

        # Chunk into batches and score them in parallel (max 3 concurrent calls)
        batches = [unscored[i:i + AI_BATCH_SIZE] for i in range(0, len(unscored), AI_BATCH_SIZE)]
        any_updates = False
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(ai_rescore_batch, batch, self.store): batch for batch in batches}
            for future in as_completed(futures):
                try:
                    updates = future.result(timeout=90)
                    if updates:
                        self.store.apply_ai_scores(updates)
                        any_updates = True
                        self.call_from_thread(self._rebuild_table)
                except Exception:
                    pass
        if any_updates:
            self.call_from_thread(self._rebuild_table)

    # ── Table ─────────────────────────────────────────────────────────────────

    def _rebuild_table(self) -> None:
        self._rows = self.store.get_sorted(
            filter_impact=self.filter_impact,
            filter_category=self.filter_category,
            search=self.search_query,
        )
        tbl = self.query_one("#tbl", DataTable)
        tbl.clear()

        for h in self._rows:
            cell_style, row_style = IMPACT_STYLE.get(h.impact, ("white", "white"))
            ai_mark     = "✦" if h.ai_scored else " "
            time_cell   = Text(f"{h.date_str} {h.ts_str}", style="dim")
            impact_cell = Text(f"{ai_mark}{h.impact}", style=cell_style)
            cat_cell    = Text(h.category, style=CATEGORY_STYLE.get(h.category, "white"))
            src_cell    = Text(h.source[:22], style="dim cyan")
            title_disp  = h.title[:108] + ("…" if len(h.title) > 108 else "")
            title_cell  = Text(title_disp, style=row_style)
            tbl.add_row(time_cell, impact_cell, cat_cell, src_cell, title_cell)

        self._update_toolbar()
        self._update_status()

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_fetch(self, text: str) -> None:
        try:
            self.query_one("#lbl-fetch", Label).update(text)
        except Exception:
            pass

    def _update_toolbar(self) -> None:
        try:
            self.query_one("#lbl-impact", Label).update(
                f"[bold cyan]IMPACT:[/bold cyan] {self.filter_impact or 'ALL'}")
            self.query_one("#lbl-cat", Label).update(
                f"[bold cyan]CAT:[/bold cyan] {self.filter_category or 'ALL'}")
            self.query_one("#lbl-search", Label).update(
                f"[bold cyan]SEARCH:[/bold cyan] {self.search_query or '—'}")
        except Exception:
            pass

    def _update_status(self) -> None:
        try:
            c      = self.store.counts()
            if AI_ENABLED:
                err = self.store.last_ai_error
                if err:
                    ai_bit = f"  [bold red]AI ERR: {_esc(err[:60])}[/bold red]"
                else:
                    pending = c['total'] - c['ai']
                    ai_bit = (
                        f"  [dim]AI:{c['ai']}✦[/dim]"
                        + (f"  [yellow]pending:{pending}[/yellow]" if pending else "")
                    )
            else:
                ai_bit = ""
            self.query_one("#status-bar", Static).update(
                f" {c['total']} headlines (12h)  │ "
                f"[bold red]HIGH:{c['high']}[/bold red]  "
                f"[yellow]MED:{c['medium']}[/yellow]  "
                f"LOW:{c['low']}{ai_bit}  │  "
                f"Showing:{len(self._rows)}  │  "
                f"Next refresh:{self._countdown}s"
            )
        except Exception:
            pass

    def _tick(self) -> None:
        self._countdown = max(0, self._countdown - 1)
        self._update_status()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.fetch_all()

    def action_cycle_impact(self) -> None:
        cycle = self.IMPACT_CYCLE
        idx   = cycle.index(self.filter_impact) if self.filter_impact in cycle else 0
        self.filter_impact = cycle[(idx + 1) % len(cycle)]
        self._rebuild_table()

    def action_cycle_category(self) -> None:
        cycle = self.CATEGORY_CYCLE
        idx   = cycle.index(self.filter_category) if self.filter_category in cycle else 0
        self.filter_category = cycle[(idx + 1) % len(cycle)]
        self._rebuild_table()

    def action_clear_filters(self) -> None:
        self.filter_impact   = None
        self.filter_category = None
        self.search_query    = ""
        self._rebuild_table()

    def action_search(self) -> None:
        def _on_search_dismiss(result: str) -> None:
            # Called on the main thread by Textual after dismiss()
            if result:  # empty string = cancelled
                self.search_query = result
            self._rebuild_table()

        self.push_screen(SearchScreen(), callback=_on_search_dismiss)

    def action_open_detail(self) -> None:
        tbl = self.query_one("#tbl", DataTable)
        if tbl.cursor_row is not None and tbl.cursor_row < len(self._rows):
            self.push_screen(DetailScreen(self._rows[tbl.cursor_row]))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # ── Events ────────────────────────────────────────────────────────────────

    def on_data_table_row_selected(self, _: DataTable.RowSelected) -> None:
        self.action_open_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is not None and event.cursor_row < len(self._rows):
            h = self._rows[event.cursor_row]
            try:
                url_display = h.url[:88] + ("…" if len(h.url) > 88 else "")
                self.query_one("#status-bar", Static).update(
                    f" [bold]{_esc(h.source)}[/bold]  "
                    f"score:{h.score}  "
                    f"tags:{_esc(', '.join(h.tags)) if h.tags else '—'}  │  "
                    f"{_esc(url_display)}"
                )
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _silence_ollama_logs() -> None:
    """
    Ollama prints GIN HTTP server logs to stdout/stderr which bleed into
    the Textual UI. We redirect both at the OS file-descriptor level so
    nothing can sneak through, even from C extensions.
    """
    import sys, os
    if AI_BACKEND != "ollama":
        return
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        # Flush before redirecting
        sys.stdout.flush()
        sys.stderr.flush()
        # Duplicate the real stdout so Textual can still use it internally
        # (Textual writes to the terminal via its own fd, not sys.stdout)
        os.dup2(devnull_fd, 2)   # silence stderr (GIN logs go here)
        os.close(devnull_fd)
    except Exception:
        pass  # non-fatal — worst case logs still show but app still works


if __name__ == "__main__":
    import sys

    missing = []
    for mod in ("feedparser", "textual", "requests"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        print(f"[InfoHunter] Missing dependencies: {', '.join(missing)}")
        print(f"Run:  pip install {' '.join(missing)}")
        sys.exit(1)

    if AI_BACKEND == "ollama":
        print(f"[InfoHunter] AI scoring: ENABLED  (Ollama / {OLLAMA_MODEL} @ {OLLAMA_HOST})")
        print(f"[InfoHunter] TIP: Start Ollama silently with:  ollama serve 2>/dev/null &")
    elif AI_BACKEND == "anthropic" and AI_ENABLED:
        print("[InfoHunter] AI scoring: ENABLED  (claude-haiku)")
    else:
        print("[InfoHunter] AI scoring: DISABLED")
        print("[InfoHunter] To enable: set INFOHUNTER_AI=ollama (or anthropic + ANTHROPIC_API_KEY)")

    _silence_ollama_logs()
    InfoHunter().run()

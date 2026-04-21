#!/usr/bin/env python3
"""
InfoHunter — Quantasset Terminal News Aggregator
Real-time financial headlines, ranked by market impact.
Usage: python3 infohunter.py
Requires: pip install feedparser textual requests
"""

import asyncio
import feedparser
import hashlib
import html
import re
import requests
import threading
import time

from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Header, Footer, DataTable, Label, Static
)
from textual.containers import Vertical, Horizontal
from textual import work
from textual.reactive import reactive
from textual.screen import Screen
from rich.text import Text

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

REFRESH_INTERVAL_SECONDS = 90       # Poll feeds every N seconds
WINDOW_HOURS = 12                   # Keep last N hours of headlines
MAX_HEADLINES = 2000                # Hard cap to avoid memory bloat
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────
# NEWS SOURCES  (all free RSS)
# ─────────────────────────────────────────────

SOURCES = [
    # Macro / Economy
    {
        "name": "Reuters Business",
        "url": "https://feeds.reuters.com/reuters/businessNews",
        "category": "MACRO",
    },
    {
        "name": "Reuters Finance",
        "url": "https://feeds.reuters.com/reuters/financialNews",
        "category": "MACRO",
    },
    {
        "name": "Reuters Top",
        "url": "https://feeds.reuters.com/reuters/topNews",
        "category": "MACRO",
    },
    {
        "name": "AP Business",
        "url": "https://rsshub.app/apnews/topics/business-news",
        "category": "MACRO",
    },
    {
        "name": "MarketWatch",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "category": "MARKETS",
    },
    {
        "name": "MarketWatch Economy",
        "url": "https://feeds.marketwatch.com/marketwatch/economy-politics/",
        "category": "MACRO",
    },
    {
        "name": "WSJ Markets",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "category": "MARKETS",
    },
    {
        "name": "WSJ World",
        "url": "https://feeds.a.dj.com/rss/RSSWSJD.xml",
        "category": "MACRO",
    },
    {
        "name": "CNBC Finance",
        "url": "https://search.cnbc.com/rs/search/combinedcombined?partnerId=wrss01&hasCBSi=1&httpHSTS=false&rootExchangeFilter=CNBC&type=cnbcnewsstory&cvurl=http%3A%2F%2Fwww.cnbc.com%2Ffinance%2F",
        "category": "MARKETS",
    },
    {
        "name": "CNBC Economy",
        "url": "https://search.cnbc.com/rs/search/combinedcombined?partnerId=wrss01&hasCBSi=1&httpHSTS=false&rootExchangeFilter=CNBC&type=cnbcnewsstory&cvurl=http%3A%2F%2Fwww.cnbc.com%2Feconomy%2F",
        "category": "MACRO",
    },
    {
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/rss/topfinstories",
        "category": "MARKETS",
    },
    {
        "name": "FT Markets",
        "url": "https://www.ft.com/rss/home/uk",
        "category": "MACRO",
    },
    {
        "name": "Bloomberg Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "category": "MARKETS",
    },
    {
        "name": "Investing.com Economy",
        "url": "https://www.investing.com/rss/news_25.rss",
        "category": "MACRO",
    },
    {
        "name": "Investing.com Crypto",
        "url": "https://www.investing.com/rss/news_301.rss",
        "category": "CRYPTO",
    },
    {
        "name": "ForexLive",
        "url": "https://www.forexlive.com/feed/news",
        "category": "FOREX",
    },
    {
        "name": "FXStreet",
        "url": "https://www.fxstreet.com/rss/news",
        "category": "FOREX",
    },
    # Crypto
    {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "category": "CRYPTO",
    },
    {
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "category": "CRYPTO",
    },
    {
        "name": "The Block",
        "url": "https://www.theblock.co/rss.xml",
        "category": "CRYPTO",
    },
    {
        "name": "Decrypt",
        "url": "https://decrypt.co/feed",
        "category": "CRYPTO",
    },
    {
        "name": "Bitcoin Magazine",
        "url": "https://bitcoinmagazine.com/feed",
        "category": "CRYPTO",
    },
    # Central Bank / Official
    {
        "name": "Fed Reserve",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "category": "CB",
    },
    {
        "name": "ECB",
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "category": "CB",
    },
    {
        "name": "IMF News",
        "url": "https://www.imf.org/en/News/rss?language=eng",
        "category": "CB",
    },
    {
        "name": "BIS",
        "url": "https://www.bis.org/rss/press.xml",
        "category": "CB",
    },
]

# ─────────────────────────────────────────────
# IMPACT SCORING ENGINE
# ─────────────────────────────────────────────

# Each tuple: (regex_pattern, weight, reason_label)
# Weights are additive; thresholds: LOW <3 | MEDIUM 3-6 | HIGH >6

IMPACT_RULES = [
    # ── Central Bank / Monetary Policy ──────────────────── HIGH territory
    (r"\bfed(?:eral reserve)?\b.{0,60}\b(rates?|hike|cut|pivot|pause|decision|raises?|lowers?)\b", 8, "Fed rate decision"),
    (r"\b(rates?|hike|cut|pivot|pause|decision|raises?|lowers?)\b.{0,60}\bfed(?:eral reserve)?\b", 8, "Fed rate decision"),
    (r"\b(fomc|federal open market)\b", 8, "FOMC"),
    (r"\b(jerome powell|chair powell)\b", 7, "Powell"),
    (r"\b(ecb|european central bank)\b.*\b(rate|decision|hike|cut)\b", 7, "ECB rate"),
    (r"\b(boe|bank of england)\b.*\b(rate|decision|hike|cut)\b", 7, "BoE rate"),
    (r"\b(boj|bank of japan)\b.*\b(rate|decision|yield|policy)\b", 7, "BoJ policy"),
    (r"\binterest rates? (decision|hike|cut|hold|pause|raise|lower|increase|decrease)\b", 7, "Rate decision"),
    (r"\b(raises?|hikes?|cuts?|lowers?)\b.{0,20}\binterest rates?\b", 7, "Rate decision"),
    (r"\bquantitative (tightening|easing|qt|qe)\b", 6, "QT/QE"),
    (r"\byield curve (control|inversion|invert)\b", 6, "Yield curve"),

    # ── Macro Data Releases ──────────────────────────────
    (r"\b(cpi|consumer price index)\b", 7, "CPI"),
    (r"\b(pce|personal consumption expenditure)\b", 7, "PCE"),
    (r"\b(ppi|producer price index)\b", 5, "PPI"),
    (r"\bnon.?farm payrolls?\b", 8, "NFP"),
    (r"\bnfp\b", 8, "NFP"),
    (r"\bjobs report\b", 7, "Jobs report"),
    (r"\bunemployment (rate|claims|data)\b", 6, "Unemployment"),
    (r"\bgdp (data|growth|contraction|contracts?|print|report|miss|beat|shrinks?|expands?)\b", 7, "GDP"),
    (r"\b(gdp|gross domestic product)\b.{0,40}\b(contraction|contracts?|shrinks?|miss|beat|falls?|drops?)\b", 7, "GDP miss/beat"),
    (r"\b(retail sales|consumer spending)\b", 5, "Consumer data"),
    (r"\bism (manufacturing|services|pmi)\b", 5, "ISM/PMI"),
    (r"\bjolts\b", 5, "JOLTS"),
    (r"\b(trade deficit|trade balance|trade war)\b", 5, "Trade data"),
    (r"\b(core inflation|headline inflation)\b", 6, "Inflation print"),
    (r"\binflation (surges?|spikes?|jumps?|rises?|falls?|cools?|drops?)\b", 6, "Inflation move"),

    # ── Market Extremes ──────────────────────────────────
    (r"\b(crash|collapse|meltdown|black swan|flash crash)\b", 9, "Market crash"),
    (r"\b(circuit breaker|trading halt)\b", 9, "Circuit breaker"),
    (r"\b(bank run|bank failure|bank collapse)\b", 9, "Bank failure"),
    (r"\b(systemic risk|contagion|financial crisis)\b", 8, "Systemic risk"),
    (r"\b(recession|depression|stagflation)\b", 7, "Recession risk"),
    (r"\b(bear market|bull run)\b", 5, "Market trend"),
    (r"\b(record high|all.?time high|ath)\b", 5, "Record price"),
    (r"\b(record low|capitulation)\b", 5, "Record low"),
    (r"\bmarket (rout|selloff|sell.off|rally|surge|plunge|plunges?|tumbles?)\b", 5, "Market move"),

    # ── Geopolitical / Macro Shock ───────────────────────
    (r"\b(war|invasion|military strike|airstrike|conflict escalat)\b", 8, "Geopolitical conflict"),
    (r"\b(sanctions|embargo|export ban)\b", 7, "Sanctions"),
    (r"\b(tariff|trade war|trade deal)\b", 6, "Trade policy"),
    (r"\b(opec|oil production cut|oil supply)\b", 7, "OPEC/Oil supply"),
    (r"\b(supply chain disruption|supply shock)\b", 6, "Supply shock"),
    (r"\b(default|sovereign debt|debt ceiling)\b", 7, "Default/Debt"),
    (r"\bdebt ceiling\b", 7, "Debt ceiling"),
    (r"\b(government shutdown)\b", 6, "Gov shutdown"),
    (r"\b(election|electoral)\b.{0,40}\b(result|win|upset|landslide)\b", 6, "Election result"),

    # ── Crypto-Specific ──────────────────────────────────
    (r"\b(sec|cftc|regulator).{0,40}\b(bitcoin|ethereum|crypto|btc|eth)\b", 7, "Crypto regulation"),
    (r"\b(bitcoin|btc).{0,40}\b(etf|approval|reject)\b", 8, "BTC ETF"),
    (r"\b(ethereum|eth).{0,40}\b(etf|upgrade|merge|fork)\b", 7, "ETH ETF/upgrade"),
    (r"\b(exchange hack|crypto hack|exploit|stolen).{0,50}(?:million|billion|\$\d)", 8, "Crypto hack"),
    (r"\b(hack|exploit|stolen|drained).{0,30}(?:\$[\d,.]+\s*(?:million|billion)|[\d,.]+\s*(?:million|billion))", 8, "Crypto hack"),
    (r"\b(stablecoin|tether|usdc|usdt).{0,40}\b(depegs?|collapse|pause)\b", 8, "Stablecoin depeg"),
    (r"\b(usdt|usdc|tether|dai)\b.{0,30}\b(?:depegs?|lost peg|breaks? peg)\b", 8, "Stablecoin depeg"),
    (r"\bdepegs?\b.{0,30}\b(tether|usdt|usdc|stablecoin)\b", 8, "Stablecoin depeg"),
    (r"\b(ftx|binance|coinbase|kraken).{0,40}\b(bankrupt|ban|sued|charge|fine)\b", 8, "Exchange legal"),
    (r"\b(liquidation|long squeeze|short squeeze)\b", 6, "Liquidation event"),
    (r"\b(bitcoin halving|halving)\b", 7, "BTC halving"),
    (r"\bopen interest\b.{0,30}\b(record|surge|spike)\b", 5, "OI spike"),

    # ── Corporate / Earnings ─────────────────────────────
    (r"\b(bankruptcy|chapter 11|insolvent|default)\b", 8, "Bankruptcy"),
    (r"\b(earnings (miss|beat|surprise)|eps (miss|beat))\b", 5, "Earnings"),
    (r"\b(major acquisition|merger|buyout|takeover)\b.{0,40}\b(billion)\b", 6, "Big M&A"),
    (r"\b(layoffs?|mass layoff|workforce reduction).{0,30}\b(\d{1,3},?\d{3}|\d+%)\b", 5, "Layoffs"),
    (r"\b(ipo|initial public offering)\b", 4, "IPO"),
    (r"\b(stock split|reverse split)\b", 3, "Stock split"),
    (r"\bfed speak\b|\b(hawkish|dovish)\b.{0,20}\bfed\b", 5, "Fed speak"),

    # ── Boost for large numbers (scale of event) ─────────
    (r"\$[\d,]+\s*(?:million|billion|trillion)", 2, "Large figure"),
    (r"\b\d+\s*(?:billion|trillion)\b", 2, "Large figure"),

    # ── Source-based boost (CB category always +2) ───────
    # Applied in code, not here

    # ── Noise reducers (negative weight) ────────────────
    (r"\b(sports?|nfl|nba|celebrity|entertain|movie|award|oscar)\b", -5, "Noise: non-financial"),
    (r"\b(horoscope|recipe|travel tip|lifestyle)\b", -5, "Noise: lifestyle"),
    (r"\b(analyst upgrades? to (buy|hold))\b(?!.*(downgrade|cut))", -2, "Routine upgrade"),
    (r"\b(weekly (wrap|recap)|market summary for)\b", -2, "Summary/recap"),
]

# Compile patterns once
_COMPILED_RULES = [
    (re.compile(p, re.IGNORECASE), w, label)
    for p, w, label in IMPACT_RULES
]

CATEGORY_BOOST = {
    "CB": 3,      # Central bank feeds get a base boost
    "MACRO": 1,
    "FOREX": 1,
    "MARKETS": 0,
    "CRYPTO": 0,
}

IMPACT_THRESHOLDS = {
    "HIGH":   7,
    "MEDIUM": 3,
    "LOW":    0,
}

def score_headline(title: str, summary: str, category: str) -> tuple[int, str, list[str]]:
    """Return (score, impact_level, matched_labels)."""
    text = f"{title} {summary}"
    score = CATEGORY_BOOST.get(category, 0)
    matched = []
    for pattern, weight, label in _COMPILED_RULES:
        if pattern.search(text):
            score += weight
            if weight > 0:
                matched.append(label)
    score = max(0, score)
    if score >= IMPACT_THRESHOLDS["HIGH"]:
        level = "HIGH"
    elif score >= IMPACT_THRESHOLDS["MEDIUM"]:
        level = "MEDIUM"
    else:
        level = "LOW"
    return score, level, matched


# ─────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────

@dataclass
class Headline:
    id: str
    timestamp: datetime
    source: str
    category: str
    impact: str          # LOW / MEDIUM / HIGH
    score: int
    title: str
    url: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def ts_str(self) -> str:
        local = self.timestamp.astimezone()
        return local.strftime("%H:%M:%S")

    @property
    def date_str(self) -> str:
        local = self.timestamp.astimezone()
        return local.strftime("%m/%d")


# ─────────────────────────────────────────────
# FEED FETCHER
# ─────────────────────────────────────────────

def _make_id(url: str, title: str) -> str:
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()[:16]

def _parse_time(entry) -> datetime:
    """Best-effort datetime extraction from a feedparser entry."""
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
    return text.strip()

def fetch_source(source: dict) -> list[Headline]:
    """Fetch and parse one RSS source. Returns a list of Headline objects."""
    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)

    try:
        resp = requests.get(
            source["url"],
            timeout=12,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:
        try:
            # Fallback: let feedparser handle it directly
            feed = feedparser.parse(source["url"])
        except Exception:
            return results

    for entry in feed.entries:
        title = _clean(entry.get("title", "")).strip()
        if not title or len(title) < 10:
            continue

        url = entry.get("link", "")
        if not url:
            continue

        summary = _clean(entry.get("summary", entry.get("description", "")))

        pub_time = _parse_time(entry)
        if pub_time < cutoff:
            continue

        score, impact, tags = score_headline(title, summary, source["category"])
        hid = _make_id(url, title)

        results.append(Headline(
            id=hid,
            timestamp=pub_time,
            source=source["name"],
            category=source["category"],
            impact=impact,
            score=score,
            title=title,
            url=url,
            summary=summary,
            tags=tags,
        ))

    return results


# ─────────────────────────────────────────────
# HEADLINE STORE (thread-safe)
# ─────────────────────────────────────────────

class HeadlineStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._headlines: dict[str, Headline] = {}

    def add_many(self, items: list[Headline]):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
        with self._lock:
            for h in items:
                if h.id not in self._headlines:
                    self._headlines[h.id] = h
            # Prune old entries
            self._headlines = {
                k: v for k, v in self._headlines.items()
                if v.timestamp >= cutoff
            }

    def get_sorted(
        self,
        filter_impact: Optional[str] = None,
        filter_category: Optional[str] = None,
        search: str = "",
    ) -> list[Headline]:
        with self._lock:
            items = list(self._headlines.values())

        if filter_impact:
            items = [h for h in items if h.impact == filter_impact]
        if filter_category:
            items = [h for h in items if h.category == filter_category]
        if search:
            sq = search.lower()
            items = [h for h in items if sq in h.title.lower() or sq in h.source.lower()]

        items.sort(key=lambda h: (h.timestamp, h.score), reverse=True)
        return items[:MAX_HEADLINES]

    def count(self) -> dict:
        with self._lock:
            total = len(self._headlines)
            high = sum(1 for h in self._headlines.values() if h.impact == "HIGH")
            med = sum(1 for h in self._headlines.values() if h.impact == "MEDIUM")
        return {"total": total, "high": high, "medium": med, "low": total - high - med}


# ─────────────────────────────────────────────
# DETAIL SCREEN
# ─────────────────────────────────────────────

IMPACT_COLORS = {
    "HIGH": "bold red",
    "MEDIUM": "yellow",
    "LOW": "dim white",
}

CATEGORY_COLORS = {
    "CB": "bold magenta",
    "MACRO": "cyan",
    "FOREX": "blue",
    "MARKETS": "green",
    "CRYPTO": "bright_yellow",
}

class DetailScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "dismiss", "Close"),
    ]

    def __init__(self, headline: Headline):
        super().__init__()
        self.headline = h = headline

    def compose(self) -> ComposeResult:
        h = self.headline
        impact_color = IMPACT_COLORS.get(h.impact, "white")
        cat_color = CATEGORY_COLORS.get(h.category, "white")

        tags_str = ", ".join(h.tags) if h.tags else "—"
        summary_str = h.summary if h.summary else "(no summary available)"

        # OSC 8 hyperlink for terminals that support it
        link_text = f"\033]8;;{h.url}\033\\{h.url}\033]8;;\033\\"

        content = (
            f"[bold]{h.title}[/bold]\n\n"
            f"[{impact_color}]⬥ IMPACT: {h.impact}  (score: {h.score})[/{impact_color}]   "
            f"[{cat_color}]▸ {h.category}[/{cat_color}]\n\n"
            f"[dim]Source:[/dim]  {h.source}\n"
            f"[dim]Time:[/dim]    {h.date_str} {h.ts_str}\n"
            f"[dim]Tags:[/dim]    {tags_str}\n\n"
            f"[dim]Summary:[/dim]\n{summary_str}\n\n"
            f"[dim]Link:[/dim]\n[link={h.url}]{h.url}[/link]\n\n"
            f"[dim]Press [bold]ESC[/bold] or [bold]Q[/bold] to return[/dim]"
        )

        yield Vertical(
            Static(content, id="detail-content"),
            id="detail-panel"
        )

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
    }
    #detail-panel {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $accent;
        padding: 2 4;
        overflow-y: auto;
    }
    #detail-content {
        width: 100%;
    }
    """


# ─────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────

class InfoHunter(App):
    """InfoHunter — Quantasset Terminal News Aggregator"""

    TITLE = "InfoHunter"
    SUB_TITLE = "Quantasset Market Intelligence"

    CSS = """
    Screen {
        background: $background;
    }
    #toolbar {
        height: 1;
        background: $panel;
        padding: 0 2;
        color: $text-muted;
    }
    #toolbar Label {
        margin-right: 2;
    }
    #status-bar {
        height: 1;
        background: $panel;
        padding: 0 2;
        color: $text-muted;
    }
    DataTable {
        height: 1fr;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $text;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $accent 50%;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh now"),
        Binding("f", "cycle_filter_impact", "Filter Impact"),
        Binding("c", "cycle_filter_category", "Filter Category"),
        Binding("s", "toggle_search", "Search"),
        Binding("enter", "open_detail", "View Detail"),
        Binding("escape", "clear_filters", "Clear filters"),
        Binding("q", "quit", "Quit"),
        Binding("h,?", "show_help", "Help"),
    ]

    # Reactive state
    filter_impact: reactive[Optional[str]] = reactive(None)
    filter_category: reactive[Optional[str]] = reactive(None)
    search_query: reactive[str] = reactive("")
    last_refresh: reactive[str] = reactive("never")
    total_count: reactive[int] = reactive(0)

    IMPACT_CYCLE = [None, "HIGH", "MEDIUM", "LOW"]
    CATEGORY_CYCLE = [None, "CB", "MACRO", "FOREX", "MARKETS", "CRYPTO"]

    def __init__(self):
        super().__init__()
        self.store = HeadlineStore()
        self._rows: list[Headline] = []
        self._fetching = False

    # ── Build UI ───────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            Label("[bold cyan]IMPACT:[/bold cyan] ALL", id="lbl-impact"),
            Label("[bold cyan]CAT:[/bold cyan] ALL", id="lbl-cat"),
            Label("[bold cyan]SEARCH:[/bold cyan] —", id="lbl-search"),
            Label("", id="lbl-fetch"),
            id="toolbar",
        )
        table = DataTable(id="headline-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("TIME", "IMP", "CAT", "SOURCE", "HEADLINE")
        yield table
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_table()
        self.fetch_all_sources()
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.fetch_all_sources)
        # Update status every second
        self.set_interval(1, self._update_status)

    # ── Fetch ──────────────────────────────────

    @work(thread=True)
    def fetch_all_sources(self) -> None:
        self._fetching = True
        self.call_from_thread(self._set_fetch_label, "⟳ fetching…")

        new_count = 0
        for source in SOURCES:
            try:
                items = fetch_source(source)
                before = len(self.store._headlines)
                self.store.add_many(items)
                after = len(self.store._headlines)
                new_count += after - before
            except Exception:
                pass

        ts = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(self._set_fetch_label, f"✓ {ts}")
        self.last_refresh = ts
        self._fetching = False
        self.call_from_thread(self._rebuild_table)

    def _set_fetch_label(self, text: str):
        try:
            self.query_one("#lbl-fetch", Label).update(text)
        except Exception:
            pass

    # ── Table rebuild ──────────────────────────

    def _rebuild_table(self):
        self._rows = self.store.get_sorted(
            filter_impact=self.filter_impact,
            filter_category=self.filter_category,
            search=self.search_query,
        )
        table = self.query_one("#headline-table", DataTable)
        table.clear()

        for h in self._rows:
            impact_color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}.get(h.impact, "white")
            cat_color = CATEGORY_COLORS.get(h.category, "white")

            time_cell = Text(f"{h.date_str} {h.ts_str}", style="dim")
            impact_cell = Text(h.impact, style=impact_color + (" bold" if h.impact == "HIGH" else ""))
            cat_cell = Text(h.category, style=cat_color)
            source_cell = Text(h.source, style="dim cyan")

            # Title: color by impact; truncate for display
            title_display = h.title[:110] + ("…" if len(h.title) > 110 else "")
            if h.impact == "HIGH":
                title_cell = Text(title_display, style="bold red")
            elif h.impact == "MEDIUM":
                title_cell = Text(title_display, style="yellow")
            else:
                title_cell = Text(title_display)

            table.add_row(time_cell, impact_cell, cat_cell, source_cell, title_cell)

        counts = self.store.count()
        self.total_count = counts["total"]
        self._update_toolbar()
        self._update_status()

    # ── Status updates ─────────────────────────

    def _update_toolbar(self):
        try:
            imp_label = self.filter_impact or "ALL"
            cat_label = self.filter_category or "ALL"
            srch_label = self.search_query or "—"
            self.query_one("#lbl-impact", Label).update(f"[bold cyan]IMPACT:[/bold cyan] {imp_label}")
            self.query_one("#lbl-cat", Label).update(f"[bold cyan]CAT:[/bold cyan] {cat_label}")
            self.query_one("#lbl-search", Label).update(f"[bold cyan]SEARCH:[/bold cyan] {srch_label}")
        except Exception:
            pass

    def _update_status(self):
        try:
            counts = self.store.count()
            shown = len(self._rows)
            status = (
                f" {counts['total']} headlines (12h) │ "
                f"[red]HIGH: {counts['high']}[/red] │ "
                f"[yellow]MED: {counts['medium']}[/yellow] │ "
                f"LOW: {counts['low']} │ "
                f"Showing: {shown} │ "
                f"Refresh in: {self._seconds_to_refresh()}s"
            )
            self.query_one("#status-bar", Static).update(status)
        except Exception:
            pass

    def _seconds_to_refresh(self) -> int:
        # Approximate; real timer is managed by Textual
        return REFRESH_INTERVAL_SECONDS

    # ── Actions ────────────────────────────────

    def action_refresh(self) -> None:
        self.fetch_all_sources()

    def action_cycle_filter_impact(self) -> None:
        cycle = self.IMPACT_CYCLE
        current = self.filter_impact
        idx = cycle.index(current) if current in cycle else 0
        self.filter_impact = cycle[(idx + 1) % len(cycle)]
        self._rebuild_table()

    def action_cycle_filter_category(self) -> None:
        cycle = self.CATEGORY_CYCLE
        current = self.filter_category
        idx = cycle.index(current) if current in cycle else 0
        self.filter_category = cycle[(idx + 1) % len(cycle)]
        self._rebuild_table()

    def action_clear_filters(self) -> None:
        self.filter_impact = None
        self.filter_category = None
        self.search_query = ""
        self._rebuild_table()

    def action_toggle_search(self) -> None:
        import asyncio
        from textual.widgets import Input

        async def _do_search():
            q = await self.app.push_screen_wait(SearchScreen())
            if q is not None:
                self.search_query = q
            self._rebuild_table()

        asyncio.create_task(_do_search())

    def action_open_detail(self) -> None:
        table = self.query_one("#headline-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < len(self._rows):
            h = self._rows[table.cursor_row]
            self.push_screen(DetailScreen(h))

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is not None and event.cursor_row < len(self._rows):
            h = self._rows[event.cursor_row]
            self._update_selected_info(h)

    def _update_selected_info(self, h: Headline):
        try:
            status = (
                f" [bold]{h.source}[/bold] │ Score: {h.score} │ "
                f"Tags: {', '.join(h.tags) if h.tags else '—'} │ "
                f"[link={h.url}]{h.url[:80]}[/link]"
            )
            self.query_one("#status-bar", Static).update(status)
        except Exception:
            pass


# ─────────────────────────────────────────────
# SEARCH SCREEN
# ─────────────────────────────────────────────

class SearchScreen(Screen[Optional[str]]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        yield Vertical(
            Static("[bold]Search headlines[/bold]\n(press Enter to apply, ESC to cancel)", id="search-title"),
            Input(placeholder="e.g. fed rate, bitcoin, recession…", id="search-input"),
            id="search-panel",
        )

    def on_mount(self):
        self.query_one("#search-input").focus()

    def on_input_submitted(self, event) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)

    DEFAULT_CSS = """
    SearchScreen {
        align: center middle;
    }
    #search-panel {
        width: 60;
        height: auto;
        background: $surface;
        border: round $accent;
        padding: 2 4;
    }
    #search-title {
        margin-bottom: 1;
    }
    """


# ─────────────────────────────────────────────
# HELP SCREEN
# ─────────────────────────────────────────────

class HelpScreen(Screen):
    BINDINGS = [Binding("escape,q,h,?", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        help_text = """[bold cyan]InfoHunter — Quantasset Market Intelligence[/bold cyan]

[bold]KEYBINDINGS[/bold]
  [yellow]↑ / ↓[/yellow]        Scroll headlines
  [yellow]PgUp / PgDn[/yellow]  Scroll fast
  [yellow]Enter[/yellow]        Open detail view (+ hyperlink)
  [yellow]R[/yellow]            Force refresh now
  [yellow]F[/yellow]            Cycle impact filter: ALL → HIGH → MEDIUM → LOW
  [yellow]C[/yellow]            Cycle category filter: ALL → CB → MACRO → FOREX → MARKETS → CRYPTO
  [yellow]S[/yellow]            Open search bar
  [yellow]ESC[/yellow]          Clear all filters
  [yellow]Q[/yellow]            Quit

[bold]IMPACT LEVELS[/bold]
  [bold red]HIGH[/bold red]      Significant market-moving event (FOMC, NFP, crash, CB action…)
  [yellow]MEDIUM[/yellow]    Noteworthy but context-dependent (earnings, PMI, Fed speak…)
  [dim]LOW[/dim]       Background noise / informational

[bold]CATEGORIES[/bold]
  [bold magenta]CB[/bold magenta]       Central Bank (Fed, ECB, BoJ, BoE, BIS, IMF)
  [cyan]MACRO[/cyan]    Macroeconomic news (Reuters, WSJ, MarketWatch…)
  [blue]FOREX[/blue]    FX / ForexLive / FXStreet
  [green]MARKETS[/green]  Equity & markets news
  [bright_yellow]CRYPTO[/bright_yellow]  Crypto / CoinDesk / Cointelegraph / The Block

[bold]NOTES[/bold]
  • Headlines auto-refresh every 90 seconds from {count} sources
  • 12-hour rolling window; up to {max} headlines retained
  • Click a headline to open detail + copy the article URL
  • Hyperlinks in detail view open in your browser if your terminal supports OSC 8

[dim]Press ESC, Q, H, or ? to close[/dim]
""".format(count=len(SOURCES), max=MAX_HEADLINES)
        yield Vertical(
            Static(help_text, id="help-content"),
            id="help-panel",
        )

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-panel {
        width: 80;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $accent;
        padding: 2 4;
        overflow-y: auto;
    }
    """


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Quick dependency check
    missing = []
    try:
        import feedparser
    except ImportError:
        missing.append("feedparser")
    try:
        import textual
    except ImportError:
        missing.append("textual")
    try:
        import requests
    except ImportError:
        missing.append("requests")

    if missing:
        print(f"[InfoHunter] Missing dependencies: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        sys.exit(1)

    app = InfoHunter()
    app.run()

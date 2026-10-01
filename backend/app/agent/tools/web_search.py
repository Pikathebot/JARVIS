import logging
from typing import Optional

logger = logging.getLogger("jarvis.agent.tools.web_search")


def web_search(query: str, max_results: int = 5, news: bool = False) -> str:
    """
    Search the web (DuckDuckGo) for current facts, documentation, releases or prices; news=true searches recent news articles, with each one's date and source. End an answer that used the web with the links you used.

    Args:
        query: Short search terms (e.g. 'Python 3.13 features'), never pasted document or chat text.
        max_results: Maximum number of top results to retrieve (default 5, maximum 10).
        news: Search news articles (dated, newest first) instead of web pages.
    """

    clean_query = str(query or "").strip()
    if not clean_query:
        return "Error: Search query cannot be empty."

    # Clamp max_results between 1 and 10 to protect context window tokens
    try:
        limit = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        limit = 5

    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            if news:
                raw_results = list(ddgs.news(clean_query, max_results=limit))
                raw_results.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
            else:
                raw_results = list(ddgs.text(clean_query, max_results=limit))

        if not raw_results:
            return f"No {'news' if news else 'search'} results found for query: '{clean_query}'."

        formatted_cards = []
        for idx, item in enumerate(raw_results, start=1):
            title = (item.get("title") or "Untitled").strip()
            href = (item.get("href") or item.get("url") or "").strip()
            snippet = (item.get("body") or "").strip()
            card = f"### [{idx}] {title}\n- **URL**: {href}"
            if news:
                date = str(item.get("date") or "")[:10] or "undated"
                card += f"\n- **Published**: {date} by {(item.get('source') or 'unknown source').strip()}"
            card += f"\n- **Summary**: {snippet}"
            formatted_cards.append(card)

        heading = "News" if news else "Search Results"
        return f"## {heading} for: \"{clean_query}\"\n\n" + "\n\n".join(formatted_cards)

    except ImportError:
        logger.error("duckduckgo_search library is not installed.")
        return "Error: 'duckduckgo_search' package is missing. Please install it with 'pip install duckduckgo_search'."
    except Exception as e:
        logger.warning("Web search failed for '%s': %s", clean_query, e)
        return f"Error during web search for '{clean_query}': {str(e)}"

from app.agent import research as pipeline


def research(question: str, query: str = "") -> str:
    """
    Answer a question from the web: searches, opens the top pages and returns the passages that answer it, with their sources. Use it for anything current, recent or that you're unsure of (news, releases, new seasons or versions, prices, mods, who-is-now) instead of answering from memory; end the answer with the links you used.

    Args:
        question: The user's question, in full (e.g. 'what is the best Black Myth: Wukong mod for multiplayer').
        query: Optional short search terms, if the question itself searches badly.
    """
    q = str(question or "").strip()
    if not q:
        return "Error: research needs the question to answer."
    try:
        text, _ = pipeline.research(q, pipeline.ddg_search, pipeline.page_text, query=str(query or ""))
    except ImportError:
        return "Error: the web search package (ddgs) is not installed."
    except Exception as exc:
        return f"Error during web research for '{q}': {exc}"
    return text

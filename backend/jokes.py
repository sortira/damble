"""Dad joke sourcing for Damble.

Fetches jokes from https://icanhazdadjoke.com/api and falls back to a local
list if the network is unavailable. The public API requires a descriptive
User-Agent and returns JSON when asked with the right Accept header.
"""
import random

import httpx

API_SEARCH = "https://icanhazdadjoke.com/search"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Damble Game (https://github.com/example/damble)",
}

# Used when the API is unreachable so the prototype always works offline.
FALLBACK_JOKES = [
    "I only know 25 letters of the alphabet. I don't know y.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Did you hear about the restaurant on the moon? Great food, no atmosphere.",
    "I used to be a banker but I lost interest.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I would tell you a chemistry joke but I know I wouldn't get a reaction.",
    "What do you call fake spaghetti? An impasta.",
    "How do you organize a space party? You planet.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "I'm on a seafood diet. I see food and I eat it.",
    "What did the ocean say to the beach? Nothing, it just waved.",
    "Why can't your nose be 12 inches long? Because then it would be a foot.",
    "I don't trust stairs. They're always up to something.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "Dad, did you get a haircut? No, I got them all cut.",
    "Why did the bicycle fall over? Because it was two-tired.",
    "What's brown and sticky? A stick.",
    "I made a pencil with two erasers. It was pointless.",
    "How does a penguin build its house? Igloos it together.",
]


async def get_jokes(count: int) -> list[str]:
    """Return `count` dad jokes, preferring the live API, topping up with fallbacks."""
    jokes: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=HEADERS) as client:
            page = random.randint(1, 20)
            resp = await client.get(API_SEARCH, params={"limit": 30, "page": page})
            resp.raise_for_status()
            data = resp.json()
            jokes = [item["joke"] for item in data.get("results", []) if item.get("joke")]
            random.shuffle(jokes)
    except Exception:
        jokes = []

    pool = jokes + random.sample(FALLBACK_JOKES, len(FALLBACK_JOKES))

    result: list[str] = []
    seen: set[str] = set()
    for joke in pool:
        if joke not in seen:
            seen.add(joke)
            result.append(joke)
        if len(result) >= count:
            break

    # Last resort: allow repeats if we still don't have enough.
    while len(result) < count:
        result.append(random.choice(FALLBACK_JOKES))

    return result[:count]

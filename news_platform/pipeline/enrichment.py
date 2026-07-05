import re
from collections import Counter
from typing import Dict, List

from news_platform.models import ExtractedArticle, SummaryBundle

TOPIC_KEYWORDS = {
    "politics": {"election", "government", "minister", "president", "parliament", "policy", "senate"},
    "finance": {"market", "stock", "inflation", "bank", "economy", "trade", "interest", "earnings"},
    "tech": {"ai", "software", "technology", "startup", "cyber", "chip", "robot", "cloud"},
    "science": {"research", "space", "climate", "study", "scientist", "physics", "biology"},
    "sports": {"match", "league", "football", "soccer", "nba", "olympic", "tournament"},
    "world news": {"war", "global", "country", "border", "diplomat", "international", "crisis"},
}

STOPWORDS = {
    "about", "after", "again", "also", "from", "have", "into", "more", "news", "over", "said",
    "that", "their", "there", "this", "with", "will", "were", "when", "which", "while", "your",
}


class EnrichmentEngine:
    def summarize(self, article: ExtractedArticle, translate: bool = False) -> SummaryBundle:
        sentences = self._sentences(article.text)
        tldr = " ".join(sentences[:2]) if sentences else article.title
        bullets = sentences[:4] if len(sentences) >= 2 else [article.title]
        full = " ".join(sentences[:6]) if sentences else article.text
        translated = self._pseudo_translate(tldr, article.language) if translate else None
        return SummaryBundle(tldr=tldr, bullets=bullets, full=full, translated=translated)

    def classify_topic(self, article: ExtractedArticle) -> str:
        haystack = f"{article.title} {article.text}".lower()
        scores = {
            topic: sum(1 for keyword in keywords if re.search(rf"\b{re.escape(keyword)}\b", haystack))
            for topic, keywords in TOPIC_KEYWORDS.items()
        }
        best, score = max(scores.items(), key=lambda item: item[1])
        return best if score > 0 else "world news"

    def keywords(self, article: ExtractedArticle, limit: int = 12) -> List[str]:
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][\wÀ-ÖØ-öø-ÿ-]{3,}", f"{article.title} {article.text}".lower())
        counts = Counter(word for word in words if word not in STOPWORDS)
        return [word for word, _ in counts.most_common(limit)]

    def entities(self, article: ExtractedArticle) -> Dict[str, List[str]]:
        text = f"{article.title}. {article.text}"
        candidates = re.findall(r"\b[A-Z][a-zA-ZÀ-ÖØ-öø-ÿ]+(?:\s+[A-Z][a-zA-ZÀ-ÖØ-öø-ÿ]+){0,3}\b", text)
        clean = []
        for item in candidates:
            if item not in clean and len(item) > 2 and item.lower() not in STOPWORDS:
                clean.append(item)
        orgs = [item for item in clean if any(token in item for token in ("Inc", "Corp", "University", "Bank", "Agency", "Council"))][:8]
        locations = [item for item in clean if item in {"United States", "Germany", "Iran", "China", "Europe", "Berlin", "Tehran", "London", "Paris"}][:8]
        people = [item for item in clean if item not in orgs and item not in locations][:8]
        return {"people": people, "organizations": orgs, "locations": locations}

    def _sentences(self, text: str) -> List[str]:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if len(s.strip()) > 20]
        return sentences[:10]

    def _pseudo_translate(self, text: str, language: str) -> str:
        if language == "en":
            return text
        return text

import re


PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "EMAIL"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN"),
    (re.compile(r"\b\d{10,}\b"), "PHONE"),
    (re.compile(r"\b(ghp|GHP|sk|sk-|pat|pats|ac_sk_|ac_broker_)[A-Za-z0-9_]{20,}\b"), "API_KEY"),
    (re.compile(r"\b(AIza|ya29\.|BQBN|ya0\.)[A-Za-z0-9_-]{20,}\b"), "GOOGLE_API_KEY"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OPENAI_KEY"),
    (re.compile(r"\bamzn\.[A-Za-z0-9=_-]{20,}\b"), "AWS_KEY"),
    (re.compile(r"\b(A3T|AQS|AGPA|SM|ASIA|AC)[A-Z0-9]{16,}\b"), "AWS_KEY"),
]


def contains_pii(text: str) -> bool:
    for pattern, _ in PII_PATTERNS:
        if pattern.search(text):
            return True
    return False


def scan_pii(text: str) -> list[str]:
    found = []
    for pattern, label in PII_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found